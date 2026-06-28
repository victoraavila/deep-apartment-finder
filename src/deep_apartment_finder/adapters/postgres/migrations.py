"""Idempotent forward-only migration runner.

Sprint 1: applies every `.sql` file in `migrations/` in lexicographic order,
exactly once, recording the applied version in a `_migrations` table that
the runner creates itself on first run. Each migration runs in a single
transaction. Already-applied files are skipped without re-running.

This is intentionally tiny — no downgrade path, no checksum verification.
Adding a column or index is a new migration, not an edit to a prior one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)


MIGRATIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS _migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: str  # filename, e.g. "001_init_apartments.sql"


def discover_migrations(migrations_dir: Path) -> list[Path]:
    """Return every `.sql` file in `migrations_dir`, sorted by filename."""
    if not migrations_dir.exists():
        return []
    return sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())


def _version_from_path(path: Path) -> str:
    return path.name


async def apply_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> list[AppliedMigration]:
    """Apply any not-yet-applied migrations. Returns the list of those just
    applied by this call (already-applied ones are not in the result)."""
    discovered = discover_migrations(migrations_dir)
    async with pool.acquire() as conn:
        await conn.execute(MIGRATIONS_TABLE_DDL)
        rows = await conn.fetch("SELECT version FROM _migrations")
        already = {r["version"] for r in rows}

    newly_applied: list[AppliedMigration] = []
    for path in discovered:
        version = _version_from_path(path)
        if version in already:
            logger.debug("migration %s already applied; skipping", version)
            continue
        sql = path.read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _migrations(version) VALUES ($1)", version
                )
        logger.info("applied migration %s", version)
        newly_applied.append(AppliedMigration(version=version))
    return newly_applied


def summarize(applied: Iterable[AppliedMigration]) -> str:
    applied = list(applied)
    if not applied:
        return "no new migrations"
    return "applied: " + ", ".join(m.version for m in applied)
