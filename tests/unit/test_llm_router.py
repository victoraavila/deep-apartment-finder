"""LLM router tests.

The router wraps a primary model and a fallback model; on a 429 / rate-limit
from the primary, it transparently retries on the fallback. These tests use
in-process fake chat models, not the real providers.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage

from deep_apartment_finder.llm import (
    _is_rate_limit,
    with_fallback,
)


class RateLimited(Exception):
    def __init__(self) -> None:
        self.response = type("R", (), {"status_code": 429})()


class _BoomThenOkChatModel(BaseChatModel):
    """A real BaseChatModel that raises RateLimited on the first call
    and delegates to an inner FakeListChatModel thereafter."""

    inner: BaseChatModel
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "boom-then-ok"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            raise RateLimited()
        return self.inner._generate(messages, stop, run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            raise RateLimited()
        return await self.inner._agenerate(messages, stop, run_manager, **kwargs)


@pytest.mark.asyncio
async def test_router_falls_back_to_fallback_on_rate_limit():
    primary = _BoomThenOkChatModel(inner=FakeListChatModel(responses=["ignored"]))
    fallback = FakeListChatModel(responses=["from-fallback"])
    router = with_fallback(primary, fallback)

    result = await router._agenerate([HumanMessage(content="hi")])
    assert result.generations[0].message.content == "from-fallback"
    assert primary.call_count == 1


@pytest.mark.asyncio
async def test_router_returns_primary_when_no_rate_limit():
    primary = FakeListChatModel(responses=["from-primary"])
    fallback = FakeListChatModel(responses=["from-fallback"])
    router = with_fallback(primary, fallback)

    result = await router._agenerate([HumanMessage(content="hi")])
    assert result.generations[0].message.content == "from-primary"


def test_with_fallback_none_returns_primary():
    primary = FakeListChatModel(responses=["x"])
    out = with_fallback(primary, None)
    assert out is primary


def test_is_rate_limit_detects_various_signals():
    class R:
        response = type("S", (), {"status_code": 429})()

    class Exc:
        response = type("S", (), {"status_code": 500})()

    # response.status_code == 429
    assert _is_rate_limit(R()) is True
    # response.status_code == 500, no rate-limit signal
    assert _is_rate_limit(Exc()) is False

    # Message-based detection
    assert _is_rate_limit(Exception("429 too many requests")) is True
    assert _is_rate_limit(Exception("Rate limit reached")) is True
    assert _is_rate_limit(Exception("rate-limit hit")) is True
    assert _is_rate_limit(Exception("rate_limit hit")) is True
    assert _is_rate_limit(Exception("Quota exceeded for model")) is True
    assert _is_rate_limit(Exception("Connection refused")) is False

    # Class-name detection (telltale tokens in the class itself).
    class RateLimitError(Exception):
        pass

    class TooManyRequestsError(Exception):
        pass

    assert _is_rate_limit(RateLimitError()) is True
    assert _is_rate_limit(TooManyRequestsError()) is True

    # The function name of the caller must NOT poison the result.
    # (Regression: a previous heuristic used repr(exc), which contains the
    # function name "test_is_rate_limit_..." and matched itself.)
    def innocent_function(_e):
        return None

    class PlainError(Exception):
        pass

    plain = PlainError()
    # Run through the same path the router uses — the function name on the
    # stack should not flip the verdict.
    innocent_function(plain)
    assert _is_rate_limit(plain) is False
