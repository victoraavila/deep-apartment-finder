"""LangSmith tracing wrappers.

A thin layer over `langsmith.run_helpers.traceable` (already in
`pyproject.toml`) that adds two project-specific behaviours:

1. **Gating on `LANGSMITH_API_KEY`.** The CLI loads settings from
   `.env` and mirrors them into process env vars before a run starts.
   When a LangSmith API key is configured, tracing is mandatory; when
   no key is configured, every wrapper is a no-op.
2. **Domain-meaningful metadata.** The wrapped spans carry the
   same counts / apartment ids / skip reasons the operator
   already sees in the run report, so the trace reconstructs
   the run without cross-referencing code.

The wrappers are designed to be cheap to apply: every call site
is `@trace("name", **default_metadata)`. The decorator accepts
both sync and async callables; we always pass through
unchanged.

The module also exposes `current_trace_url()` to extract the
current or most-recent trace URL. Sprint 3 reads this before
persisting the CLI run report and stamps it on the JSON.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar, cast

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])
_TRUTHY = {"1", "true", "yes", "on"}
_LAST_TRACE_URL: str | None = None


def configure_langsmith_from_settings(settings: Any, *, force: bool = False) -> bool:
    """Normalize LangSmith SDK env vars from loaded application settings.

    The CLI loads `.env` through Pydantic, but the LangSmith SDK reads
    process env vars. Keep those two worlds in sync and make tracing
    mandatory when a LangSmith API key is configured. Without an API key
    tracing remains disabled; `force` is kept for the CLI's `--trace`
    flag but cannot invent credentials.
    """
    api_key = getattr(settings, "langsmith_api_key", None)
    if not api_key:
        os.environ["LANGSMITH_TRACING"] = "false"
        return False

    os.environ["LANGSMITH_API_KEY"] = str(api_key)
    project = getattr(settings, "langsmith_project", None)
    if project:
        os.environ["LANGSMITH_PROJECT"] = str(project)
    if force or api_key:
        os.environ["LANGSMITH_TRACING"] = "true"
    return True


def langsmith_tracing_enabled() -> bool:
    """Return `True` iff LangSmith tracing is on for this run.

    Requires `LANGSMITH_API_KEY` and a truthy `LANGSMITH_TRACING`
    value (`1`, `true`, `yes`, `on`). The CLI sets
    `LANGSMITH_TRACING=true` automatically when the key is present.
    """
    if not os.environ.get("LANGSMITH_API_KEY"):
        return False
    val = os.environ.get("LANGSMITH_TRACING", "false").strip().lower()
    return val in _TRUTHY


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
    def _decorator(fn: _F) -> _F:
        wrapped: Callable[..., Any] | None = None

        def _wrapped() -> Callable[..., Any] | None:
            nonlocal wrapped
            if not langsmith_tracing_enabled():
                return None
            if wrapped is not None:
                return wrapped
            # Late import: the `langsmith` package is always in
            # `pyproject.toml` but we don't want the no-op path to
            # pay the import cost on every run.
            try:
                from langsmith.run_helpers import traceable
            except ImportError:
                logger.debug(
                    "langsmith.run_helpers not importable; trace decorators are no-ops"
                )
                return None

            kwargs: dict[str, Any] = {
                "name": name,
                "run_type": cast(Any, run_type),
            }
            if metadata:
                kwargs["metadata"] = metadata
            wrapped = traceable(**kwargs)(fn)
            return wrapped

        @wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            traced = _wrapped()
            if traced is None:
                return await fn(*args, **kwargs)
            return await traced(*args, **kwargs)

        @wraps(fn)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            traced = _wrapped()
            if traced is None:
                return fn(*args, **kwargs)
            return traced(*args, **kwargs)

        if _is_coroutine_function(fn):
            return cast(_F, _async_wrapper)
        return cast(_F, _sync_wrapper)

    return _decorator


def _is_coroutine_function(fn: Callable[..., Any]) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


def _trace_url_from_tree(tree: Any) -> str | None:
    metadata = getattr(tree, "metadata", None)
    if isinstance(metadata, Mapping):
        url = metadata.get("trace_url")
        if url:
            return str(url)
    run_id = getattr(tree, "id", None)
    if run_id:
        return f"https://smith.langchain.com/r/{run_id}"
    return None


@contextmanager
def root_trace(
    name: str,
    *,
    run_type: str = "chain",
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any | None]:
    """Open a parent LangSmith span for a CLI run when tracing is enabled."""
    global _LAST_TRACE_URL
    if not langsmith_tracing_enabled():
        yield None
        return
    try:
        from langsmith.run_helpers import trace as langsmith_trace
    except ImportError:
        logger.debug("langsmith.run_helpers not importable; root trace is disabled")
        yield None
        return

    try:
        manager = langsmith_trace(
            name,
            run_type=cast(Any, run_type),
            metadata=metadata,
            project_name=_resolve_project(),
        )
        tree = manager.__enter__()
    except Exception as init_exc:  # noqa: BLE001
        logger.warning(
            "LangSmith root trace failed; continuing without trace: %s",
            init_exc,
        )
        yield None
        return

    exc_type: type[BaseException] | None = None
    active_exc: BaseException | None = None
    tb: Any = None
    try:
        _LAST_TRACE_URL = _trace_url_from_tree(tree)
        yield tree
    except BaseException as caught:
        exc_type = type(caught)
        active_exc = caught
        tb = caught.__traceback__
        raise
    finally:
        _LAST_TRACE_URL = _trace_url_from_tree(tree) or _LAST_TRACE_URL
        try:
            manager.__exit__(exc_type, active_exc, tb)
        except Exception as close_exc:  # noqa: BLE001
            logger.warning("LangSmith root trace close failed: %s", close_exc)


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
        return _LAST_TRACE_URL
    return _trace_url_from_tree(tree) or _LAST_TRACE_URL


__all__ = [
    "configure_langsmith_from_settings",
    "current_trace_url",
    "langsmith_tracing_enabled",
    "root_trace",
    "trace",
]
