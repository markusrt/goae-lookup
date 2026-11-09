## GOÄ lookups

Never read the GOÄ source text or the database directly. Use the CLI; every command
prints a few lines only.

- `python -m goae search "<service, German keywords>"` → `code|points|text`, top 5.
  If nothing fits, search again with the medical term (e.g. "Magnetresonanztomographie"
  instead of "MRT") rather than asking for more results.
- `python -m goae get <code>` → full text, points, fees at 1.0x / threshold / max, own rules.
- `python -m goae check <code> <code> …` → exclusion rules between codes billed together.
- `python -m goae rules <code>` → general rules that mention the code.

To complete a table, do not keep results in the conversation: run
`python -m goae fill <file.csv>` or write each row's result into the file right away.
