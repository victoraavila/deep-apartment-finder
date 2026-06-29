"""`search_listings` tool for the Idealista subagent.

Calls the injected `ScraperPort.search_listings(...)` and returns
the cards as a JSON array. The subagent iterates the result,
decides which cards are worth ingesting, and uses `ingest_apartment`
on the full listings.

This is a verbatim copy of the Fotocasa tool (see
`tools/fotocasa/search_listings.py`) — both adapters implement the
same `ScraperPort`. The only difference is the closure variable
that captures the bound scraper instance.
"""
