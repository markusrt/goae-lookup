import csv
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goae import db, tools  # noqa: E402
from goae.fill import fill  # noqa: E402
from goae.ingest import build  # noqa: E402


class Parse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "goae.db")
        cls.stats = build(Path(cls.db))
        cls.con = db.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def row(self, code):
        return self.con.execute("SELECT * FROM ziffer WHERE code=?", (code,)).fetchone()

    def test_counts(self):
        self.assertGreater(self.stats["entries"], 2700)
        missing = self.con.execute("SELECT count(*) FROM ziffer WHERE points IS NULL").fetchone()[0]
        self.assertLessEqual(missing, 3)  # only percentage surcharges

    def test_entries(self):
        self.assertEqual(self.row("250")["points"], 40)
        self.assertEqual(self.row("1")["section"], "B")
        self.assertEqual(self.row("1200")["section"], "I")   # 'I' as section letter
        self.assertEqual(self.row("1")["subsection"], "I")   # 'I' as roman numeral

    def test_wrapped_and_catalog_rows(self):
        k1 = self.row("K 1")
        self.assertEqual(k1["points"], 120)
        self.assertIn("4. Lebensjahr", k1["text"])
        self.assertEqual(self.row("3504")["points"], 60)

    def test_factor_classes(self):
        self.assertEqual(self.row("1")["factor_class"], "personal")
        self.assertEqual(self.row("250")["factor_class"], "tech")   # listed in Abschnitt A
        self.assertEqual(self.row("5137")["factor_class"], "tech")  # Abschnitt O
        self.assertEqual(self.row("3550")["factor_class"], "lab")   # Abschnitt M


class Query(unittest.TestCase):
    def test_price(self):
        self.assertIn("10,72 €", db.price("1"))
        self.assertIn("4,20 €", db.price("250"))
        self.assertIn("4,02 €", db.price("3550"))
        self.assertIn("Höchstsatz", db.price("1", 4.0))

    def test_get_is_small(self):
        out = db.get("4")
        self.assertIn("nur einmal", out)
        self.assertLess(len(out), 1200)

    def test_search(self):
        for q, code in [("Blutabnahme aus der Vene", "250"), ("Ruhe-EKG", "651"),
                        ("Hausbesuch", "50"), ("Ultraschall der Schilddrüse", "417")]:
            codes = [l.split("|")[0] for l in db.search(q).splitlines()]
            self.assertIn(code, codes, q)
            self.assertLessEqual(len(codes), 5)

    def test_check(self):
        self.assertIn("✗", db.check(["1", "3"]))
        self.assertIn("✗", db.check(["5", "6"]))
        self.assertIn("✗", db.check(["5", "600"]))     # section rule
        self.assertIn("✗", db.check(["437", "3550"]))  # 'Abschnitt M'
        self.assertNotIn("✗", db.check(["1", "5"]))
        self.assertNotIn("✗", db.check(["3", "A"]))    # surcharge made for Nr. 3

    def test_unknown(self):
        self.assertIn("unbekannt", db.get("99999"))


class Mcp(unittest.TestCase):
    def test_roundtrip(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "goae_search", "arguments": {"q": "Ruhe-EKG"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "nope"},
        ]
        p = subprocess.run([sys.executable, "-m", "goae", "mcp"], cwd=ROOT, capture_output=True,
                           text=True, input="\n".join(json.dumps(m) for m in msgs) + "\n", timeout=60)
        out = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual(len(out), 4)  # no answer to the notification
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in out[1]["result"]["tools"]],
                         [t["name"] for t in tools.TOOLS])
        self.assertIn("651|", out[2]["result"]["content"][0]["text"])
        self.assertEqual(out[3]["error"]["code"], -32601)


