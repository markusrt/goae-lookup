"""Query layer: every function returns a *small* plain-text answer.

The design rule is "an agent should never need more than one screen of text
per question". Large results are cut, long texts are truncated, and details
are only shown when asked for (progressive disclosure: search -> get -> rules).
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .ingest import DEFAULT_DB, PUNKTWERT_EUR, build, code_key

ROOT = Path(__file__).resolve().parent.parent
SYNONYMS = ROOT / "data" / "synonyms.tsv"

FACTORS = {  # § 5 GOÄ: (threshold factor, maximum factor)
    "personal": (2.3, 3.5),
    "tech": (1.8, 2.5),
    "lab": (1.15, 1.3),
}

STOPWORDS = set("""
aber alle als also am an auch auf aus bei beim bis da das dass dem den der des die
durch ein eine einem einen einer eines es für ggf im in ist je mit nach nicht oder
ohne pro so um und uns vom von vor was welche welcher welches wie wird zu zum zur
ziffer ziffern goä goae nummer nr leistung leistungen gebühr gebührenziffer
mindestens
""".split())


@dataclass
class Profile:
    """Knobs the benchmark loop turns. The defaults are the tuned result."""
    k: int = 5
    text_max: int | None = 80
    fmt: str = "lines"          # 'lines' or 'json'
    strategy: str = "hybrid"    # 'fts', 'ngram' or 'hybrid'
    synonyms: str = "all"       # 'none', 'dev' (entries above the post-hoc marker) or 'all'
    notes_in_search: bool = False


DEFAULT = Profile()


# --------------------------------------------------------------------------- db

@lru_cache(maxsize=4)
def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB
    if not path.exists():
        build(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _all_rows(con):
    return con.execute("SELECT * FROM ziffer ORDER BY seq").fetchall()


def normalize_code(code: str) -> str:
    code = code.strip().replace("Nr.", "").replace("nr.", "").strip()
    m = re.match(r"^([A-Ka-k])\s*(\d)?$", code)
    if m:
        return m.group(1).upper() + (f" {m.group(2)}" if m.group(2) else "")
    return code.lower()


# ------------------------------------------------------------------- formatting

def _trunc(text: str, n: int | None) -> str:
    if n is None or len(text) <= n:
        return text
    cut = text[: n - 1]
    if " " in cut[n // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,-(") + "…"


def eur(amount: float) -> str:
    return f"{amount:.2f}".replace(".", ",")


def fee(points: int, factor: float) -> float:
    # round half up to the cent (§ 5 Abs. 1 Satz 4 GOÄ)
    return math.floor(points * PUNKTWERT_EUR * factor * 100 + 0.5) / 100


# ----------------------------------------------------------------------- search

def _fold(s: str) -> str:
    s = s.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return s


def _words(q: str) -> list[str]:
    return [w for w in re.findall(r"[\wäöüß]+", q.lower())
            if len(w) >= 3 and w not in STOPWORDS]


@lru_cache(maxsize=2)
def _synonyms(level: str = "all") -> dict[str, str]:
    out = {}
    if SYNONYMS.exists():
        for line in SYNONYMS.read_text(encoding="utf-8").splitlines():
            if line.startswith("# --- post-hoc") and level == "dev":
                break
            if not line.strip() or line.startswith("#"):
                continue
            src, _, dst = line.partition("\t")
            out[src.strip().lower()] = dst.strip()
    return out


def expand_query(q: str, level: str | bool = "all") -> str:
    if not level or level == "none":
        return q
    syn = _synonyms("dev" if level == "dev" else "all")
    low = q.lower()
    extra = [dst for src, dst in syn.items()
             if re.search(rf"(?<![\wäöüß]){re.escape(src)}", low)]
    return q + (" " + " ".join(extra) if extra else "")


def _grams(s: str, n: int = 3) -> list[str]:
    s = f" {s} "
    return [s[i:i + n] for i in range(len(s) - n + 1)]


class NgramIndex:
    """Character trigram TF-IDF. Robust against German compounds and
    inflection ('Blutentnahme' vs. 'Entnahme von Blut', 'Impfung' vs. 'Schutzimpfung')."""

    def __init__(self, rows):
        self.codes = [r["code"] for r in rows]
        docs = [Counter(g for w in _words(_fold(r["text"])) for g in _grams(w)) for r in rows]
        df = Counter(g for d in docs for g in d)
        n = len(docs)
        self.idf = {g: math.log((n + 1) / (c + 0.5)) for g, c in df.items()}
        self.vecs = []
        self.inv = defaultdict(list)
        for i, d in enumerate(docs):
            v = {g: (1 + math.log(tf)) * self.idf[g] for g, tf in d.items()}
            norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            for g, x in v.items():
                self.inv[g].append((i, x / norm))

    def search(self, q: str, limit: int) -> list[str]:
        qv = Counter(g for w in _words(_fold(q)) for g in _grams(w))
        scores = defaultdict(float)
        for g, tf in qv.items():
            w = (1 + math.log(tf)) * self.idf.get(g, 0.0)
            for i, x in self.inv.get(g, ()):
                scores[i] += w * x
        best = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return [self.codes[i] for i, _ in best]


@lru_cache(maxsize=2)
def _ngram(db_path: str | None) -> NgramIndex:
    return NgramIndex(_all_rows(connect(db_path)))


def _fts(con, q: str, limit: int) -> list[str]:
    terms = [w for w in _words(q)]
    if not terms:
        return []
    match = " OR ".join(f'"{t}"*' for t in terms)
    rows = con.execute(
        "SELECT code FROM ziffer_fts WHERE ziffer_fts MATCH ? ORDER BY bm25(ziffer_fts) LIMIT ?",
        (match, limit)).fetchall()
    return [r["code"] for r in rows]


def rank(q: str, profile: Profile = DEFAULT, db_path: str | None = None,
         limit: int | None = None) -> list[str]:
    con = connect(db_path)
    limit = limit or profile.k
    q = expand_query(q, profile.synonyms)
    if profile.strategy == "fts":
        return _fts(con, q, limit)
    if profile.strategy == "ngram":
        return _ngram(db_path).search(q, limit)
    # hybrid: reciprocal rank fusion of word-prefix BM25 and trigram TF-IDF
    pool = 50
    a = _fts(con, q, pool)
    b = _ngram(db_path).search(q, pool)
    score = defaultdict(float)
    for lst in (a, b):
        for r, c in enumerate(lst):
            score[c] += 1.0 / (20 + r)
    return [c for c, _ in sorted(score.items(), key=lambda kv: -kv[1])[:limit]]


def search(q: str, k: int | None = None, section: str | None = None,
           profile: Profile = DEFAULT, db_path: str | None = None) -> str:
    """Top-k candidate codes for a free-text description."""
    con = connect(db_path)
    k = k or profile.k
    codes = rank(q, profile, db_path, limit=k if not section else 200)
    rows = []
    for c in codes:
        r = con.execute("SELECT * FROM ziffer WHERE code=?", (c,)).fetchone()
        if section and r["section"] != section.upper():
            continue
        rows.append(r)
        if len(rows) >= k:
            break
    if not rows:
        return "keine Treffer"
    if profile.fmt == "json":
        import json
        out = []
        for r in rows:
            d = {"code": r["code"], "text": r["text"], "points": r["points"],
                 "section": r["section"], "section_title": r["section_title"],
                 "subsection": r["subsection"], "subsection_title": r["subsection_title"],
                 "factor_class": r["factor_class"]}
            if profile.notes_in_search:
                d["notes"] = [n["text"] for n in con.execute(
                    "SELECT text FROM note WHERE code=?", (r["code"],))]
            out.append(d)
        return json.dumps(out, ensure_ascii=False, indent=2)
    lines = []
    for r in rows:
        pts = f"{r['points']}P" if r["points"] is not None else "%"
        lines.append(f"{r['code']}|{pts}|{_trunc(r['text'], profile.text_max)}")
    return "\n".join(lines)


# -------------------------------------------------------------------------- get

def _row(con, code: str):
    return con.execute("SELECT * FROM ziffer WHERE code=?", (normalize_code(code),)).fetchone()


def get(code: str, db_path: str | None = None) -> str:
    """Full text, points, section, fee range and the entry's own notes."""
    con = connect(db_path)
    r = _row(con, code)
    if r is None:
        return f"{code}: unbekannte Ziffer"
    thr, mx = FACTORS[r["factor_class"]]
    head = f"Nr. {r['code']} · {r['section']}.{r['subsection'] or '-'} {r['section_title']}"
    lines = [head, r["text"]]
    if r["points"] is not None:
        lines.append(f"{r['points']} Punkte · 1,0x {eur(fee(r['points'], 1))} € · "
                     f"{eur(thr).rstrip('0').rstrip(',')}x {eur(fee(r['points'], thr))} € (Regel) · "
                     f"{eur(mx).rstrip('0').rstrip(',')}x {eur(fee(r['points'], mx))} € (max)")
    else:
        lines.append("keine Punktzahl (prozentualer Zuschlag, siehe Text)")
    notes = con.execute("SELECT text FROM note WHERE code=? AND kind='note'", (r["code"],)).fetchall()
    for n in notes:
        lines.append(f"- {_trunc(n['text'], 300)}")
    n_rules = con.execute(
        "SELECT count(*) FROM note n JOIN note_ref f ON f.note_id=n.id "
        "WHERE f.code=? AND (n.code IS NULL OR n.code<>?)", (r["code"], r["code"])).fetchone()[0]
    if n_rules:
        lines.append(f"(+{n_rules} weitere Bestimmungen nennen Nr. {r['code']} → rules)")
    return "\n".join(lines)


