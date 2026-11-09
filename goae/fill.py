"""Complete a CSV table with GOÄ codes, one row at a time.

The loop lives here, not in an agent conversation: every row gets a fresh,
tiny prompt (row + top-k candidates) and the result is written to the file
immediately. The table *is* the memory, so nothing accumulates in any
context window and an interrupted run simply continues where it stopped.

Without --llm the candidates are written as-is (useful for review or for
any other tool). With --llm an OpenAI-compatible endpoint (Ollama,
llama.cpp server, LM Studio, vLLM, ...) picks one candidate per row.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from . import db

PROMPT = """Wähle die passende GOÄ-Ziffer für die Leistung.
Leistung: {row}
Kandidaten (Ziffer|Punkte|Text):
{candidates}
Antworte nur mit JSON: {{"code": "<Ziffer oder leer>", "confidence": <0..1>}}"""


def ask_llm(prompt: str, url: str, model: str, timeout: int = 120) -> dict:
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', 'local')}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def fill(src: Path, dst: Path, col: str | None, k: int, llm: str | None, model: str,
         min_conf: float) -> None:
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    if not rows:
        print("empty table", file=sys.stderr)
        return
    col = col or next(iter(rows[0]))
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    done = {}
    for prev in (dst, tmp):  # resume: finished output or an interrupted run
        if prev.exists():
            for r in csv.DictReader(prev.open(encoding="utf-8")):
                if r.get("goae_candidates") and (r.get("goae") or not llm):
                    done[r[col]] = r
    fields = list(rows[0]) + [f for f in ("goae", "goae_confidence", "goae_candidates")
                              if f not in rows[0]]
    profile = db.Profile(k=k)
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, row in enumerate(rows, 1):
            if row[col] in done:
                w.writerow({k_: done[row[col]].get(k_, "") for k_ in fields})
                continue
            cands = db.search(row[col], profile=profile)
            codes = [l.split("|", 1)[0] for l in cands.splitlines() if "|" in l]
            row["goae_candidates"] = " ".join(codes)
            if llm and codes:
                prompt = PROMPT.format(row=row[col], candidates=cands)
                try:
                    ans = ask_llm(prompt, llm, model)
                except Exception as e:  # keep going, the row stays reviewable
                    ans = {}
                    print(f"row {i}: LLM error {e}", file=sys.stderr)
                code = db.normalize_code(str(ans.get("code", "")))
                conf = float(ans.get("confidence") or 0)
                if code in codes and conf >= min_conf:  # never accept a code outside the candidates
                    row["goae"], row["goae_confidence"] = code, f"{conf:.2f}"
                else:
                    row["goae"], row["goae_confidence"] = "", f"{conf:.2f}"
            w.writerow({k_: row.get(k_, "") for k_ in fields})
            f.flush()
            print(f"{i}/{len(rows)} {row[col][:40]!r} -> {row.get('goae') or row['goae_candidates']}",
                  file=sys.stderr)
    tmp.replace(dst)


# ------------------------------------------------- agent mode: next / set
# For agents that work through a table themselves. The agent never reads the
# file: `next` shows one open row with its candidates, `set` writes the answer
# back immediately. Progress lives in the file, so a new session can continue.

def _load(path: Path, col: str | None):
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"{path}: empty table")
    fields = list(rows[0])
    if "goae" not in fields:
        fields.append("goae")
    return rows, fields, col or fields[0]


def next_row(path: Path, col: str | None = None, k: int = 5) -> str:
    rows, _fields, col = _load(path, col)
    open_rows = [i for i, r in enumerate(rows, 1) if not (r.get("goae") or "").strip()]
    if not open_rows:
        unsure = [str(i) for i, r in enumerate(rows, 1) if r.get("goae") == "?"]
        return f"done: {len(rows)} rows" + (f", '?' in rows {', '.join(unsure)}" if unsure else "")
    i = open_rows[0]
    cands = db.search(rows[i - 1][col], profile=db.Profile(k=k))
    return f"row {i} ({len(open_rows)} open): {rows[i - 1][col]}\n{cands}"


def set_row(path: Path, row: int, code: str, col: str | None = None) -> str:
    rows, fields, _col = _load(path, col)
    if not 1 <= row <= len(rows):
        raise SystemExit(f"row {row} out of range 1..{len(rows)}")
    if code != "?":
        r = db._row(db.connect(), code)
        if r is None:
            return f"unknown code {code}: nothing written (use ? if no code fits)"
        code = r["code"]
    rows[row - 1]["goae"] = code
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)
    tmp.replace(path)
    left = sum(1 for r in rows if not (r.get("goae") or "").strip())
    return f"row {row} = {code} ({left} open)"
