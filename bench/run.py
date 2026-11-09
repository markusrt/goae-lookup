#!/usr/bin/env python3
"""Benchmark loop: how much context does an agent need per GOÄ question?

For every approach ("iteration") the example tasks in bench/tasks.json are
answered with a scripted, optimal sequence of tool calls. We measure

* schema  – tokens of the tool definitions (sent with every request),
* output  – tokens of all tool results needed to answer the task,
* success – whether the answer is actually contained in those results.

Tokens are estimated as ceil(chars / 4). German tokenizes somewhat worse on
most tokenizers, so absolute numbers are optimistic, but ratios hold.

Usage:
  python bench/run.py                 # all iterations -> bench/results.json + CONTEXT_STATS.md
  python bench/run.py --only it5      # one iteration, print details
  python bench/run.py --log "note"    # also append the run to bench/history.jsonl
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goae import db, tools  # noqa: E402
from goae.ingest import DEFAULT_SOURCE  # noqa: E402

TASKS = json.loads((ROOT / "bench" / "tasks.json").read_text(encoding="utf-8"))
HISTORY = ROOT / "bench" / "history.jsonl"
CTX_LIMIT = 32_000  # the colleague's local model


def tok(s: str) -> int:
    return math.ceil(len(s) / 4)


# ---------------------------------------------------------------- approaches

class FullDoc:
    """Paste the whole fee schedule into the prompt."""
    name = "full document in context"

    def __init__(self):
        self.text = DEFAULT_SOURCE.read_text(encoding="utf-8")

    def schema(self):
        return ""

    def run(self, t):
        return [("(document)", self.text)] if t is TASKS["dev"][0] else [("(document)", "")]


class RagChunks:
    """Emulates a generic docs RAG server (like docs-mcp-server): the page is
    split into ~1500 character chunks, a query returns the top 5 chunks."""
    name = "generic docs RAG (top-5 chunks à ~1500 chars)"

    def __init__(self, size=1500, k=5):
        self.k = k
        text = DEFAULT_SOURCE.read_text(encoding="utf-8")
        text = text[text.index("## Anlage"):]
        chunks, cur = [], ""
        for block in re.split(r"\n\s*\n", text):
            if len(cur) + len(block) > size and cur:
                chunks.append(cur)
                cur = ""
            cur += block + "\n\n"
        chunks.append(cur)
        self.chunks = chunks
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE VIRTUAL TABLE c USING fts5(t, tokenize='unicode61 remove_diacritics 2')")
        self.con.executemany("INSERT INTO c(rowid, t) VALUES (?,?)", enumerate(chunks))

    def schema(self):
        return ""  # not measured: depends on the server

    def query(self, q):
        terms = [w for w in db._words(q)]
        if not terms:
            return ""
        m = " OR ".join(f'"{w}"' for w in terms)
        rows = self.con.execute("SELECT rowid FROM c WHERE c MATCH ? ORDER BY bm25(c) LIMIT ?",
                                (m, self.k)).fetchall()
        return "\n---\n".join(self.chunks[r[0]] for r in rows)

    def run(self, t):
        if t["type"] == "find":
            return [("search", self.query(t["q"]))]
        if t["type"] == "price":
            return [("search", self.query(f"Nummer {t['code']}"))]
        if t["type"] == "check":
            a, b = t["codes"]
            return [("search", self.query(f"Nummer {a} neben Nummer {b} nicht berechnungsfähig"))]
        return [("search", self.query(t["q"]))]


class Tools:
    """Our tool server with a given profile and tool set."""

    def __init__(self, name, profile, toolset="terse", naive_get=False):
        self.name = name
        self.profile = profile
        self.toolset = toolset
        self.naive_get = naive_get

    def schema(self):
        ts = tools.TOOLS if self.toolset == "terse" else tools.VERBOSE_TOOLS
        return json.dumps(ts, ensure_ascii=False, separators=(",", ":"))

    def _get(self, code):
        if not self.naive_get:
            return db.get(code)
        # naive: dump the row, all own notes and all rules mentioning the code as JSON
        con = db.connect()
        r = dict(con.execute("SELECT * FROM ziffer WHERE code=?", (code,)).fetchone())
        r["fees"] = {f: db.fee(r["points"], f) for f in (1.0, *db.FACTORS[r["factor_class"]])}
        r["notes"] = [n[0] for n in con.execute(
            "SELECT DISTINCT n.text FROM note n JOIN note_ref f ON f.note_id=n.id WHERE f.code=?", (code,))]
        return json.dumps(r, ensure_ascii=False, indent=2)

    def run(self, t):
        p = self.profile
        if t["type"] == "find":
            return [("search", db.search(t["q"], profile=p))]
        if t["type"] == "price":
            if self.toolset == "verbose":
                return [("price", db.price(t["code"], t["factor"]))]
            return [("get", self._get(t["code"]))]
        if t["type"] == "check":
            return [("check", db.check(t["codes"]))]
        if t["type"] == "rule":
            calls = [("get", self._get(t["code"]))]
            if t["expect"] not in calls[0][1]:
                calls.append(("rules", db.rules(t["code"])))
            return calls
        raise ValueError(t["type"])


# ---------------------------------------------------------------- evaluation

def result_codes(text: str) -> list[str]:
    s = text.lstrip()
    if s.startswith("["):
        try:
            return [d["code"] for d in json.loads(s)]
        except json.JSONDecodeError:
            return []
    return [line.split("|", 1)[0] for line in text.splitlines() if "|" in line]


def chunk_has_code(text: str, code: str) -> bool:
    return re.search(rf"^\*    \*   {re.escape(code)}\s*$", text, re.M) is not None


def evaluate(approach, t, calls) -> tuple[bool, int | None]:
    out = "\n".join(c[1] for c in calls)
    if isinstance(approach, FullDoc):
        return True, None
    if t["type"] == "find":
        want = t.get("expect") or t.get("expect_any")
        if isinstance(approach, RagChunks):
            hits = [c for c in want if chunk_has_code(out, c)]
            ok = len(hits) == len(want) if "expect" in t else bool(hits)
            return ok, None
        codes = result_codes(out)
        ranks = [codes.index(c) + 1 for c in want if c in codes]
        ok = (len(ranks) == len(want)) if "expect" in t else bool(ranks)
        return ok, (min(ranks) if ranks else None)
    if t["type"] == "price":
        if isinstance(approach, RagChunks):  # the model would still have to compute the fee
            return chunk_has_code(out, t["code"]), None
        return (t["expect"] in out
                or t["expect"].replace(",", ".") in out.replace(" ", "")), None
    if t["type"] == "check":
        if isinstance(approach, RagChunks):
            a, b = t["codes"]
            sents = [s for s in re.split(r"(?<=\.)\s", out) if "nicht" in s
                     and re.search(rf"\b{a}\b", s) and re.search(rf"\b{b}\b", s)]
            return bool(sents) == t["conflict"], None
        return ("✗" in out) == t["conflict"], None
    if t["type"] == "rule":
        return t["expect"] in out, None
    raise ValueError(t["type"])


def run_approach(approach) -> dict:
    schema_toks = tok(approach.schema())
    res = {"name": approach.name, "schema": schema_toks, "tasks": {}}
    for split in ("dev", "holdout"):
        for t in TASKS[split]:
            calls = approach.run(t)
            ok, rank = evaluate(approach, t, calls)
            res["tasks"][t["id"]] = {
                "split": split, "type": t["type"], "ok": ok, "rank": rank,
                "calls": len(calls), "out": sum(tok(c[1]) for c in calls),
            }
    T = res["tasks"].values()
    ok = lambda s: sum(x["ok"] for x in T if x["split"] == s)
    n = lambda s: sum(1 for x in T if x["split"] == s)
    outs = [x["out"] for x in T]
    find_out = [x["out"] for x in T if x["type"] == "find"]
    res["summary"] = {
        "dev": f"{ok('dev')}/{n('dev')}",
        "holdout": f"{ok('holdout')}/{n('holdout')}",
        "avg_out": round(sum(outs) / len(outs)),
        "max_out": max(outs),
        "avg_find_out": round(sum(find_out) / len(find_out)),
        "session": schema_toks + sum(outs),  # all 40 tasks in one agent session
    }
    if isinstance(approach, FullDoc):
        res["summary"].update(avg_out=outs[0], max_out=outs[0], avg_find_out=outs[0],
                              session=outs[0])
    return res


# ------------------------------------------------------------- the iterations

P = db.Profile
ITERATIONS = [
    ("it0a", "Baseline: whole fee schedule in the prompt", FullDoc),
    ("it0b", "Baseline: generic docs RAG, top-5 chunks", RagChunks),
    ("it1", "Straightforward tool: verbose schema, JSON, k=10, full text + notes, BM25",
     lambda: Tools("naive tools", P(k=10, text_max=None, fmt="json", strategy="fts",
                                     synonyms="none", notes_in_search=True),
                   toolset="verbose", naive_get=True)),
    ("it2", "Line format 'code|points|text' instead of JSON, notes only via get",
     lambda: Tools("lines", P(k=10, text_max=None, fmt="lines", strategy="fts", synonyms="none"),
                   toolset="verbose")),
    ("it3", "Truncate text to 80 chars, k=5",
     lambda: Tools("truncate+k5", P(k=5, text_max=80, strategy="fts", synonyms="none"),
                   toolset="verbose")),
    ("it4", "Hybrid ranking: BM25 prefix + character-trigram TF-IDF (compounds)",
     lambda: Tools("hybrid", P(k=5, text_max=80, strategy="hybrid", synonyms="none"),
                   toolset="verbose")),
    ("it5", "Synonym file for colloquial terms (derived from dev failures only)",
     lambda: Tools("synonyms", P(k=5, text_max=80, strategy="hybrid", synonyms="dev"),
                   toolset="verbose")),
    ("it6", "Terse 4-tool schema, fees folded into goae_get",
     lambda: Tools("terse schema", P(k=5, text_max=80, strategy="hybrid", synonyms="dev"))),
    ("it7", "Probe: k=3, text 60 chars",
     lambda: Tools("k3", P(k=3, text_max=60, strategy="hybrid", synonyms="dev"))),
    ("it8", "Post-hoc: 6 synonyms for the failed test tasks (test tasks no longer unseen!)",
     lambda: Tools("post-hoc synonyms", P(k=5, text_max=80, strategy="hybrid", synonyms="all"))),
    ("final", "Default profile = it8 (k=5, 80 chars, hybrid, all synonyms, terse schema)",
     lambda: Tools("default", db.DEFAULT)),
]


def fill_simulation(profile) -> dict:
    """'Complete a table' with the 12 test find tasks, two ways."""
    rows = [t for t in TASKS["holdout"] if t["type"] == "find"]
    outs = [db.search(t["q"], profile=profile) for t in rows]
    schema = tok(json.dumps(tools.TOOLS, ensure_ascii=False, separators=(",", ":")))
    from goae.fill import PROMPT
    per_call = [tok(PROMPT.format(row=t["q"], candidates=o)) for t, o in zip(rows, outs)]
    return {
        "rows": len(rows),
        "agent_session_tool_tokens": schema + sum(tok(o) for o in outs),
        "pipeline_max_prompt": max(per_call),
        "pipeline_avg_prompt": round(sum(per_call) / len(per_call)),
    }


def write_stats(results: list[dict], fill: dict) -> None:
    hist = []
    if HISTORY.exists():
        hist = [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines() if l.strip()]
    L = []
    L.append("# Context statistics\n")
    L.append("Generated by `python bench/run.py` — do not edit by hand.\n")
    L.append(f"Tasks: {len(TASKS['dev'])} tuning tasks (`dev`, may be used to decide on changes) + "
             f"{len(TASKS['holdout'])} test tasks (`holdout`, only measured; not used for changes before it8), see [`bench/tasks.json`](bench/tasks.json). "
             "Tokens are estimated as the number of characters divided by four. *Schema* = tool definitions, re-sent with every request. "
             "*Output* = tool results needed to answer one task. *Session* = schema + all outputs "
             f"if one agent answered all {len(TASKS['dev']) + len(TASKS['holdout'])} tasks in a "
             "single conversation (what fills the context window).\n")
    L.append("| # | Change | Tuning ✓ | Test ✓ | Schema | Ø output/task | Ø search result | Max output | Session | Fits 32K? |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        s = r["summary"]
        L.append(f"| {r['id']} | {r['label'].replace('|', chr(92) + '|')} | {s['dev']} | {s['holdout']} | "
                 f"{r['schema'] or '–'} | {s['avg_out']:,} | {s['avg_find_out']:,} | {s['max_out']:,} | "
                 f"{s['session']:,} | {'yes' if s['session'] < CTX_LIMIT else '**no**'} |")
    L.append("")
    by = {r["id"]: r["summary"] for r in results}
    if {"it0b", "it1", "final", "it7"} <= by.keys():
        f, rag, naive = by["final"], by["it0b"], by["it1"]
        L.append("**Takeaways**\n")
        L.append(f"* One search costs ~{f['avg_find_out']} tokens instead of ~{rag['avg_find_out']:,} "
                 f"(RAG chunks) or ~{naive['avg_find_out']} (straightforward JSON tool).")
        L.append(f"* All 40 tasks in one conversation: {f['session']:,} tokens — "
                 f"{rag['session'] / f['session']:.0f}x less than RAG, {naive['session'] / f['session']:.0f}x less "
                 "than the straightforward tool. (The agent's own system prompt comes on top of this.)")
        L.append("* Output format and truncation (it1→it3) save the most tokens; ranking and synonyms "
                 "(it4→it5) win back the recall that truncation and a small k cost.")
        L.append(f"* Before it8 the test tasks scored {by['it5']['holdout']} — the misses were colloquial terms "
                 "(Blutzucker, MRT, Lungenfunktion, …). it8 shows the fix: one line per term in "
                 "`data/synonyms.tsv`. Expect the same with new data: extend the synonym file.")
        L.append(f"* k=3 (it7) would save another ~{f['avg_find_out'] - by['it7']['avg_find_out']} tokens per "
                 "search, but two test-task hits already sit at rank 3, right at the edge; k=5 keeps "
                 "headroom for unseen wording.\n")
    L.append("Notes on the baselines: *it0a* is always 'correct' because everything is in the prompt, "
             "but it does not fit a 32K (or 64K) window at all. *it0b* counts a hit when the right "
             "entry is somewhere in the returned chunks; the model would still have to read and "
             "compute (fees, exclusions) itself. Schema size of a real docs server is not measured.\n")

    final = next(r for r in results if r["id"] == "final")
    L.append("## Per task (final profile)\n")
    L.append("| Task | Group | Type | ✓ | Rank | Calls | Output tokens |")
    L.append("|---|---|---|---|---|---|---|")
    for tid, x in final["tasks"].items():
        L.append(f"| {tid} | {'tuning' if x['split'] == 'dev' else 'test'} | {x['type']} | {'✓' if x['ok'] else '✗'} | "
                 f"{x['rank'] or ''} | {x['calls']} | {x['out']} |")
    L.append("")
    L.append("## Filling a table (12 test-task rows)\n")
    L.append(f"* **Agent session** (every search result stays in the conversation): "
             f"{fill['agent_session_tool_tokens']:,} tokens of schema + tool output, growing linearly with rows.")
    L.append(f"* **Pipeline** (`python -m goae fill`, one fresh LLM call per row): "
             f"Ø {fill['pipeline_avg_prompt']} / max {fill['pipeline_max_prompt']} tokens per call — "
             "constant, independent of table length.\n")
    if hist:
        L.append("## Loop history\n")
        L.append("Each entry is one turn of the optimisation loop, recorded when it was run "
                 "(`--log`). Later code changes are not retro-applied to older entries.\n")
        L.append("| When | Iteration | Note | Tuning ✓ | Test ✓ | Ø search result | Session |")
        L.append("|---|---|---|---|---|---|---|")
        for h in hist:
            L.append(f"| {h['when']} | {h['id']} | {h['note']} | {h['dev']} | {h['holdout']} | "
                     f"{h['avg_find_out']} | {h['session']:,} |")
        L.append("")
    (ROOT / "CONTEXT_STATS.md").write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--log")
    args = ap.parse_args()
    results = []
    for iid, label, factory in ITERATIONS:
        if args.only and iid != args.only:
            continue
        r = run_approach(factory())
        r.update(id=iid, label=label)
        results.append(r)
        s = r["summary"]
        print(f"{iid:6} dev {s['dev']:>6} holdout {s['holdout']:>6} schema {r['schema']:>5} "
              f"avg {s['avg_out']:>6} find {s['avg_find_out']:>6} session {s['session']:>7}  {label}")
        if args.only:
            for tid, x in r["tasks"].items():
                if not x["ok"]:
                    print(f"   ✗ {tid} ({x['split']}, {x['type']})")
        if args.log and (args.only or iid == "final"):
            with HISTORY.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"when": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    "id": iid, "note": args.log, **s}, ensure_ascii=False) + "\n")
    if not args.only:
        fill = fill_simulation(db.DEFAULT)
        (ROOT / "bench" / "results.json").write_text(
            json.dumps({"iterations": results, "fill": fill}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        write_stats(results, fill)
        print("fill:", fill)


if __name__ == "__main__":
    main()
