"""Complete a CSV table with GOÄ codes.

Two ways, both keep the progress in an output file (``<input>.goae.csv``)
instead of in a conversation, and both can be interrupted and resumed:

* ``fill``      – a script loop. Every row gets a fresh, tiny LLM prompt
                  (row + top-k candidates). The LLM is either an
                  OpenAI-compatible endpoint (``--llm``) or any command line
                  tool that reads a prompt on stdin (``--llm-cmd``, e.g.
                  ``ollama run <model>``, or ``claude`` for a slimmed-down
                  Claude Code call). Without either,
                  only the candidates are written.
* ``next``/``set`` – for an agent that works through the table itself. The
                  agent never opens the file: ``next`` shows a batch of open
                  rows with candidates, ``set`` saves answers and shows the
                  next batch. The input file is never modified, so editors and
                  agents that watch it do not re-inject it into the context.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import db

PROMPT = """Wähle die passende GOÄ-Ziffer für die Leistung.
Leistung: {row}
Kandidaten (Ziffer|Punkte|Text):
{candidates}
Antworte nur mit JSON: {{"code": "<Ziffer oder leer>", "confidence": <0..1>}}"""

EXTRA = ("goae", "goae_confidence", "goae_candidates")


def output_path(src: Path) -> Path:
    return src.with_name(src.stem + ".goae.csv")


# ----------------------------------------------------------------- LLM calls

def _json_from(text: str) -> dict:
    m = re.search(r"\{.*?\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def ask_llm(prompt: str, url: str, model: str, timeout: int = 120) -> dict:
    """OpenAI-compatible chat completion (Ollama, llama.cpp, LM Studio, vLLM, …)."""
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
        return _json_from(json.load(r)["choices"][0]["message"]["content"])


# `--llm-cmd claude`: Claude Code as a plain question-answer tool. A default
# `claude -p` call carries Claude Code's full system prompt, tool definitions,
# MCP servers and skills: ~42,000 input tokens per call when measured (Claude
# Code 2.1.321). These flags replace or switch off all of that: ~3,400 tokens.
CLAUDE_SYSTEM = ("Du ordnest medizinische Leistungen GOÄ-Ziffern zu. "
                 "Antworte ausschließlich mit dem verlangten JSON.")
CLAUDE_PRESET = ["claude", "-p", "--model", "haiku", "--system-prompt", CLAUDE_SYSTEM,
                 "--tools", "", "--strict-mcp-config", "--disallowedTools", "mcp__*",
                 "--no-chrome", "--disable-slash-commands", "--no-session-persistence"]


def ask_cmd(prompt: str, cmd: str, timeout: int = 300) -> dict:
    """Any CLI that reads the prompt on stdin and prints the answer.
    Each call is a new process, so every row starts with an empty conversation.
    The special value 'claude' runs Claude Code with CLAUDE_PRESET."""
    if cmd.strip() == "claude":
        import shutil
        exe = shutil.which("claude") or "claude"
        p = subprocess.run([exe, *CLAUDE_PRESET[1:]], input=prompt, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    else:
        p = subprocess.run(cmd, shell=True, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:200] or f"exit code {p.returncode}")
    return _json_from(p.stdout)


# --------------------------------------------------------------- table state

def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _state(src: Path, col: str | None, out: Path | None = None):
    """Input rows merged with results from the output file (matched by row number)."""
    rows = _read(src)
    if not rows:
        raise SystemExit(f"{src}: empty table")
    fields = list(rows[0]) + [k for k in EXTRA if k not in rows[0]]
    col = col or fields[0]
    out = out or output_path(src)
    if out.exists():
        done = _read(out)
        if len(done) == len(rows) and all(d.get(col) == r.get(col) for d, r in zip(done, rows)):
            for r, d in zip(rows, done):
                for k in EXTRA:
                    if d.get(k):
                        r[k] = d[k]
    return rows, fields, col


def _write(path: Path, rows: list[dict], fields: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)
    tmp.replace(path)


# ---------------------------------------------------------------- script loop

def fill(src: Path, dst: Path | None, col: str | None, k: int, llm: str | None, model: str,
         min_conf: float, llm_cmd: str | None = None) -> Path:
    dst = dst or output_path(src)
    rows, fields, col = _state(src, col, dst)
    use_llm = bool(llm or llm_cmd)
    profile = db.Profile(k=k)
    for i, row in enumerate(rows, 1):
        if row.get("goae") or (not use_llm and row.get("goae_candidates")):
            continue  # resume: already done
        cands = db.search(row[col], profile=profile)
        codes = [l.split("|", 1)[0] for l in cands.splitlines() if "|" in l]
        row["goae_candidates"] = " ".join(codes)
        if use_llm and codes:
            prompt = PROMPT.format(row=row[col], candidates=cands)
            try:
                ans = ask_cmd(prompt, llm_cmd) if llm_cmd else ask_llm(prompt, llm, model)
            except Exception as e:  # keep going, the row stays reviewable
                ans = {}
                print(f"row {i}: LLM error {e}", file=sys.stderr)
            code = db.normalize_code(str(ans.get("code", "")))
            conf = float(ans.get("confidence") or 0)
            ok = code in codes and conf >= min_conf  # never accept a code outside the candidates
            row["goae"], row["goae_confidence"] = (code if ok else ""), f"{conf:.2f}"
        _write(dst, rows, fields)  # after every row: progress survives interruptions
        print(f"{i}/{len(rows)} {row[col][:40]!r} -> {row.get('goae') or row['goae_candidates']}",
              file=sys.stderr)
    _write(dst, rows, fields)
    return dst


# ------------------------------------------------- agent mode: next / set

def next_rows(src: Path, col: str | None = None, k: int = 3, n: int = 10) -> str:
    """Up to n open rows, each with its top-k candidates (short texts)."""
    rows, _fields, col = _state(src, col)
    open_rows = [i for i, r in enumerate(rows, 1) if not (r.get("goae") or "").strip()]
    if not open_rows:
        unsure = [str(i) for i, r in enumerate(rows, 1) if r.get("goae") == "?"]
        return (f"done: {len(rows)} rows → {output_path(src).name}"
                + (f", '?' in rows {', '.join(unsure)}" if unsure else ""))
    profile = db.Profile(k=k, text_max=60)
    out = [f"{len(open_rows)} open; answer with: set {src.name} ROW=CODE … (? = nothing fits)"]
    for i in open_rows[:n]:
        out.append(f"#{i} {rows[i - 1][col]}")
        out += ["  " + line for line in db.search(rows[i - 1][col], profile=profile).splitlines()]
    return "\n".join(out)


def set_rows(src: Path, assignments: list[str], col: str | None = None,
             k: int = 3, n: int | None = None) -> str:
    """Save ROW=CODE pairs to the output file, then show the next batch."""
    rows, fields, col = _state(src, col)
    saved, errors = [], []
    con = db.connect()
    for a in assignments:
        m = re.fullmatch(r"#?(\d+)=(.+)", a.strip())
        if not m:
            errors.append(f"{a}: expected ROW=CODE")
            continue
        row, code = int(m.group(1)), m.group(2).strip()
        if not 1 <= row <= len(rows):
            errors.append(f"{row}: no such row")
            continue
        if code != "?":
            r = db._row(con, code)
            if r is None:
                errors.append(f"{row}: unknown code {code}")
                continue
            code = r["code"]
        rows[row - 1]["goae"] = code
        saved.append(f"{row}={code}")
    if saved:
        _write(output_path(src), rows, fields)
    head = (f"saved {' '.join(saved)}" if saved else "nothing saved") + \
           (f"; errors: {'; '.join(errors)}" if errors else "")
    return head + "\n" + next_rows(src, col, k, n or max(len(assignments), 1))
