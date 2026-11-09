"""Command line interface. Any agent that can run shell commands can use it.

  python -m goae search "Blutabnahme aus der Vene"
  python -m goae get 250
  python -m goae price 250 --factor 2.5
  python -m goae check 1 3 250
  python -m goae rules 5
  python -m goae fill table.csv -o table.out.csv [--llm http://localhost:11434/v1 --model qwen2.5:7b]
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
    f.add_argument("--model", default="qwen2.5:7b-instruct")
    f.add_argument("--min-confidence", type=float, default=0.5)

    b = sub.add_parser("build", help="(re)build a SQLite database")
    b.add_argument("--source", choices=["official", "abrechnungsstelle"], default="official",
                   help="official GOÄ text (default) or abrechnungsstelle.com (downloads 14 pages)")
    b.add_argument("--refresh", action="store_true", help="re-download cached pages")
    sub.add_parser("mcp", help="run the MCP server on stdio")

    a = ap.parse_args(argv)
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
        out = a.out or a.csv.with_name(a.csv.stem + ".goae.csv")
        fill(a.csv, out, a.col, a.k, a.llm, a.model, a.min_confidence)
        print(out)
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
