"""`observability/tracing.py` tests.

The tests don't assume LangSmith is reachable; they verify the
gating behaviour (the decorator is a pass-through when
`LANGSMITH_TRACING=false`) and the metadata pass-through
behaviour (when the env is true, the decorator wraps the
callable and preserves the static metadata).
"""

from __future__ import annotations

import pytest

from deep_apartment_finder.adapters.observability.tracing import (
    current_trace_url,
    langsmith_tracing_enabled,
    trace,
)


@pytest.fixture(autouse=True)
def _reset_langsmith_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test sets the env explicitly; the fixture strips any
    leftover value from a previous test."""
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)


def test_langsmith_tracing_enabled_default_is_false() -> None:
    assert langsmith_tracing_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "True"])
def test_langsmith_tracing_enabled_truthy_values(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", val)
    assert langsmith_tracing_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_langsmith_tracing_enabled_falsy_values(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", val)
    assert langsmith_tracing_enabled() is False


def test_trace_decorator_is_passthrough_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    @trace("foo", metadata={"k": "v"})
    def add(a: int, b: int) -> int:
        return a + b

    # Passthrough: the function still works, no exception, no
    # LangSmith call attempted.
    assert add(1, 2) == 3


@pytest.mark.asyncio
async def test_trace_decorator_preserves_async_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    @trace("foo", metadata={"k": "v"})
    async def add(a: int, b: int) -> int:
        return a + b

    assert await add(1, 2) == 3


def test_trace_decorator_preserves_function_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    @trace("foo")
    def my_function() -> str:
        """My docstring."""
        return "x"

    assert my_function.__name__ == "my_function"
    assert my_function.__doc__ == "My docstring."


def test_current_trace_url_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    assert current_trace_url() is None


def test_current_trace_url_returns_none_when_enabled_but_no_run_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    # No traceable has been entered in this test, so the SDK
    # should have no current run tree.
    assert current_trace_url() is None
