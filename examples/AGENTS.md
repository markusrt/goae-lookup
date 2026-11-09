## GOÄ lookups

Never read the GOÄ source text or the database directly. Use the CLI; every command
prints a few lines only.

- `python -m goae search "<service, German keywords>"` → `code|points|text`, top 5.
  If nothing fits, search again with the medical term (e.g. "Magnetresonanztomographie"
  instead of "MRT") rather than asking for more results.
- `python -m goae get <code>` → full text, points, fees at 1.0x / threshold / max, own rules.
- `python -m goae check <code> <code> …` → exclusion rules between codes billed together.
- `python -m goae rules <code>` → general rules that mention the code.

To complete a table, do not keep results in the conversation and do not open the CSV:
run `python -m goae next <file.csv>` (10 open rows with 3 candidates each), then save the
whole batch with `python -m goae set <file.csv> 1=250 2=651 3=?` (`?` = nothing fits),
which prints the next batch. Repeat until it prints "done". Results are written to
`<file>.goae.csv`. Or run `python -m goae fill <file.csv> --llm-cmd "<cli that reads stdin>"`.