def price(code: str, factor: float | None = None, db_path: str | None = None) -> str:
    con = connect(db_path)
    r = _row(con, code)
    if r is None:
        return f"{code}: unbekannte Ziffer"
    if r["points"] is None:
        return f"Nr. {r['code']}: prozentualer Zuschlag, kein fester Betrag"
    thr, mx = FACTORS[r["factor_class"]]
    f = factor or thr
    warn = ""
    if f > mx:
        warn = f" (über Höchstsatz {mx}x!)"
    elif f > thr:
        warn = f" (über Schwellenwert {thr}x: Begründung nötig)"
    return f"Nr. {r['code']}: {r['points']} P x {f:g} = {eur(fee(r['points'], f))} €{warn}"


# ------------------------------------------------------------------------ rules

def rules(code: str, db_path: str | None = None, limit: int = 8) -> str:
    """All notes and general rules that mention a code (cut to `limit`)."""
    con = connect(db_path)
    c = normalize_code(code)
    rows = con.execute(
        "SELECT DISTINCT n.id, n.code, n.section, n.kind, n.text FROM note n "
        "JOIN note_ref f ON f.note_id=n.id WHERE f.code=? ORDER BY n.id", (c,)).fetchall()
    if not rows:
        return f"keine Bestimmungen zu Nr. {c}"
    out = []
    for n in rows[:limit]:
        src = f"Nr. {n['code']}" if n["code"] else f"Abschnitt {n['section']}"
        out.append(f"[{src}] {_trunc(n['text'], 220)}")
    if len(rows) > limit:
        out.append(f"(+{len(rows) - limit} weitere)")
    return "\n".join(out)