class FakeLLM(http.server.BaseHTTPRequestHandler):
    answer = {"code": "250", "confidence": 0.9}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLLM.prompts.append(body["messages"][0]["content"])
        data = json.dumps({"choices": [{"message": {"content": json.dumps(FakeLLM.answer)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class Fill(unittest.TestCase):
    def setUp(self):
        FakeLLM.prompts = []
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        self.dir = tempfile.TemporaryDirectory()
        self.src = Path(self.dir.name) / "in.csv"
        self.src.write_text("leistung\nBlutabnahme aus der Vene\nRuhe-EKG\n", encoding="utf-8")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    def test_fill_with_llm(self):
        dst = Path(self.dir.name) / "out.csv"
        fill(self.src, dst, None, 5, self.url, "x", 0.5)
        rows = list(csv.DictReader(dst.open(encoding="utf-8")))
        self.assertEqual(rows[0]["goae"], "250")
        self.assertEqual(rows[1]["goae"], "")          # 250 is not a candidate for Ruhe-EKG
        self.assertTrue(all(len(p) < 1000 for p in FakeLLM.prompts))  # tiny prompts

    def test_resume(self):
        dst = Path(self.dir.name) / "out.csv"
        fill(self.src, dst, None, 5, None, "x", 0.5)
        fill(self.src, dst, None, 5, self.url, "x", 0.5)
        self.assertEqual(len(FakeLLM.prompts), 2)
        n = len(FakeLLM.prompts)
        fill(self.src, dst, None, 5, self.url, "x", 0.5)  # 'Blutabnahme' row is done now
        self.assertEqual(len(FakeLLM.prompts) - n, 1)


class AgentMode(unittest.TestCase):
    def test_next_and_set(self):
        from goae.fill import next_rows, output_path, set_rows
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.csv"
            original = "leistung\nBlutabnahme aus der Vene\nRuhe-EKG\nHausbesuch\n"
            path.write_text(original, encoding="utf-8")
            out = next_rows(path, n=2)
            self.assertTrue(out.startswith("3 open"))
            self.assertIn("#1 Blutabnahme", out)
            self.assertIn("  250|", out)
            self.assertNotIn("#3", out)                       # batch size
            out = set_rows(path, ["1=250", "2=99999"])
            self.assertTrue(out.startswith("saved 1=250; errors: 2: unknown code 99999"))
            self.assertIn("#2 Ruhe-EKG", out)                  # next batch follows
            set_rows(path, ["2=651", "3=?"])
            self.assertTrue(next_rows(path).startswith("done: 3 rows"))
            self.assertIn("'?' in rows 3", next_rows(path))
            self.assertEqual(path.read_text(encoding="utf-8"), original)  # input untouched
            result = list(csv.DictReader(output_path(path).open(encoding="utf-8")))
            self.assertEqual([r["goae"] for r in result], ["250", "651", "?"])
            self.assertTrue(result[0]["goae_candidates"].startswith("1012 250"))

    def test_fill_with_llm_cmd(self):
        from goae.fill import fill
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "fake.py"
            fake.write_text("import sys, re\n"
                            "m = re.search(r'^([^|\\s]+)\\|', sys.stdin.read(), re.M)\n"
                            "print('ok {\"code\": \"%s\", \"confidence\": 0.9}' % m.group(1))\n",
                            encoding="utf-8")
            src = Path(d) / "t.csv"
            src.write_text("leistung\nRuhe-EKG\n", encoding="utf-8")
            dst = fill(src, None, None, 5, None, "", 0.5, llm_cmd=f'"{sys.executable}" "{fake}"')
            rows = list(csv.DictReader(dst.open(encoding="utf-8")))
            self.assertEqual(rows[0]["goae"], "651")


class AbrechnungsstelleAdapter(unittest.TestCase):
    """Offline test of the site adapter against a hand-written fixture."""

    def test_parse_and_build(self):
        from goae.sources import abrechnungsstelle as src
        fixture = ROOT / "tests" / "fixtures" / "abrechnungsstelle_sample.html"
        entries = src.parse_pages([fixture])
        by = {e.code: e for e in entries}
        self.assertEqual(list(by), ["1", "2", "K 1", "3630.H", "A3754"])
        self.assertEqual(by["1"].points, 80)
        self.assertEqual(by["1"].text, "Beratung - auch mittels Fernsprecher")
        self.assertIn("Nummern 2, 3", by["1"].notes[0])
        self.assertIn("einer Inanspruchnahme des Arztes nicht zusammen mit anderen Gebühren", by["2"].notes[0])
        self.assertEqual(len(by["2"].notes), 2)  # paragraph + generated exclusion sentence
        self.assertFalse(any("Footer" in n for e in entries for n in e.notes))

        orig = src.fetch_pages
        src.fetch_pages = lambda refresh=False, pages=None: [fixture]
        try:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "site.db"
                stats = src.build(path)
                self.assertEqual(stats["entries"], 5)
                self.assertIn("✗", db.check(["1", "2"], db_path=str(path)))
                self.assertIn("✗", db.check(["1", "K 1"], db_path=str(path)))
                self.assertIn("Fernsprecher", db.search("Telefon Beratung", db_path=str(path)))
                self.assertIn("Höchstwert", db.get("3630h", db_path=str(path)))
                db.connect.cache_clear()
        finally:
            src.fetch_pages = orig


if __name__ == "__main__":
    unittest.main()
