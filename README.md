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

## Completing a table: use a pipeline, not a conversation

```bash
# candidates only (no LLM): adds a goae_candidates column for review
python -m goae fill examples/leistungen.csv

# with a local model via any OpenAI-compatible endpoint (Ollama, llama.cpp, LM Studio, vLLM)
python -m goae fill examples/leistungen.csv --llm http://localhost:11434/v1 --model qwen2.5:7b-instruct
```

Each row gets a prompt of ~140 tokens (row + 5 candidates) and must answer with JSON.
A code outside the candidate list, or below `--min-confidence`, is left empty for human
review. If you prefer to stay inside your agent: let it call `goae_search` per row and
write each result into the file straight away — but a script loop is more robust,
especially with small local models.

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

## Data and limitations

* Source: the official GOÄ text (BGBl. I 1996, last amended 21.10.2019), via the
  Markdown mirror of gesetze-im-internet.de at
  [bundestag/gesetze](https://github.com/bundestag/gesetze) (`g/goae_1982/index.md`,
  Unlicense). German statutes are official works without copyright (§ 5 UrhG). A copy is
  in [`data/source/`](data/source/).
* Not included: analogue codes (BÄK Analogbewertungen), commentary, case law, the
  planned new GOÄ. The page the original question referred to
  (abrechnungsstelle.com) contains such commentary; the same pattern applies if you
  parse it into the schema in `goae/ingest.py`.
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
goae/__main__.py    CLI
bench/              tasks, benchmark loop, results, history
data/               source text, synonyms; goae.db is generated
tests/              unittest suite (python -m unittest discover -s tests)
```

## License

Code: MIT. Data: see above (public domain).
