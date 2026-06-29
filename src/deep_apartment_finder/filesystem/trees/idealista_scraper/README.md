# idealista_scraper — subagent filesystem

This is the persistent subtree of the `idealista_scraper` subagent.
Writes under any of these paths are routed to the persistent
`StoreBackend`; writes outside the prefix land in ephemeral
`StateBackend` and are lost when the run finishes (ADR-005).

## Folders

- `raw/` — captured HTML pages (`search_page1.html`, etc.) and raw
  JSON snapshots for replay & debug.
- `extracted/` — normalized JSON the subagent produced before
  handing off to `ingest_apartment`.
- `cache/` — URLs already seen this run (pre-DB cross-run dedup).
- `selectors/` — editable CSS selectors (no recompile).
- `logs/` — scraper run logs.

The `save_snapshot` tool writes only to `raw/`. The other folders
are populated by the subagent's filesystem tools.
