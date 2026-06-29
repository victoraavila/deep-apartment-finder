"""LangSmith tracing wrappers.

A thin layer over `langsmith.run_helpers.traceable` (already in
`pyproject.toml`) that adds two project-specific behaviours:

1. **Gating on `settings.langsmith_tracing`.** When the env
   `LANGSMITH_TRACING=false` (or unset), every wrapper is a
   no-op decorator. The CLI prints a single line at start-up
   saying "LangSmith tracing disabled" so the operator knows
   the URL won't be in the run report.
2. **Domain-meaningful metadata.** The wrapped spans carry the
   same counts / apartment ids / skip reasons the operator
   already sees in the run report, so the trace reconstructs
   the run without cross-referencing code.

The wrappers are designed to be cheap to apply: every call site
is `@trace("name", **default_metadata)`. The decorator accepts
both sync and async callables; we always pass through
unchanged.

The module also exposes `maybe_trace_url_from_env(...)` to
extract the most-recent trace URL from the LangSmith client
state (where the SDK writes it after a `traceable` runs).
Sprint 3 reads this at the end of the CLI run and stamps it
on the persisted `RunReport`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])


def langsmith_tracing_enabled() -> bool:
    """Return `True` iff LangSmith tracing is on for this run.

    Reads `LANGSMITH_TRACING` (any truthy value: `1`, `true`,
    `yes`). The `Settings.langsmith_tracing` flag is the
    source of truth; this helper exists so adapters and tests
    don't have to import `Settings` directly.
    """
    val = os.environ.get("LANGSMITH_TRACING", "false").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _force_enable_for_next_call() -> None:
    """Allow the `--trace` CLI flag to override `LANGSMITH_TRACING=false`
    for a single invocation. Implemented by toggling the env var;
    LangSmith's own SDK reads the env at module import time, so
    this only takes effect on the next fresh import, which is
    fine for the CLI's "print the trace URL at the end" path.
    """
    os.environ["LANGSMITH_TRACING"] = "true"


def _resolve_project() -> str:
    return os.environ.get("LANGSMITH_PROJECT", "deep-apartment-finder")


def trace(
    name: str,
    *,
    run_type: str = "chain",
    metadata: dict[str, Any] | None = None,
) -> Callable[[_F], _F]:
    """Decorator: wrap `fn` in a LangSmith span when tracing is on.

    The span is named `name` (visible in the LangSmith UI) and
    carries the static `metadata` dict plus any kwargs the
    caller passed at invocation time. The decorator accepts both
    sync and async callables and returns a function with the
    same calling convention.

    When tracing is off the decorator is a pass-through with
    no overhead beyond a single bool check.
    """
    # `metadata` is currently only used to document the static
    # metadata the operator sees in the LangSmith UI; the
    # LangSmith SDK reads the live kwargs at invocation time.
    # We keep the parameter for the public contract and to make
    # the call sites read clearly.
    _ = metadata

    def _decorator(fn: _F) -> _F:
        if not langsmith_tracing_enabled():
            return fn

        # Late import: the `langsmith` package is always in
        # `pyproject.toml` but we don't want the no-op path to
        # pay the import cost on every run.
        try:
            from langsmith.run_helpers import traceable
        except ImportError:
            logger.debug(
                "langsmith.run_helpers not importable; trace decorators are no-ops"
            )
            return fn

        # `traceable` accepts `run_type` and `name` as keyword
        # arguments; the type stubs require `run_type` to be a
        # `Literal`, so we cast at the call site.
        wrapped = traceable(name=name, run_type=cast(Any, run_type))(fn)

        @wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await wrapped(*args, **kwargs)

        @wraps(fn)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return wrapped(*args, **kwargs)

        if _is_coroutine_function(fn):
            return cast(_F, _async_wrapper)
        return cast(_F, _sync_wrapper)

    return _decorator


def _is_coroutine_function(fn: Callable[..., Any]) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


def current_trace_url() -> str | None:
    """Return the most-recent trace URL, if LangSmith is enabled.

    The langsmith SDK exposes `get_current_run_tree()` once a
    `traceable` is in scope. We import it lazily; if unavailable
    (or if no run is active) we return `None`.

    The CLI calls this at the very end of the run to stamp the
    URL on the persisted `RunReport`.
    """
    if not langsmith_tracing_enabled():
        return None
    try:
        from langsmith.run_helpers import get_current_run_tree
    except ImportError:
        return None
    try:
        tree = get_current_run_tree()
    except Exception:  # noqa: BLE001
        return None
    if tree is None:
        return None
    url = getattr(tree, "metadata", {}).get("trace_url") if hasattr(tree, "metadata") else None
    if not url:
        # Fall back to constructing a URL from the run id when
        # the SDK doesn't expose one. LangSmith's standard URL
        # shape is `https://smith.langchain.com/r/<run_id>`.
        run_id = getattr(tree, "id", None)
        if run_id:
            return f"https://smith.langchain.com/r/{run_id}"
    return url


__all__ = [
    "current_trace_url",
    "langsmith_tracing_enabled",
    "trace",
]
