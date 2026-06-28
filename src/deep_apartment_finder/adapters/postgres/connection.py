"""asyncpg connection pool factory.

Sprint 1 uses a single process-scoped pool. We keep the surface small
(just `get_pool`) so the composition root has a single thing to close.
"""

from __future__ import annotations

import asyncpg

from deep_apartment_finder.config import Settings


async def get_pool(settings: Settings) -> asyncpg.Pool:
    """Create and return a connection pool to the configured Postgres DSN.

    The pool is configured conservatively for a personal-scale agent:
    a handful of concurrent connections, a generous idle timeout. Tune
    in Sprint 5 if/when this runs on a VPS.
    """
    return await asyncpg.create_pool(
        dsn=str(settings.postgres_dsn),
        min_size=1,
        max_size=5,
        command_timeout=30.0,
    )
