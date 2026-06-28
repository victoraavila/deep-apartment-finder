# `/fotocasa_scraper/` subtree — contract

The `fotocasa_scraper` subagent owns everything under this prefix.
ADR-005 documents the three layers of isolation; this README documents
the *shape* the subagent is expected to leave behind, so a human
maintainer and the LLM agree on the same contract.

## Folders

| Folder       | Purpose                                                                              | Lifecycle |
| ------------ | ------------------------------------------------------------------------------------ | --------- |
| `raw/`       | HTML / JSON snapshots of search results and detail pages, for replay & debug.       | Persistent |
| `extracted/` | Normalized JSON before ingest, the LLM's intermediate view of a listing.             | Persistent |
| `cache/`     | URLs already seen (pre-DB cross-run dedup hint; the DB is the source of truth).      | Persistent |
| `selectors/` | Editable CSS / JSON-LD selectors — see `adapters/scrapers/fotocasa/selectors.py`.    | Persistent |
| `logs/`      | Per-run scraper logs.                                                                | Persistent |

## Why persistent?

Cross-run replay. If a parse changes and we want to re-ingest a run from
disk instead of re-hitting Fotocasa, the raw HTML is here.

The persistent store is **not** a source of truth. Postgres is. The
subagent must not rely on the file system to dedup — that's the
repository's job.

## What lives here in practice?

In Sprint 1 the subagent typically writes one file per fetched detail
page (`raw/<external_id>.html`) and one extracted JSON
(`extracted/<external_id>.json`). Other folders are reserved for
Sprint 2/3.
