# goae-lookup

**Context-minimal, agent-agnostic lookup of German GOÄ fee codes.**

A small worked example for a common problem: an agent with a local LLM is asked to
complete a table ("find the GOÄ code for each service"). The reference page is loaded
into a docs/RAG server, every query returns large text chunks, the 32K context window
fills up, the model loses track of what it already found — and the house of cards
collapses. A bigger window only delays the moment.

This repo shows the alternative: **turn the reference into structured data, answer every
question with a few lines of text, and keep state in files instead of in the context.**

```text
$ python -m goae search "Blutabnahme aus der Vene"
1012|74P|Blutentnahme beim Fetus
250|40P|Blutentnahme mittels Spritze, Kanüle oder Katheter aus der Vene
284|90P|Eigenbluteinspritzung - einschließlich Blutentnahme -
1013|178P|Blutentnahme beim Fetus - einschließlich pH-Messung(en) im Blut -
251|60P|Blutentnahme mittels Spritze oder Kanüle aus der Arterie

$ python -m goae get 250
Nr. 250 · C.II Nichtgebietsbezogene Sonderleistungen
Blutentnahme mittels Spritze, Kanüle oder Katheter aus der Vene
40 Punkte · 1,0x 2,33 € · 1,8x 4,20 € (Regel) · 2,5x 5,83 € (max)
(+3 weitere Bestimmungen nennen Nr. 250 → rules)

$ python -m goae check 1 3 250
✗ 3 nicht neben 1, 250: Die Leistung nach Nummer 3 (Dauer mindestens 10 Minuten) ist nur berechnungsfähig als einzige Leistung oder im Zusammenhang mit einer Untersuchung nach Nummer 5, 6, 7, 8, 800 oder 801. … [Nr. 3]
```

## Results

Built in a measurement loop over 40 example tasks (find a code, compute a fee, check an
exclusion, look up a rule). Full numbers, per-task details and the loop history are in
**[CONTEXT_STATS.md](CONTEXT_STATS.md)**.

| Approach | Tokens per search | 40 tasks in one session | Holdout ✓ |
|---|---|---|---|
| Whole fee schedule in the prompt | ~137,000 | does not fit | (17/17) |
| Generic docs RAG, top-5 chunks | ~1,150 | ~49,000 | 5/17 |
| Naive tool (verbose schema, JSON, k=10) | ~690 | ~22,000 | 13/17 |
| **This repo** | **~90** | **~3,700** | 12/17 blind, 17/17 after adding 6 synonyms |

Tokens are estimated as characters/4.

## Principles

1. **Structured data, not text chunks.** The fee schedule is parsed once into SQLite
   (code, text, points, section, notes, rules). Searching is a database query, not
   "read five pages and hope".
2. **Every answer fits on one screen.** Line format instead of JSON, texts truncated to
   80 characters, top 5 results. A search result is ~90 tokens.
3. **Progressive disclosure.** `search` → `get` → `rules`/`check`. Details are fetched
   only for the one code that matters.
4. **Compute deterministically.** Fees (§ 5 GOÄ factors) and exclusion rules are
   evaluated in code. The model never does arithmetic or legal parsing.
5. **State lives in files, not in the conversation.** For "complete this table", a
   pipeline makes one fresh, tiny LLM call per row and writes the result immediately.
   Nothing accumulates; an interrupted run resumes where it stopped.
6. **Small schema.** Four tools, terse descriptions: 245 tokens instead of 583. The schema
   is re-sent with every request.

## Quick start

Requirements: Python 3.10+ with SQLite FTS5 (included in standard CPython builds). No
other dependencies.

```bash
git clone https://github.com/markusrt/goae-lookup && cd goae-lookup
python -m goae search "Ultraschall der Schilddrüse"   # builds data/goae.db on first run (~1 s)
python -m goae get 417
python -m goae price 417 --factor 2.0
python -m goae rules 5
```

Optionally `pip install -e .` gives you a `goae` command.

## Using it from an agent

The same four operations are offered three ways, so any agent can use them.

