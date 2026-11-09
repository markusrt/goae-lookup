# goae-lookup

**How to give an LLM agent access to reference data that is far too large for its
context window — a worked example using the German medical fee schedule (GOÄ).**

The code is a complete, dependency-free tool (CLI, MCP server, batch pipeline) for
looking up GOÄ fee codes. The ideas behind it apply to any large reference dataset:
price lists, product catalogs, code systems (ICD-10, customs tariffs), regulations,
configuration references.

## The problem

Many agent tasks need lookups in a dataset that does not fit into the prompt:
*"Find the right code for each of these 200 items."* The usual approaches break down:

* **Put everything into the prompt.** The GOÄ alone is 550 KB of text, about 137,000
  tokens. It does not fit into a 32K or 64K window.
* **Generic RAG over text chunks.** Each query returns several pages of text (~1,000+
  tokens), of which a few lines are relevant. In a multi-step task every result stays in
  the conversation. After a few dozen lookups the window is full, and well before that the
  model starts losing track of what it has already found.

A larger context window only postpones this. The fix is to change what the agent gets
back.

## The pattern

1. **Turn the source into structured data once.** Parse it into records (here: code,
   text, points, section, notes, rules) in SQLite. A lookup becomes a query, not
   "read five pages and pick out the answer".
2. **Answer every question in a few lines.** Compact line format instead of JSON, long
   texts truncated, top 5 results. A search result here is ~90 tokens.
3. **Disclose details progressively.** A cheap `search` first, then `get` for the one
   record that matters, then `rules`/`check` only if needed.
4. **Compute in code, not in the model.** Fees and exclusion rules are evaluated by the
   tool. The model never does arithmetic or parses legal text.
5. **Keep state in files, not in the conversation.** For batch work, run a loop outside
   the agent: one small, independent LLM call per item, result written to a file
   immediately.
6. **Keep the tool schema small.** Tool definitions are sent with every request. Four
   tools with terse descriptions cost 245 tokens here instead of 583.