# ------------------------------------------------------------------------ check

_SENT_SPLIT = re.compile(r"(?<=[a-zäöüß)])\.\s+(?=[A-ZÄÖÜ])")
_VERB = re.compile(r"\b(sind|ist|darf|dürfen|können|kann)\b")


def _codes_in(text: str, con) -> set[str]:
    from .ingest import expand_numbers
    codes = _existing_codes(con)
    found = expand_numbers(text, codes)
    for m in re.finditer(r"Abschnitt(?:en)?\s+([A-P])(?:\s+bis\s+([A-P]))?", text):
        lo, hi = m.group(1), m.group(2) or m.group(1)
        secs = [chr(x) for x in range(ord(lo), ord(hi) + 1)]
        found |= {r["code"] for r in con.execute(
            f"SELECT code FROM ziffer WHERE section IN ({','.join('?' * len(secs))})", secs)}
    return found


_CODES_CACHE: dict[int, list[str]] = {}


def _existing_codes(con) -> list[str]:
    key = id(con)
    if key not in _CODES_CACHE:
        _CODES_CACHE[key] = [r[0] for r in con.execute("SELECT code FROM ziffer")]
    return _CODES_CACHE[key]


def exclusion_pairs(sentence: str, owner: str | None, con) -> tuple[set[str], set[str], str] | None:
    """Return (X, Y, kind) meaning 'X must not be billed together with Y'."""
    s = sentence
    low = s.lower()
    if "nicht" not in low and "nur" not in low:
        return None
    # "nur ... als einzige Leistung oder im Zusammenhang mit <list>" -> whitelist
    m = re.search(r"nur .*?als einzige leistung oder im zusammenhang mit (.*)", low)
    if m and owner:
        return {owner}, _codes_in(s[m.start(1):], con), "only_with"
    if re.search(r"nicht zusammen mit anderen gebühren", low) and owner:
        return {owner}, set(), "alone"
    if "nicht" not in low:
        return None
    idx = low.find("neben")
    if idx < 0:
        idx = low.find("zusammen mit")
    if idx < 0:
        return None
    if idx < 3:  # "Neben X sind Y nicht ..."
        vm = _VERB.search(s, idx)
        if not vm:
            return None
        x = _codes_in(s[idx:vm.start()], con)
        y = _codes_in(s[vm.end():], con)
    else:        # "X ist neben Y nicht ..."
        x = _codes_in(s[:idx], con)
        y = _codes_in(s[idx:], con)
    if not x and owner and "nummer" not in low[:idx]:
        x = {owner}
    if not x or not y:
        return None
    return x, y, "not_with"


