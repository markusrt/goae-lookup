"""Parse the official GOÄ text (Markdown mirror of gesetze-im-internet.de) into SQLite.

The fee schedule (Anlage) is a nested Markdown list where every table row
starts with ``*    *`` and every cell is a ``*`` bullet. Rows are classified as

* fee entries   – first non-empty cell is a code (``250``, ``250a``, ``K 1``)
                  followed by the service text and a point value,
* headings      – bold cells (``**C.**``, ``**IV.**``),
* notes         – everything else; attached to the preceding fee entry.

Free text outside of rows ("Allgemeine Bestimmungen") is stored as section
rules. Section and subsection are assigned from the overview table at the
start of the Anlage, which lists number ranges for every (sub)section.

Only the standard library is used.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "data" / "source" / "goae_1982.md"
DEFAULT_DB = ROOT / "data" / "goae.db"

PUNKTWERT_EUR = 0.0582873  # § 5 Abs. 1 GOÄ: 5,82873 Cent

CODE_RE = re.compile(r"^(\d{1,4}[a-z]?(?:\.H\d?)?|[A-K](?: ?\d)?)$")  # 250a, 3541.H, 3562.H1, K 1
ROMAN_RE = re.compile(r"^(?:[IVX]+)\.")
LETTER_SECTION_RE = re.compile(r"^([A-P])\.\s*(.*)$")
NUM_TOKEN_RE = re.compile(r"\b(\d{1,4}[a-z]?)\b")
_SEP = r"(?:\s*(?:,|und/oder|und|oder|bis|sowie|/)\s*)"
NUMBER_LIST_RE = re.compile(
    rf"(?:Nummern?|Nrn?\.|Leistungsnummern?)\s+((?:\d{{1,4}}[a-z]?{_SEP}?)+)")
LETTER_LIST_RE = re.compile(
    rf"Buchstaben?\s+((?:(?:K ?[12]|[A-J])(?![\w]){_SEP}?)+)")


@dataclass
class Entry:
    code: str
    text: str
    points: int | None
    seq: int
    notes: list[str] = field(default_factory=list)
    section: str = ""
    section_title: str = ""
    subsection: str = ""
    subsection_title: str = ""


def code_key(code: str) -> tuple[int, str]:
    """Numeric sort key for codes like '250a'; letters sort as -1."""
    m = re.match(r"^(\d+)([a-z]?(?:\.H\d?)?)$", code)
    if m:
        return int(m.group(1)), m.group(2)
    return -1, code


def _clean(text: str) -> str:
    text = text.replace("**", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _split_rows(lines: list[str]):
    """Yield ('row', [cells]) or ('text', paragraph) in document order."""
    row: list[str] | None = None
    cell: list[str] | None = None
    para: list[str] = []

    def flush_para():
        nonlocal para
        if para:
            text = _clean(" ".join(para))
            para = []
            if text:
                return ("text", text)
        return None

    def flush_row():
        nonlocal row, cell
        if row is None:
            return None
        if cell is not None:
            row.append(_clean(" ".join(cell)))
        out = ("row", row)
        row, cell = None, None
        return out

    for line in lines:
        if line.startswith("*    *"):
            for item in (flush_para(), flush_row()):
                if item:
                    yield item
            row = []
            rest = line[len("*    *"):]
            cell = [rest.strip()] if rest.strip() else None
            if cell is None:
                row.append("")  # empty first cell
            continue
        if row is not None:
            m = re.match(r"^\s{4}\*(\s+(.*))?$", line)
            if m:  # new cell
                if cell is not None:
                    row.append(_clean(" ".join(cell)))
                cell = [m.group(2) or ""]
                continue
            if line.startswith("        ") and cell is not None:
                cell.append(line.strip())
                continue
            if not line.strip():
                continue
            # anything else ends the row
            item = flush_row()
            if item:
                yield item
        if not line.strip():
            item = flush_para()
            if item:
                yield item
            continue
        para.append(line.strip())
    for item in (flush_para(), flush_row()):
        if item:
            yield item


def _parse_range(label: str):
    """'200 bis 449' -> ('num', 200, 449); 'A bis K 1' -> ('letter', 'A', 'K 1')."""
    m = re.match(r"^(\d+)\s+bis\s+(\d+)$", label)
    if m:
        return ("num", int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d+)$", label)
    if m:
        return ("num", int(m.group(1)), int(m.group(1)))
    m = re.match(r"^([A-K](?: ?\d)?)\s+bis\s+([A-K](?: ?\d)?)$", label)
    if m:
        return ("letter", m.group(1), m.group(2))
    return None


def parse(source: Path = DEFAULT_SOURCE):
    lines = source.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("## Anlage"))
    body = lines[start:]

    overview: list[dict] = []  # ordered (sub)sections with ranges
    entries: list[Entry] = []
    rules: list[dict] = []  # free text rules
    pending_text: list[str] = []
    a_list_text = ""
    in_overview = True
    catalog = None  # (header text, points) of a catalog block
    seq = 0

    for kind, payload in _split_rows(body):
        if kind == "text":
            if payload.startswith("A. Gebühren in besonderen Fällen"):
                in_overview = False
            if payload.startswith("Für die nachfolgend genannten Leistungen dürfen"):
                a_list_text = payload
            pending_text.append(payload)
            continue

        cells = [c for c in payload]
        nonempty = [c for c in cells if c]
        if not nonempty:
            continue

        if in_overview:
            # rows: range | number | title | page
            if len(nonempty) >= 3 and _parse_range(nonempty[0]):
                overview.append({
                    "range": _parse_range(nonempty[0]),
                    "label": nonempty[1].rstrip("."),
                    "title": nonempty[2],
                })
            continue

        first = nonempty[0]
        if first.startswith("**") or cells[0].startswith("**"):
            catalog = None
            continue  # heading cell; titles come from the overview
        if first in ("Katalog", "Nummer"):
            continue

        is_code = bool(CODE_RE.match(first))
        if is_code and len(nonempty) >= 2 and not nonempty[1].isdigit():
            points = int(nonempty[2]) if len(nonempty) >= 3 and nonempty[2].isdigit() else None
            text = nonempty[1]
            if points is None and catalog:
                # catalog item: inherits points of the catalog header row
                points = catalog[1]
                text = f"{text} ({catalog[0]})"
            elif points is not None:
                catalog = None
            seq += 1
            e = Entry(code=first, text=text, points=points, seq=seq)
            entries.append(e)
            for t in pending_text:
                rules.append({"text": t, "next_seq": seq})
            pending_text = []
            continue

        if not is_code and len(nonempty) >= 2 and nonempty[1].isdigit():
            if cells[0] == "" and entries and entries[-1].points is None:
                # continuation of a wrapped entry ("... bis zum vollendeten" / "4. Lebensjahr")
                entries[-1].text = f"{entries[-1].text} {first}"
                entries[-1].points = int(nonempty[1])
            else:
                catalog = (first, int(nonempty[1]))  # header of a catalog block
            continue

        if re.match(r"^[A-P]\.$", first) or ROMAN_RE.match(first):
            continue
        if entries:
            entries[-1].notes.append(_clean(" ".join(nonempty)))

    for t in pending_text:
        rules.append({"text": t, "next_seq": None})

    assign_sections(entries, overview)
    by_seq = {e.seq: e for e in entries}
    resolved = []
    for r in rules:
        text = r["text"]
        if (text in ("Allgemeine Bestimmungen",) or re.match(r"^[A-P]\. ", text)
                or text.startswith(("#", "(Fundstelle", "Für die nachfolgend genannten"))):
            continue
        nxt = by_seq.get(r["next_seq"]) if r["next_seq"] else None
        resolved.append({"text": text, "section": nxt.section if nxt else "",
                         "subsection": nxt.subsection if nxt else ""})
    return entries, resolved, overview, a_list_text


def base_code(code: str) -> str:
    """Numeric code used for section lookup: 'A3754' (analog) -> '3754', '3832-3854' -> '3832'."""
    return re.sub(r"^A(?=\d)", "", code).split("-")[0]


def assign_sections(entries: list[Entry], overview: list[dict]) -> None:
    # main sections are labelled with a single letter, subsections with roman numerals
    sections, subs = [], []
    current_main = None

    def contained(inner, outer):
        return (outer is not None and inner[0] == "num" and outer[0] == "num"
                and outer[1] <= inner[1] and inner[2] <= outer[2])

    for item in overview:
        # 'I' and 'V' are both a section letter and a roman numeral: a label is a
        # main section unless its range lies inside the current main section.
        is_letter = re.match(r"^[A-P]$", item["label"]) is not None
        inside = current_main is not None and (
            contained(item["range"], current_main["range"]) or item["range"][0] == "letter")
        if is_letter and not inside:
            current_main = item
            sections.append(item)
        else:
            subs.append({**item, "main": current_main})

    def find(code: str, prev_numeric: int):
        k, _ = code_key(base_code(code))
        main = sub = None
        if k >= 0:
            for s in sections:
                r = s["range"]
                if r[0] == "num" and r[1] <= k <= r[2]:
                    main = s
            for s in subs:
                r = s["range"]
                if r[0] == "num" and r[1] <= k <= r[2] and s["main"] is main:
                    sub = s
        else:
            # letter codes (surcharges): the letter subsection following the
            # subsection of the previous numeric code
            for s in sections:
                r = s["range"]
                if r[0] == "num" and r[1] <= prev_numeric <= r[2]:
                    main = s
            letter_subs = [s for s in subs if s["range"][0] == "letter" and s["main"] is main]
            prev_sub_idx = None
            for i, s in enumerate(subs):
                r = s["range"]
                if r[0] == "num" and r[1] <= prev_numeric <= r[2] and s["main"] is main:
                    prev_sub_idx = i
            for s in letter_subs:
                if prev_sub_idx is not None and subs.index(s) > prev_sub_idx:
                    sub = s
                    break
            if sub is None and letter_subs:
                sub = letter_subs[-1]
        return main, sub

    prev_numeric = 0
    for e in entries:
        main, sub = find(e.code, prev_numeric)
        k, _ = code_key(base_code(e.code))
        if k >= 0:
            prev_numeric = k
        if main:
            e.section, e.section_title = main["label"], main["title"]
        if sub:
            e.subsection, e.subsection_title = sub["label"], sub["title"]


def expand_numbers(text: str, existing: list[str]) -> set[str]:
    """Return all codes mentioned in a rule text, expanding 'X bis Y' ranges.

    Only numbers that exist as codes are returned, so amounts like
    '100 v.H.' or '10 Minuten' are mostly filtered out.
    """
    existing_set = set(existing)
    numeric = sorted((code_key(c), c) for c in existing if code_key(c)[0] >= 0)
    found: set[str] = set()
    # surcharge letters: "Buchstaben A bis D sowie K 1", "Zuschlag nach Buchstabe E"
    for m in LETTER_LIST_RE.finditer(text):
        span = m.group(1)
        for k in re.findall(r"\bK ?([12])\b", span):
            found.add(f"K {k}")
        span = re.sub(r"\bK ?[12]\b", "", span)
        for lo, hi in re.findall(r"\b([A-J])\s+bis\s+([A-J])\b", span):
            found |= {chr(x) for x in range(ord(lo), ord(hi) + 1)}
        found |= set(re.findall(r"\b([A-J])\b", span))
    # numeric codes only inside "Nummer(n) <list>" spans, so that durations,
    # ages or percentages ("4 bis 12 Teilnehmern", "100 v.H.") are ignored
    for m in NUMBER_LIST_RE.finditer(text):
        span = m.group(1)
        for a, b in re.findall(r"(\d{1,4}[a-z]?)\s+bis\s+(\d{1,4}[a-z]?)", span):
            lo, hi = code_key(a), code_key(b)
            for key, c in numeric:
                if lo <= key <= hi:
                    found.add(c)
        for tok in NUM_TOKEN_RE.findall(span):
            if tok in existing_set:
                found.add(tok)
    return {c for c in found if c in existing_set}


def factor_class(e: Entry, a_list: set[str]) -> str:
    """§ 5 Abs. 3/4 GOÄ: technical (1,8/2,5), lab (1,15/1,3) or personal (2,3/3,5)."""
    if e.section == "M" or e.code == "437":
        return "lab"
    if e.section in ("E", "O") or e.code in a_list:
        return "tech"
    return "personal"


SCHEMA = """
CREATE TABLE ziffer (
  code TEXT PRIMARY KEY,
  sort INTEGER, suffix TEXT, seq INTEGER,
  text TEXT NOT NULL,
  points INTEGER,
  section TEXT, section_title TEXT,
  subsection TEXT, subsection_title TEXT,
  factor_class TEXT
);
CREATE TABLE note (
  id INTEGER PRIMARY KEY,
  code TEXT,            -- owning code (NULL for section rules)
  section TEXT, subsection TEXT,
  kind TEXT,            -- 'note' (under an entry) or 'rule' (Allgemeine Bestimmungen)
  text TEXT NOT NULL
);
CREATE TABLE note_ref (note_id INTEGER, code TEXT);
CREATE INDEX note_ref_code ON note_ref(code);
CREATE VIRTUAL TABLE ziffer_fts USING fts5(code UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE ziffer_tri USING fts5(code UNINDEXED, text, tokenize='trigram');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def write_db(db_path: Path, entries: list[Entry], rules: list[dict], a_list_text: str,
             meta: dict) -> dict:
    """Write entries (with their notes) and general rules into a fresh SQLite file."""
    codes = [e.code for e in entries]
    a_list = expand_numbers(a_list_text, codes)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    seen = set()
    for e in entries:
        if e.code in seen:  # keep first occurrence
            continue
        seen.add(e.code)
        k, suf = code_key(base_code(e.code))
        con.execute(
            "INSERT INTO ziffer VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (e.code, k, suf, e.seq, e.text, e.points, e.section, e.section_title,
             e.subsection, e.subsection_title, factor_class(e, a_list)),
        )
        con.execute("INSERT INTO ziffer_fts VALUES (?,?)", (e.code, e.text))
        con.execute("INSERT INTO ziffer_tri VALUES (?,?)", (e.code, e.text))
        for n in e.notes:
            cur = con.execute(
                "INSERT INTO note(code, section, subsection, kind, text) VALUES (?,?,?,?,?)",
                (e.code, e.section, e.subsection, "note", n))
            refs = expand_numbers(n, codes) | {e.code}
            con.executemany("INSERT INTO note_ref VALUES (?,?)", [(cur.lastrowid, c) for c in refs])
    for r in rules:
        cur = con.execute(
            "INSERT INTO note(code, section, subsection, kind, text) VALUES (NULL,?,?,?,?)",
            (r["section"], r["subsection"], "rule", r["text"]))
        refs = expand_numbers(r["text"], codes)
        con.executemany("INSERT INTO note_ref VALUES (?,?)", [(cur.lastrowid, c) for c in refs])
    meta = {**meta, "punktwert_eur": str(PUNKTWERT_EUR), "entries": str(len(seen))}
    con.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    con.commit()
    con.close()
    tmp.replace(db_path)
    return {"db": str(db_path), "entries": len(seen), "rules": len(rules)}


def build(db_path: Path = DEFAULT_DB, source: Path = DEFAULT_SOURCE) -> dict:
    """Build the database from the official GOÄ text."""
    entries, rules, _overview, a_list_text = parse(source)
    return write_db(db_path, entries, rules, a_list_text, {"source": source.name})


if __name__ == "__main__":
    print(build())
