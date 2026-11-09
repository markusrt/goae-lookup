"""Ingest example: build the database from https://abrechnungsstelle.com/goae/ziffern/.

The site lists all GOÄ codes on 14 range pages (``/goae/1-498/`` …). Each code
is one grid item with:

* ``GOÄ-Ziffer: <code>``                     (label + value)
* ``<h3><a>GOÄ <code>: <title></a></h3>``    (service text)
* ``<value>`` followed by the label ``Punktzahl``
* fee values (ignored, we compute fees ourselves)
* optional ``<p>`` paragraphs with the billing notes
* optional ``Ausschlussziffern:`` followed by links ``GOÄ <code>``

The site's content is NOT stored in this repository. Running this module
downloads the 14 pages once (politely: robots.txt is checked, one request
every few seconds, raw HTML cached under ``data/cache/``, which is git-ignored)
and writes a separate database, ``data/goae-abrechnungsstelle.db``.
Check the site's terms before using the data beyond your own purposes.

Section structure, § 5 factor classes and the general rules ("Allgemeine
Bestimmungen") still come from the official text in ``data/source/``.

Only the standard library is used. If the site changes its markup, run
``python -m goae.sources.abrechnungsstelle --debug 1-498`` and adapt
``ItemParser`` — it is the only site-specific part.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

from ..ingest import DEFAULT_SOURCE, ROOT, Entry, assign_sections, parse, write_db

BASE = "https://abrechnungsstelle.com"
PAGES = [
    "1-498", "500-887", "1001-1386", "1400-1860", "2000-2358", "2380-2732", "2750-3097",
    "3120-3321", "3500-3615", "3630h-4014", "4020-4469", "4500-4787", "4800-5380", "5400-6018",
]
CACHE = ROOT / "data" / "cache" / "abrechnungsstelle"
DEFAULT_DB = ROOT / "data" / "goae-abrechnungsstelle.db"
USER_AGENT = "goae-lookup/0.1 (+https://github.com/markusrt/goae-lookup)"
DELAY_S = 3.0


# ------------------------------------------------------------------ download

def fetch_pages(refresh: bool = False, pages: list[str] = PAGES) -> list[Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    robots = urllib.robotparser.RobotFileParser(BASE + "/robots.txt")
    robots.read()
    paths = []
    for i, slug in enumerate(pages):
        url = f"{BASE}/goae/{slug}/"
        path = CACHE / f"{slug}.html"
        paths.append(path)
        if path.exists() and not refresh:
            continue
        if not robots.can_fetch(USER_AGENT, url):
            raise SystemExit(f"robots.txt disallows {url}")
        if i:
            time.sleep(DELAY_S)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            path.write_bytes(r.read())
        print(f"fetched {url} ({path.stat().st_size // 1024} KB)", file=sys.stderr)
    return paths


# ------------------------------------------------------------------- parsing

class ItemParser(HTMLParser):
    """Turns a range page into one token list per grid item.

    Tokens are (kind, text): 'text', 'h3', 'p' (a whole paragraph) or 'a'
    (link text). Matching is done on visible text and the stable structure
    (grid item, h3, p, a), not on the theme's generated CSS class names.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[list[tuple[str, str]]] = []
        self._div_depth = 0
        self._item_depth: int | None = None
        self._in = {"h3": 0, "p": 0, "a": 0, "script": 0, "style": 0}
        self._buf = {"h3": [], "p": []}

    @property
    def _cur(self):
        return self.items[-1] if self._item_depth is not None else None

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            cls = dict(attrs).get("class") or ""
            if self._item_depth is None and "w-grid-item-h" in cls.split():
                self._item_depth = self._div_depth
                self.items.append([])
            self._div_depth += 1
        elif tag in self._in:
            self._in[tag] += 1
            if tag in self._buf:
                self._buf[tag] = []

    def handle_endtag(self, tag):
        if tag == "div":
            self._div_depth -= 1
            if self._item_depth is not None and self._div_depth == self._item_depth:
                self._item_depth = None
        elif tag in self._in and self._in[tag]:
            self._in[tag] -= 1
            if tag in self._buf and self._cur is not None:
                text = re.sub(r"\s+", " ", "".join(self._buf[tag])).strip()
                if text:
                    self._cur.append((tag, text))

    def handle_data(self, data):
        if self._cur is None or self._in["script"] or self._in["style"]:
            return
        for tag in ("h3", "p"):
            if self._in[tag]:
                self._buf[tag].append(data)
                return
        text = data.strip()
        if text:
            self._cur.append(("a" if self._in["a"] else "text", text))


