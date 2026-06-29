"""Per-subagent filesystem routes (ADR-005).

Builds the `CompositeBackend` passed to `create_deep_agent`.

Layered isolation (per ADR-005):

1. **Hard (backend)** — `CompositeBackend` routes writes under
   `/fotocasa_scraper/` and `/orchestrator/` to a persistent `StoreBackend`.
   Everything else goes to an ephemeral `StateBackend`.
2. **Boundary (tools)** — Each subagent receives only the tools it needs.
   Tools that write files force a prefix to the subagent's own subtree.
3. **Soft (prompt)** — The subagent's system prompt documents its allowed
   subtree and the purpose of each folder.

Sprint 1 uses `InMemoryStore`; Sprint 5 swaps in `PostgresStore` for
cross-process persistence.

Deep Agents 0.7.0 deprecates the `(runtime) -> Backend` factory shape in
favour of passing a `BackendProtocol` instance directly. The current pin
(`>=0.6.12,<0.7`) still accepts the factory, but we build the instance
here so the migration to 0.7 is a one-line bump rather than a refactor.
"""

from __future__ import annotations

from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import BackendProtocol
from langgraph.store.memory import InMemoryStore

# Persistent subtrees (Sprint 1 + Sprint 2; Sprint 3/4/5 add /memories/).
PERSISTENT_ROUTES: tuple[str, ...] = (
    "/fotocasa_scraper/",
    "/orchestrator/",
    "/researcher/",
    "/ranker/",
    "/notifier/",
)


def build_backend(
    *,
    store: InMemoryStore | None = None,
) -> CompositeBackend:
    """Return a `CompositeBackend` instance ready to hand to `create_deep_agent`.

    - `StateBackend` (default) is ephemeral per LangGraph run.
    - `StoreBackend` (persistent) backs every route in `PERSISTENT_ROUTES`,
      sharing a single `InMemoryStore` namespace. Sprint 5 swaps the
      in-memory store for `PostgresStore` to persist across processes.
    """
    if store is None:
        store = InMemoryStore()

    default: BackendProtocol = StateBackend()
    persistent: BackendProtocol = StoreBackend(store=store)
    routes: dict[str, BackendProtocol] = {
        prefix: persistent for prefix in PERSISTENT_ROUTES
    }
    return CompositeBackend(default=default, routes=routes)


__all__ = ["PERSISTENT_ROUTES", "build_backend"]
