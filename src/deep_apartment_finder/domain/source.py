"""Listing sources. Sprint 1 only knows Fotocasa; later sprints add more."""

from __future__ import annotations

from enum import StrEnum


class Source(StrEnum):
    FOTOCASA = "fotocasa"
    # Sprint 3 — present in the enum now so dedup keys can be source-scoped.
    IDEALISTA = "idealista"
