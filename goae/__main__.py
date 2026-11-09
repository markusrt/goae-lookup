"""Command line interface. Any agent that can run shell commands can use it.

  python -m goae search "Blutabnahme aus der Vene"
  python -m goae get 250
  python -m goae price 250 --factor 2.5
  python -m goae check 1 3 250
  python -m goae rules 5
  python -m goae fill table.csv -o table.out.csv [--llm http://localhost:11434/v1 --model qwen2.5:7b]
  python -m goae fill table.csv --llm-cmd "claude -p"   # any CLI as the LLM, one call per row
  python -m goae next table.csv / set table.csv 3=651 4=375   # agent works through a table in batches
  python -m goae build          # (re)build data/goae.db from data/source/goae_1982.md
  python -m goae build --source abrechnungsstelle   # alternative DB from abrechnungsstelle.com
  python -m goae --db data/goae-abrechnungsstelle.db search "…"   # or set GOAE_DB
  python -m goae mcp            # MCP server on stdio
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="goae", description="Context-minimal GOÄ lookup")
    ap.add_argument("--db", help="database file (default: $GOAE_DB or data/goae.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="find codes for a service description")
    s.add_argument("q", nargs="+")
    s.add_argument("-k", type=int, default=None, help="max results (default 5)")
    s.add_argument("--section", help="restrict to section letter A-P")

    g = sub.add_parser("get", help="details of one code")
    g.add_argument("code", nargs="+")

    p = sub.add_parser("price", help="fee for a code and factor")
    p.add_argument("code")
    p.add_argument("--factor", type=float)

    c = sub.add_parser("check", help="exclusion rules between codes")
    c.add_argument("codes", nargs="+")

    r = sub.add_parser("rules", help="all rules mentioning a code")
    r.add_argument("code")
    r.add_argument("--limit", type=int, default=8)

    f = sub.add_parser("fill", help="complete a CSV table row by row")
    f.add_argument("csv", type=Path)
    f.add_argument("-o", "--out", type=Path)
    f.add_argument("--col", help="column with the service description (default: first)")
    f.add_argument("-k", type=int, default=5)
    f.add_argument("--llm", help="OpenAI-compatible base URL, e.g. http://localhost:11434/v1")
    f.add_argument("--llm-cmd", help="CLI that reads the prompt on stdin, e.g. \"claude -p\" or \"ollama run qwen2.5\"")
    f.add_argument("--model", default="qwen2.5:7b-instruct")
    f.add_argument("--min-confidence", type=float, default=0.5)

    n = sub.add_parser("next", help="agent mode: show the next open rows of a CSV with candidates")
    n.add_argument("csv", type=Path)
    n.add_argument("--col", help="column with the service description (default: first)")
    n.add_argument("-n", type=int, default=10, help="rows per batch (default 10)")
    n.add_argument("-k", type=int, default=3, help="candidates per row (default 3)")

    st = sub.add_parser("set", help="agent mode: save ROW=CODE pairs ('?' = nothing fits), show next rows")
    st.add_argument("csv", type=Path)
    st.add_argument("pairs", nargs="+", help="e.g. 3=651 4=375 5=?")
    st.add_argument("--col")
    st.add_argument("-k", type=int, default=3)

    b = sub.add_parser("build", help="(re)build a SQLite database")
    b.add_argument("--source", choices=["official", "abrechnungsstelle"], default="official",
                   help="official GOÄ text (default) or abrechnungsstelle.com (downloads 14 pages)")
    b.add_argument("--refresh", action="store_true", help="re-download cached pages")
    sub.add_parser("mcp", help="run the MCP server on stdio")

    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # UTF-8 even on Windows consoles and pipes
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if a.db:
        os.environ["GOAE_DB"] = a.db
    if a.cmd == "search":
        print(db.search(" ".join(a.q), k=a.k, section=a.section))
    elif a.cmd == "get":
        # " ".join handles 'K 1' passed without quotes
        codes = [" ".join(a.code)] if len(a.code) == 2 and a.code[0].upper() == "K" else a.code
        print("\n\n".join(db.get(x) for x in codes))
    elif a.cmd == "price":
        print(db.price(a.code, a.factor))
    elif a.cmd == "check":
        print(db.check(a.codes))
    elif a.cmd == "rules":
        print(db.rules(a.code, limit=a.limit))
    elif a.cmd == "fill":
        from .fill import fill
        print(fill(a.csv, a.out, a.col, a.k, a.llm, a.model, a.min_confidence, a.llm_cmd))
    elif a.cmd == "next":
        from .fill import next_rows
        print(next_rows(a.csv, a.col, a.k, a.n))
    elif a.cmd == "set":
        from .fill import set_rows
        print(set_rows(a.csv, a.pairs, a.col, a.k))
    elif a.cmd == "build":
        if a.source == "abrechnungsstelle":
            from .sources import abrechnungsstelle as src
            print(src.build(Path(a.db) if a.db else src.DEFAULT_DB, a.refresh))
        else:
            from .ingest import DEFAULT_DB, build
            print(build(Path(a.db) if a.db else DEFAULT_DB))
    elif a.cmd == "mcp":
        from .mcp_server import main as serve
        serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