**Shell (any agent that can run commands).** Put the snippet from
[`examples/AGENTS.md`](examples/AGENTS.md) into your `AGENTS.md` / `CLAUDE.md` /
system prompt.

**MCP (stdio, standard library only).** Tools: `goae_search`, `goae_get`,
`goae_check`, `goae_rules`.

opencode (`opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "goae": {
      "type": "local",
      "command": ["python3", "-m", "goae", "mcp"],
      "environment": { "PYTHONPATH": "/path/to/goae-lookup" },
      "enabled": true
    }
  }
}
```

Claude Code / Claude Desktop / most other clients (`.mcp.json`):

```json
{
  "mcpServers": {
    "goae": {
      "command": "python3",
      "args": ["-m", "goae", "mcp"],
      "env": { "PYTHONPATH": "/path/to/goae-lookup" }
    }
  }
}
```

**Python.** `from goae import db; db.search("Hausbesuch")`.

## Completing a table: move the loop out of the conversation

**What goes wrong today.** When an agent completes a table inside one chat, every row
adds a search call *and its result* to the conversation, and nothing is ever removed:

```text
system prompt + tool definitions
row 1  → tool call → result 1      ┐
row 2  → tool call → result 2      │  the conversation grows with every row;
…                                  │  after a few dozen rows the window is full and
row 30 → tool call → result 30     ┘  the model loses track of what it already found
```

Smaller tool results (the rest of this repo) slow this down by a factor of 10–15, but the
growth is still linear. For a table of hundreds of rows it will eventually overflow.

**The fix: one small, independent LLM call per row.** A plain script walks through the
table. For each row it does the lookup itself, asks the model one short question, and
writes the answer straight into the output file. The model never sees the other rows,
and the progress is stored in the file, not in a conversation:

```text
for each row in leistungen.csv:
    candidates = goae search(row)        # SQLite, no LLM, ~90 tokens
    prompt     = row + candidates        # ~140 tokens, always the same size
    answer     = LLM(prompt)             # fresh call, no chat history
    write answer to leistungen.goae.csv  # the file is the memory
```

**One row, end to end.** Input row `Ruhe-EKG`. The prompt the model receives is the
complete context, nothing else:

```text
Wähle die passende GOÄ-Ziffer für die Leistung.
Leistung: Ruhe-EKG
Kandidaten (Ziffer|Punkte|Text):
651|253P|Elektrokardiographische Untersuchung in Ruhe - auch gegebenenfalls nach…
650|152P|Elektrokardiographische Untersuchung zur Feststellung einer Rhythmusstörung…
653|253P|Elektrokardiographische Untersuchung auf telemetrischem Wege
659|400P|Elektrokardiographische Untersuchung über mindestens 18 Stunden (Langzeit-EKG)…
5422|1000P|Szintigraphische Untersuchung des Myokards mit myokardaffinen Tracern in Ruhe…
Antworte nur mit JSON: {"code": "<Ziffer oder leer>", "confidence": <0..1>}
```

The model answers e.g. `{"code": "651", "confidence": 0.9}`, and the script adds three
columns to that row of the output file:

| leistung | … | goae | goae_confidence | goae_candidates |
|---|---|---|---|---|
| Ruhe-EKG | … | 651 | 0.90 | 651 650 653 659 5422 |

Safety rails: an answer that is not one of the candidates, or below `--min-confidence`,
leaves `goae` empty so a human can decide. If the run is interrupted, starting it again
skips the rows that are already done.

**Three ways to use it**

```bash
# 1. Candidates only, no LLM: a person picks from goae_candidates (e.g. in Excel)
python -m goae fill examples/leistungen.csv

# 2. Fully automatic with a local model (any OpenAI-compatible endpoint:
#    Ollama, llama.cpp server, LM Studio, vLLM)
python -m goae fill examples/leistungen.csv --llm http://localhost:11434/v1 --model qwen2.5:7b-instruct

# 3. Options: which column holds the description, how many candidates
python -m goae fill my.csv --col Leistungsbeschreibung -k 5 -o my.done.csv
```

If you would rather keep the agent in charge, give it the same rule: process one row at a
time with `goae_search`, write the result into the file immediately, and do not collect
results in the conversation. That works, but small local models are noticeably more
reliable with the script.

## The measurement loop

```bash
python bench/run.py                   # all iterations → bench/results.json + CONTEXT_STATS.md
python bench/run.py --only final      # one iteration, lists failed tasks
python bench/run.py --log "what I changed"   # append a line to the loop history
```

Every iteration answers all tasks in [`bench/tasks.json`](bench/tasks.json) with a
scripted, optimal sequence of tool calls and records schema size, output size and
whether the answer is contained in the output. Tasks are split into *dev* (tune on
these) and *holdout* (measure only). The loop that produced the current defaults:
JSON → lines, truncation and smaller k (cheap but loses recall), hybrid ranking
(BM25 + character trigrams, robust against German compounds), a synonym file for
colloquial terms, and a terser tool schema.

To improve recall for your own wording, add a line to
[`data/synonyms.tsv`](data/synonyms.tsv) (`colloquial<TAB>official wording`), add a task
to `bench/tasks.json`, and rerun the benchmark.

## Other sources: abrechnungsstelle.com

The original question used <https://abrechnungsstelle.com/goae/ziffern/>, which adds
analogue codes and exclusion lists per code. `goae/sources/abrechnungsstelle.py` shows
how to ingest such a site **without putting its content into this repository**: the data
is downloaded on your machine when you build.

```bash
python -m goae build --source abrechnungsstelle      # → data/goae-abrechnungsstelle.db
python -m goae --db data/goae-abrechnungsstelle.db search "Ruhe-EKG"
export GOAE_DB=data/goae-abrechnungsstelle.db        # same for the MCP server / agents
```

* Downloads 14 range pages once (robots.txt is checked, one request every 3 s), caches
  the raw HTML in `data/cache/` and writes a separate database. `data/cache/` and the
  database are git-ignored. `--refresh` downloads again.
* Per code it takes the service text, points, the notes and the *Ausschlussziffern*.
  The exclusion lists are turned into rule sentences, so `goae check` uses them too.
  Section structure, § 5 factors and general rules still come from the official text.
* The parser is tested offline against a hand-written fixture that mimics the page
  markup (`tests/fixtures/`). If the site changes its layout,
  `python -m goae.sources.abrechnungsstelle --debug 1-498` shows what the parser sees.
* Check the site's terms before you use the data beyond your own purposes.

The same pattern works for any other source: produce `Entry` objects (code, text,
points, notes) and call `ingest.write_db`. Search, tools, MCP server and the benchmark
stay unchanged.

## Data and limitations

* Source: the official GOÄ text (BGBl. I 1996, last amended 21.10.2019), via the
  Markdown mirror of gesetze-im-internet.de at
  [bundestag/gesetze](https://github.com/bundestag/gesetze) (`g/goae_1982/index.md`,
  Unlicense). German statutes are official works without copyright (§ 5 UrhG). A copy is
  in [`data/source/`](data/source/).
* Not included in the official text: analogue codes (BÄK Analogbewertungen),
  commentary, case law, the planned new GOÄ. For analogue codes see the
  abrechnungsstelle.com adapter above.
* `check` only evaluates explicit "nicht neben" / "nur … im Zusammenhang mit" rules.
  Frequency limits ("im Behandlungsfall nur einmal") are shown via `get`/`rules`, not
  enforced.
* This is a technical example, not billing or legal advice.

## Layout

```text
goae/ingest.py      parse the Markdown fee schedule → SQLite (+ FTS5 indexes)
goae/db.py          search / get / price / check / rules, all returning short text
goae/tools.py       tool definitions (terse set + the verbose set used as baseline)
goae/mcp_server.py  MCP over stdio, no dependencies
goae/fill.py        table pipeline with optional OpenAI-compatible LLM
goae/sources/       optional adapters for other sources (abrechnungsstelle.com)
goae/__main__.py    CLI
bench/              tasks, benchmark loop, results, history
data/               source text, synonyms; databases and cache are generated
tests/              unittest suite (python -m unittest discover -s tests), HTML fixture
```

## License

Code: MIT. Data: see above (public domain).