7. **Measure.** Define example tasks with known answers and measure tokens *and*
   correctness for every design change (see [How the design was found](#how-the-design-was-found)).

## Results for the GOÄ example

40 example tasks with known answers: find a code from a plain-language description,
compute a fee, check whether two codes may be billed together, look up a rule.

| Approach | Tokens per search | All 40 tasks in one conversation | Test tasks answered correctly¹ |
|---|---|---|---|
| Whole fee schedule in the prompt | ~137,000 | does not fit | – |
| Generic RAG, top-5 text chunks | ~1,150 | ~49,000 | 5/17 |
| Straightforward tool (verbose, JSON, 10 results) | ~680 | ~22,000 | 13/17 |
| **This repository** | **~90** | **~3,700** | 12/17, 17/17 with 6 added synonyms |

¹ 17 of the 40 tasks are *test tasks*: they were not used while the tool was being
improved, so they show how well it handles questions it was not tuned for. 12/17 is that
result. Only in a final step were 6 synonyms added for the failed ones, which gives 17/17
(see [How the design was found](#how-the-design-was-found)).

Token counts are estimates: number of characters divided by four. Results for every
single task are in [CONTEXT_STATS.md](CONTEXT_STATS.md).

## Quick start

Requirements: Python 3.10+ with SQLite FTS5 (part of standard CPython builds). No other
dependencies.

```bash
git clone https://github.com/markusrt/goae-lookup && cd goae-lookup
python -m goae search "Blutabnahme aus der Vene"   # builds data/goae.db on first run (~1 s)
```

```text
1012|74P|Blutentnahme beim Fetus
250|40P|Blutentnahme mittels Spritze, Kanüle oder Katheter aus der Vene
284|90P|Eigenbluteinspritzung - einschließlich Blutentnahme -
1013|178P|Blutentnahme beim Fetus - einschließlich pH-Messung(en) im Blut -
251|60P|Blutentnahme mittels Spritze oder Kanüle aus der Arterie
```

```bash
python -m goae get 250          # full text, points, fees, own rules
python -m goae check 1 3 250    # may these codes be billed together?
python -m goae rules 5          # general rules that mention a code
python -m goae price 250 --factor 2.0
```

```text
$ python -m goae get 250
Nr. 250 · C.II Nichtgebietsbezogene Sonderleistungen
Blutentnahme mittels Spritze, Kanüle oder Katheter aus der Vene
40 Punkte · 1,0x 2,33 € · 1,8x 4,20 € (Regel) · 2,5x 5,83 € (max)
(+3 weitere Bestimmungen nennen Nr. 250 → rules)

$ python -m goae check 1 3 250
✗ 3 nicht neben 1, 250: Die Leistung nach Nummer 3 (Dauer mindestens 10 Minuten) ist nur berechnungsfähig als einzige Leistung oder im Zusammenhang mit einer Untersuchung nach Nummer 5, 6, 7, 8, 800 oder 801. … [Nr. 3]
```

Optionally `pip install -e .` provides a `goae` command.

## Using it from an agent

The same operations are available in three ways, so it works with any agent.

**Shell** — for any agent that can run commands. Add the snippet from
[`examples/AGENTS.md`](examples/AGENTS.md) to your `AGENTS.md`, `CLAUDE.md` or system
prompt.

**MCP** — stdio server, standard library only. Tools: `goae_search`, `goae_get`,
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

Claude Code, Claude Desktop and most other clients (`.mcp.json`):

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

**Python** — `from goae import db; print(db.search("Hausbesuch"))`.

## Processing many items: a loop outside the conversation

Compact results make the context fill up more slowly. But if one conversation handles
all items, it still grows with every item, because each result stays in it:

```text
system prompt + tool definitions
item 1  → tool call → result 1      ┐
item 2  → tool call → result 2      │  every result stays in the conversation;
…                                   │  with enough items the window overflows
item N  → tool call → result N      ┘
```

For batch work, move the loop into a script. For each item it runs the lookup itself,
asks the model one short question and writes the answer to the output file. Every LLM
call starts without any chat history, and the file holds the progress:

```text
for each row in input.csv:
    candidates = search(row)        # database, no LLM, ~90 tokens
    prompt     = row + candidates   # ~140 tokens, the same for every row
    answer     = LLM(prompt)        # fresh call, no history
    write answer to output.csv      # the file is the memory
```

**One row, end to end.** For the input row `Ruhe-EKG` this is the complete prompt the
model receives:

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

The model answers, for example, `{"code": "651", "confidence": 0.9}`, and the row in the
output file gets three new columns:

| leistung | … | goae | goae_confidence | goae_candidates |
|---|---|---|---|---|
| Ruhe-EKG | … | 651 | 0.90 | 651 650 653 659 5422 |

An answer that is not among the candidates, or has a confidence below
`--min-confidence`, leaves `goae` empty for a person to decide. An interrupted run
continues with the rows that are not done yet.

```bash
# candidates only, no LLM: a person picks from goae_candidates (e.g. in a spreadsheet)
python -m goae fill examples/leistungen.csv

# fully automatic with a local model behind any OpenAI-compatible endpoint
# (Ollama, llama.cpp server, LM Studio, vLLM)
python -m goae fill examples/leistungen.csv --llm http://localhost:11434/v1 --model qwen2.5:7b-instruct

# fully automatic with any CLI that reads a prompt on stdin (see below)
python -m goae fill examples/leistungen.csv --llm-cmd "claude -p --model haiku"

# options: column with the description, number of candidates, output file
python -m goae fill my.csv --col Leistungsbeschreibung -k 5 -o my.done.csv
```

### No API? Use the agent CLI you already have as the LLM

`--llm-cmd` runs a command line tool that reads a prompt on stdin, once per row. Each call
is a new process, so every row starts with an empty conversation:

```bash
python -m goae fill examples/leistungen.csv --llm-cmd claude
python -m goae fill examples/leistungen.csv --llm-cmd "ollama run qwen2.5:7b-instruct"
```

`claude` is a preset for Claude Code that uses your normal subscription, no API key.
The preset matters: a plain `claude -p` call carries Claude Code's complete system
prompt, tool definitions, MCP servers and skills into *every* call. The preset replaces
the system prompt and switches the rest off (`--system-prompt … --tools "" --strict-mcp-config
--disallowedTools "mcp__*" --no-chrome --disable-slash-commands --no-session-persistence`,
model `haiku`). Measured with Claude Code 2.1.321 on the example table:

| | Input tokens per row | 8 rows |
|---|---|---|
| plain `claude -p` | ~42,000 | ~340,000 |
| `--llm-cmd claude` (preset) | ~2,000 | ~16,000, 8/8 correct, 18 s |

For comparison, an agent session is not free per step either: every turn re-sends the
whole conversation, including the agent's own system prompt (~30,000 tokens in Claude
Code), mostly as cheaper cached input. The per-row preset calls stay small and constant.

### Letting an agent work through the table

If an agent should do the work inside its own session, two commands keep that session
small. `next` shows a batch of open rows with three short candidates each; `set` saves a
whole batch of answers and immediately prints the next batch. One tool call handles ten
rows, and the agent never has to open the file:

```text
$ python -m goae next examples/leistungen.csv
8 open; answer with: set leistungen.csv ROW=CODE … (? = nothing fits)
#1 Blutabnahme aus der Vene
  1012|74P|Blutentnahme beim Fetus
  250|40P|Blutentnahme mittels Spritze, Kanüle oder Katheter aus der…
  284|90P|Eigenbluteinspritzung - einschließlich Blutentnahme -
#2 Ruhe-EKG
  651|253P|Elektrokardiographische Untersuchung in Ruhe - auch…
…
$ python -m goae set examples/leistungen.csv 1=250 2=651 3=375 4=3 5=417 6=3550 7=50 8=70
saved 1=250 2=651 3=375 4=3 5=417 6=3550 7=50 8=70
done: 8 rows → leistungen.goae.csv
```

A prompt for the agent:

```text
Find the GOÄ codes for examples/leistungen.csv. Work in batches:

1. Run `python -m goae next examples/leistungen.csv`. It lists up to 10 open rows
   (#row and description), each with three candidate codes.
2. For each row pick the matching candidate. Only if none fits, run
   `python -m goae search "<medical term>"` for that row; use ? if still nothing fits.
3. Save the whole batch with one command, e.g.
   `python -m goae set examples/leistungen.csv 1=250 2=651 3=?`
   It saves the answers and prints the next batch. Repeat 2–3 until it prints "done".

Rules: do not open, print or edit the CSV files yourself. Do not repeat rows or results
in your messages, and keep your reasoning short. At the end, report only the output file
and the rows marked ?.
```

What to expect: a batch of 10 rows is about 530 tokens of tool output. The agent's own
reasoning and tool-call messages usually cost more than that, which is why batching
matters: it pays this overhead once per ten rows instead of once per row.

Two more things keep the session small:

* **Results go to a separate file** (`leistungen.goae.csv`); the input is never changed.
  Some agents re-insert a file into the conversation whenever a file they have read
  changes on disk (Claude Code does this as a "file updated" note with a diff). Writing
  to a file the agent has never opened avoids that.
* **Delegate the loop to a subagent** if your agent supports it (Claude Code, opencode):
  start the prompt above with "Use a subagent for this and report only its summary." The
  subagent's context is discarded afterwards; your main conversation only receives the
  summary.

No MCP configuration is needed for any of this: an agent that can run shell commands
uses the CLI. MCP (`.mcp.json`, `opencode.json`) is only needed for agents without shell
access, or if you prefer tool calls to shell commands; `next` and `set` are CLI-only.

## How the design was found

The defaults in this repository were not designed up front. They are the result of an
experiment that you can rerun and reuse for your own data:

1. **Write tasks with known answers** in [`bench/tasks.json`](bench/tasks.json), split
   into two groups:
   * 23 **tuning tasks** (`dev` in the file). While improving the tool you may look at
     these, e.g. when a search fails, find out why and fix it.
   * 17 **test tasks** (`holdout` in the file). These are only run, never used to
     decide on a change. If a change helps the tuning tasks but not the test tasks, it
     was probably tailored to those specific questions and will not help real users.
2. **Answer every task with the tools** and measure three things: the size of the tool
   definitions, the size of all tool results, and whether the correct answer is
   contained in them.
3. **Change one thing, measure again, keep what helps.**

Each step in the table is one such change. *Tokens* is the average size of a search
result; the last two columns count correctly answered tasks.

| Step | Change | Tokens | Tuning | Test |
|---|---|---|---|---|
| it1 | Straightforward tool: JSON, full texts and notes, 10 results | 678 | 17/23 | 13/17 |
| it2 | Line format `code\|points\|text` instead of JSON, notes only on request | 171 | 17/23 | 13/17 |
| it3 | Texts truncated to 80 characters, 5 results — cheap, but loses hits | 62 | 15/23 | 11/17 |
| it4 | Hybrid ranking (word search + character trigrams for German compounds) | 88 | 19/23 | 12/17 |
| it5 | Synonym file for plain-language terms, built from failed tuning tasks | 88 | 23/23 | 12/17 |
| it6 | Terse tool schema, 4 instead of 5 tools (583 → 245 tokens per request) | 88 | 23/23 | 12/17 |
| it8 | 6 synonyms for the failed test tasks (from here on the test tasks are no longer unseen) | 89 | 23/23 | 17/17 |

The lesson: output format and size bring the largest savings, better ranking wins back
the hits that truncation costs, and the remaining misses are vocabulary gaps.

**Where the synonyms come from.** Users write "Blutabnahme", "EKG" or "MRT"; the official
text says "Blutentnahme", "Elektrokardiographische Untersuchung" and
"Magnetresonanztomographie". No ranking method bridges that gap reliably, so
[`data/synonyms.tsv`](data/synonyms.tsv) maps everyday terms to the official wording and
the search adds that wording to the query. Every entry was added by hand after a search
failed: look at the task, look up the official text of the expected code, add one line.
Ten entries came from failed tuning tasks (step it5) and one from the example table in
`examples/`. Six more were added after test tasks failed (step it8, marked separately in
the file; "CT" was added along with "MRT"). In a real deployment the best source
is the searches your users run that find nothing; a domain expert or an LLM can propose
candidates, but each entry should be checked against a failing example.

```bash
python bench/run.py                          # all steps → CONTEXT_STATS.md
python bench/run.py --only final             # current defaults, lists failed tasks
python bench/run.py --log "what I changed"   # also record the run in the history
```

[CONTEXT_STATS.md](CONTEXT_STATS.md) has all numbers, including the two baselines (whole
document, generic RAG) and a per-task breakdown.

## Applying the pattern to your own data

The GOÄ-specific parts are small. To adapt the repository to another dataset:

| Replace | With |
|---|---|
| `goae/ingest.py` | a parser for your source that produces records (`Entry`: code, text, points, notes) and calls `write_db` |
| `goae/db.py` → `get`, `price`, `check` | the operations your tasks need; keep `search` |
| `goae/tools.py` | tool names and one-line descriptions |
| `data/synonyms.tsv` | the vocabulary of your users |
| `bench/tasks.json` | 20–40 tasks with known answers from your domain |

Then run the benchmark and iterate. Search, MCP server, batch pipeline and benchmark
work unchanged.

**Example: briefing a coding agent.** The following prompt applies the pattern to a
completely different dataset — a parts catalog — and can be handed to any coding agent
(Claude Code, opencode, Codex, …) together with a link to this repository:

```text
Build a context-minimal lookup tool for our electronic parts catalog. Use the pattern
and project structure of https://github.com/markusrt/goae-lookup (read its README first).

Data
- catalog.csv (~400,000 rows, 300 MB): part_number, manufacturer, description,
  category, package, attributes (JSON: resistance, tolerance, voltage, …),
  price_breaks (JSON: quantity → unit price), stock, lifecycle (active/NRND/EOL)
- The file is far too large for any context window. Never put it, or large parts of
  it, into a prompt.

Questions the tool must answer
1. Which parts match a free-text description from a bill of materials,
   e.g. "10k resistor 0603 1%" or "100nF 50V X7R 0805"?
2. What does part X cost at quantity N (use the price breaks; compute in code)?
3. Is part X still active, and if not, which in-stock parts have the same category,
   package and key attributes?
4. Fill a BOM (bom.csv, one line per item) with a part number per line.

Requirements
- Parse the catalog once into SQLite with full-text search. Normalise units in
  descriptions and attributes (10k = 10 kΩ = 10000, 100nF = 0.1 µF).
- Operations: search, get, price, alternatives. Every answer at most ~10 short lines;
  search returns the top 5 as "part|key attributes|price@1|stock".
- Offer them as a CLI and as an MCP server with short tool descriptions.
- BOM filling runs as a script loop: one small LLM call per line (line + 5 candidates),
  result written to the output file immediately, resumable, codes outside the
  candidate list rejected.
- Write 40 tasks with known answers (25 tuning, 15 test) and a benchmark that reports
  tokens per answer and correctness. Iterate on output format, ranking and synonyms
  ("cap" → capacitor, "res" → resistor) until results are small and correct, using only
  the tuning tasks to decide on changes. Report the test results honestly.
- Write the results of each iteration to CONTEXT_STATS.md.
```

## Building the database from another source

By default the database is built from the official GOÄ text. As an example of a
different source, `goae/sources/abrechnungsstelle.py` builds it from the website
<https://abrechnungsstelle.com/goae/ziffern/>, which adds analogue codes and an exclusion
list per code. The website's content is not part of this repository; it is downloaded
when you build:

```bash
python -m goae build --source abrechnungsstelle      # → data/goae-abrechnungsstelle.db
python -m goae --db data/goae-abrechnungsstelle.db search "Ruhe-EKG"
export GOAE_DB=data/goae-abrechnungsstelle.db        # use it for the MCP server / agents
```

* Downloads 14 pages once (respects robots.txt, one request every 3 s), caches the HTML
  in `data/cache/` and writes a separate database. Both are git-ignored. `--refresh`
  downloads again.
* Takes service text, points, notes and exclusion codes per entry. The exclusion lists
  are converted into rule sentences, so `check` uses them as well. Sections, fee
  factors and general rules still come from the official text.
* Tested offline against a hand-written HTML fixture (`tests/fixtures/`). If the website
  changes its layout, `python -m goae.sources.abrechnungsstelle --debug 1-498` shows what
  the parser sees.
* Check the website's terms before using the data beyond personal purposes.

## Data and limitations

* Default source: the official GOÄ text (BGBl. I 1996, last amended 21.10.2019) from the
  Markdown mirror of gesetze-im-internet.de,
  [bundestag/gesetze](https://github.com/bundestag/gesetze) (`g/goae_1982/index.md`,
  Unlicense). German statutes are official works without copyright (§ 5 UrhG). A copy is
  in [`data/source/`](data/source/).
* The official text has no analogue codes (BÄK Analogbewertungen), no commentary and no
  case law, and the planned new GOÄ is not included.
* `check` evaluates explicit exclusion rules ("nicht neben", "nur … im Zusammenhang
  mit"). Frequency limits ("im Behandlungsfall nur einmal") are shown by `get` and
  `rules`, but not enforced.
* This is a technical example, not billing or legal advice.

## Layout

```text
goae/ingest.py      parse the official text → SQLite with full-text indexes
goae/db.py          search / get / price / check / rules, all returning short text
goae/tools.py       tool definitions (terse set, plus the verbose set used as a baseline)
goae/mcp_server.py  MCP server over stdio, no dependencies
goae/fill.py        batch pipeline (optional OpenAI-compatible LLM), next/set for agents
goae/sources/       optional adapters for other sources (abrechnungsstelle.com)
goae/__main__.py    CLI
bench/              tasks, benchmark, results, history
data/               source text and synonyms; databases and cache are generated
tests/              unittest suite (python -m unittest discover -s tests), HTML fixture
```

## License

Code: MIT. GOÄ text: public domain (see above).
