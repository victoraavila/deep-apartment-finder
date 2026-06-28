"""Migration runner tests.

We can't assume Postgres is available in unit tests, so we test the
*behaviour* of the runner with a recording asyncpg fake. The fake records
the SQL it was asked to execute and lets the test seed the already-applied
set to verify skip-vs-apply logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deep_apartment_finder.adapters.postgres.migrations import (
    MIGRATIONS_TABLE_DDL,
    apply_migrations,
    discover_migrations,
)


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, applied: set[str], executed: list[str], inserts: list[tuple[str]]) -> None:
        self._applied = applied
        self._executed = executed
        self._inserts = inserts

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> str:
        self._executed.append(sql)
        if sql == MIGRATIONS_TABLE_DDL.strip():
            return ""
        # Treat as "INSERT INTO _migrations"
        if "INSERT INTO _migrations" in sql and args:
            self._applied.add(args[0])
            self._inserts.append((args[0],))
        return ""

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "SELECT version FROM _migrations" in sql:
            return [{"version": v} for v in sorted(self._applied)]
        return []


class _FakePool:
    def __init__(self, applied: set[str], executed: list[str], inserts: list[tuple[str]]) -> None:
        self._applied = applied
        self._executed = executed
        self._inserts = inserts

    def acquire(self):
        conn = _FakeConn(self._applied, self._executed, self._inserts)

        class _Ctx:
            async def __aenter__(self_inner) -> _FakeConn:  # noqa: N805
                return conn

            async def __aexit__(self_inner, *exc: Any) -> None:
                return None

        return _Ctx()


def _write_migration(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_discover_migrations_returns_sorted_sql_files(tmp_path: Path):
    _write_migration(tmp_path, "002_add_index.sql", "CREATE INDEX x;")
    _write_migration(tmp_path, "001_init.sql", "CREATE TABLE a;")
    _write_migration(tmp_path, "README.md", "ignore me")
    found = discover_migrations(tmp_path)
    assert [p.name for p in found] == ["001_init.sql", "002_add_index.sql"]


@pytest.mark.asyncio
async def test_apply_migrations_runs_each_unapplied_file(tmp_path: Path):
    _write_migration(tmp_path, "001_init.sql", "CREATE TABLE a;")
    _write_migration(tmp_path, "002_idx.sql", "CREATE INDEX b ON a(x);")

    applied: set[str] = set()
    executed: list[str] = []
    inserts: list[tuple[str]] = []
    pool = _FakePool(applied, executed, inserts)

    result = await apply_migrations(pool, tmp_path)
    assert [m.version for m in result] == ["001_init.sql", "002_idx.sql"]
    # DDL for the bookkeeping table is created
    assert any("CREATE TABLE IF NOT EXISTS _migrations" in s for s in executed)
    # Both migration bodies are executed
    assert any("CREATE TABLE a" in s for s in executed)
    assert any("CREATE INDEX b" in s for s in executed)
    # Both are recorded
    assert inserts == [("001_init.sql",), ("002_idx.sql",)]


@pytest.mark.asyncio
async def test_apply_migrations_skips_already_applied(tmp_path: Path):
    _write_migration(tmp_path, "001_init.sql", "CREATE TABLE a;")
    _write_migration(tmp_path, "002_idx.sql", "CREATE INDEX b ON a(x);")

    applied: set[str] = {"001_init.sql"}  # already there
    executed: list[str] = []
    inserts: list[tuple[str]] = []
    pool = _FakePool(applied, executed, inserts)

    result = await apply_migrations(pool, tmp_path)
    assert [m.version for m in result] == ["002_idx.sql"]
    # 001 body must NOT run
    assert not any("CREATE TABLE a;" in s for s in executed)
    # 002 body runs once
    assert any("CREATE INDEX b" in s for s in executed)


@pytest.mark.asyncio
async def test_apply_migrations_is_idempotent(tmp_path: Path):
    _write_migration(tmp_path, "001_init.sql", "CREATE TABLE a;")
    applied: set[str] = set()
    executed: list[str] = []
    inserts: list[tuple[str]] = []

    # First run applies the migration.
    p1 = _FakePool(applied, executed, inserts)
    first = await apply_migrations(p1, tmp_path)
    assert [m.version for m in first] == ["001_init.sql"]

    # Second run finds the bookkeeping table populated, applies nothing.
    p2 = _FakePool(applied, executed, inserts)
    second = await apply_migrations(p2, tmp_path)
    assert second == []