def site_code(raw: str) -> str:
    """'K1' -> 'K 1'; everything else as printed ('250a', '3630.H', 'A3754')."""
    raw = raw.strip()
    m = re.fullmatch(r"K\s?(\d)", raw)
    return f"K {m.group(1)}" if m else raw


def _exclusion_sentence(code: str, excl: list[str]) -> str:
    """Phrase the site's exclusion list like the official notes, so that
    `goae check` evaluates it with the same rule parser."""
    nums = [c for c in excl if re.match(r"^\d", c)]
    letters = [c for c in excl if not re.match(r"^\d", c)]
    parts = []
    if nums:
        parts.append("den Leistungen nach den Nummern " + ", ".join(nums))
    if letters:
        parts.append("den Zuschlägen nach den Buchstaben " + ", ".join(letters))
    subject = (f"Der Zuschlag nach Buchstabe {code}" if not re.match(r"^\d", code)
               else f"Die Leistung nach Nummer {code}")
    return f"{subject} ist neben {' sowie '.join(parts)} nicht berechnungsfähig (Ausschlussziffern laut Quelle)."


def parse_item(tokens: list[tuple[str, str]]) -> dict | None:
    code = title = None
    points = None
    notes, excl = [], []
    in_excl = False
    for i, (kind, text) in enumerate(tokens):
        if text.startswith("GOÄ-Ziffer:"):
            rest = text.split(":", 1)[1].strip()
            code = rest or (tokens[i + 1][1] if i + 1 < len(tokens) else None)
        elif kind == "h3":
            title = re.sub(r"^GOÄ\s+[^:]+:\s*", "", text)
        elif text == "Punktzahl" and i and re.fullmatch(r"\d+", tokens[i - 1][1]):
            points = int(tokens[i - 1][1])
        elif text.startswith("Ausschlussziffern"):
            in_excl = True
        elif kind == "a" and in_excl:
            m = re.fullmatch(r"GOÄ\s+(\S+)", text)
            if m:
                excl.append(site_code(m.group(1)))
        elif kind == "p":
            notes.append(text)
    if not code or not title:
        return None
    code = site_code(code)
    title = title.replace("–", "-")
    if excl:
        notes.append(_exclusion_sentence(code, excl))
    return {"code": code, "text": title, "points": points, "notes": notes}


def parse_pages(paths: list[Path]) -> list[Entry]:
    entries, seq = [], 0
    for path in paths:
        p = ItemParser()
        p.feed(path.read_text(encoding="utf-8", errors="replace"))
        for tokens in p.items:
            item = parse_item(tokens)
            if item:
                seq += 1
                entries.append(Entry(code=item["code"], text=item["text"], points=item["points"],
                                     seq=seq, notes=item["notes"]))
    return entries


def build(db_path: Path = DEFAULT_DB, refresh: bool = False) -> dict:
    entries = parse_pages(fetch_pages(refresh))
    if not entries:
        raise SystemExit("no entries parsed - did the site's markup change? Try --debug.")
    _official, rules, overview, a_list_text = parse(DEFAULT_SOURCE)
    assign_sections(entries, overview)
    return write_db(db_path, entries, rules, a_list_text,
                    {"source": f"{BASE}/goae/ziffern/ (cached {CACHE.relative_to(ROOT)})"})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--refresh", action="store_true", help="download the pages again")
    ap.add_argument("--debug", metavar="PAGE", help="print the tokens of the first items of one page")
    a = ap.parse_args(argv)
    if a.debug:
        path = fetch_pages(False, [a.debug])[0]
        p = ItemParser()
        p.feed(path.read_text(encoding="utf-8", errors="replace"))
        for tokens in p.items[:3]:
            print(tokens[:40], "\n->", parse_item(tokens), "\n")
        return
    print(build(a.db, a.refresh))


if __name__ == "__main__":
    main()