def _is_surcharge_for(con, b: str, a: str) -> bool:
    r = _row(con, b)
    if r is None or not r["text"].startswith("Zuschlag"):
        return False
    from .ingest import expand_numbers
    ctx = f"{r['text']} {r['subsection_title'] or ''}"
    return a in expand_numbers(ctx, _existing_codes(con))


def check(codes: list[str], db_path: str | None = None, verbose: bool = False) -> str:
    """Report exclusion rules ('nicht neben') between the given codes."""
    con = connect(db_path)
    cs = [normalize_code(c) for c in codes]
    unknown = [c for c in cs if _row(con, c) is None]
    if unknown:
        return "unbekannt: " + ", ".join(unknown)
    hits: dict[tuple, list[str]] = {}  # (sentence, a, source) -> [b, ...]
    rows = con.execute(
        f"SELECT DISTINCT n.id, n.code, n.section, n.text FROM note n JOIN note_ref f "
        f"ON f.note_id=n.id WHERE f.code IN ({','.join('?' * len(cs))})", cs).fetchall()
    for n in rows:
        for sent in _SENT_SPLIT.split(n["text"]):
            sent = re.sub(r"^\d+\.\s+", "", sent)
            res = exclusion_pairs(sent, n["code"], con)
            if not res:
                continue
            x, y, kind = res
            for a in cs:
                if a not in x:
                    continue
                for b in cs:
                    if a == b:
                        continue
                    if kind == "only_with" and _is_surcharge_for(con, b, a):
                        continue  # e.g. surcharge A-D is made for Nr. 1/3/5-8
                    hit = (kind == "alone"
                           or (kind == "only_with" and b not in y)
                           or (kind == "not_with" and b in y))
                    if hit:
                        src = f"Nr. {n['code']}" if n["code"] else f"Abschnitt {n['section']}"
                        lst = hits.setdefault((sent, a, src), [])
                        if b not in lst:
                            lst.append(b)
    found = [f"✗ {a} nicht neben {', '.join(bs)}: {_trunc(sent, 200)} [{src}]"
             for (sent, a, src), bs in hits.items()]
    if not found:
        return (f"✓ keine Ausschlussregel zwischen {', '.join(cs)} gefunden "
                f"(nur 'nicht neben'-Regeln geprüft; Häufigkeitsgrenzen → rules)")
    return "\n".join(found)
