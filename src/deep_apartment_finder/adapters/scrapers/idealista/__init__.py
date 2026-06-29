"""Idealista scraper adapter (Sprint 3, Pillar E).

Mirrors the layout of `adapters/scrapers/fotocasa/`. The strategy is
documented in `scraper.py` — `curl_cffi` impersonating Chrome 131
against the SSR search HTML, with `fetch_listing` returning card
data only (no second HTTP call, because DataDome blocks the detail
endpoint for non-browser clients).
"""

from .api import card_to_apartment, parse_search_page
from .scraper import IdealistaScraper

__all__ = ["IdealistaScraper", "card_to_apartment", "parse_search_page"]
