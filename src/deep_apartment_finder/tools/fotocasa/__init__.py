"""Tools that wrap the `ScraperPort` for the `fotocasa_scraper` subagent.

The subagent gets:
- `search_listings` — returns cards as a JSON array.
- `fetch_listing`   — fetches and parses a single detail page.
- `save_snapshot`   — writes a debug snapshot to the subagent's
  `/fotocasa_scraper/raw/` subtree (ADR-005).

All tools are factory-built; they capture the `ScraperPort` in a closure
so the subagent does not have to know about concrete adapters.
"""

from __future__ import annotations
