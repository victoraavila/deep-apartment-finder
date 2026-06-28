"""Per-subagent filesystem routes (ADR-005).

Builds the `CompositeBackend` factory passed to `create_deep_agent`.

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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langgraph.store.memory import InMemoryStore

# Persistent subtrees (Sprint 1; Sprint 3/4/5 add /ranker/, /notifier/, /memories/).
PERSISTENT_ROUTES: tuple[str, ...] = (
    "/fotocasa_scraper/",
    "/orchestrator/",
)


def build_backend_factory(
    *,
    store: InMemoryStore | None = None,
) -> Callable[..., Any]:
    """Return a `(runtime) -> CompositeBackend` factory.

    The factory is what `create_deep_agent(backend=...)` expects. It
    receives a runtime object from LangGraph on each invocation and
    returns a fresh `CompositeBackend` for that run.
    """
    if store is None:
        store = InMemoryStore()

    def _factory(runtime: Any) -> CompositeBackend:
        # StateBackend is the ephemeral default; anything not matched by
        # a route below lands here and disappears at the end of the run.
        # Per deepagents 0.7.0+, StateBackend reads state via get_config()
        # and no longer takes `runtime`.
        default = StateBackend()
        # StoreBackend is the persistent store. The default namespace is
        # `("filesystem",)`, so all persistent files live under that
        # prefix in the store. Per deepagents 0.7.0+, pass the store
        # explicitly; StoreBackend no longer takes `runtime`.
        persistent: Any = StoreBackend(store=store)
        routes: dict[str, Any] = {prefix: persistent for prefix in PERSISTENT_ROUTES}
        return CompositeBackend(default=default, routes=routes)

    return _factory


__all__ = ["PERSISTENT_ROUTES", "build_backend_factory"]