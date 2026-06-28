"""LLM factory: ChatGroq primary, ChatOpenAI(opencode-go) fallback.

The agent code only sees a chat model. The provider swap is an adapter
detail (ADR-002).

Two layers:
- `build_chat_model(settings)` returns the primary model.
- `with_fallback(primary, fallback, settings)` wraps them in a router that
  retries the fallback on rate-limit / 429 from the primary.

In Sprint 1 we cannot easily reproduce a Groq rate-limit in CI, so the
fallback path is exercised by an injectable `FakeChatModel` (see tests).
The router is small and explicit; we do not pull in a third-party
fallback library.
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from deep_apartment_finder.config import Settings

logger = logging.getLogger(__name__)

# Anything pydantic accepts as an "API key" field for a chat model.
_ApiKey = str | None


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Construct the primary chat model. Raises if `GROQ_API_KEY` is unset."""
    if not settings.has_groq:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Configure it in .env "
            "(see .env.example) before running the orchestrator."
        )
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=settings.groq_model,
        api_key=_to_secret(settings.groq_api_key),
        max_retries=0,  # We do our own retry on the fallback path.
    )


def build_fallback_model(settings: Settings) -> BaseChatModel | None:
    """Construct the opencode-go fallback model, or `None` if unconfigured."""
    if not settings.has_opencode_fallback:
        return None
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.opencode_model,
        api_key=_to_secret(settings.opencode_api_key),
        base_url=settings.opencode_base_url,
        max_retries=0,
    )


def _to_secret(value: _ApiKey) -> Any:
    """Coerce a plain str into a pydantic `SecretStr` (the type LangChain
    provider integrations expect for API keys). Returns `None` for None."""
    if value is None:
        return None
    from pydantic import SecretStr

    return SecretStr(value)


class _FallbackRouter(BaseChatModel):
    """A chat model that delegates to `primary`, and on rate-limit falls back
    to `fallback`. The two underlying models must be interchangeable in
    interface (both `BaseChatModel`).

    Implementation note: we keep `primary` and `fallback` as pydantic
    fields typed as `BaseChatModel` so the type-checker is happy. For
    tests, we use a test double whose class is registered via
    `__init_subclass__` — see `_register_fake_chat_model` below.
    """

    primary: BaseChatModel
    fallback: BaseChatModel | None = None

    @property
    def _llm_type(self) -> str:
        return "daf-fallback-router"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            return self.primary._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
        except Exception as exc:  # noqa: BLE001
            if self.fallback is None or not _is_rate_limit(exc):
                raise
            logger.warning(
                "Primary LLM rate-limited; falling back to opencode-go: %s", exc
            )
            return self.fallback._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            return await self.primary._agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
        except Exception as exc:  # noqa: BLE001
            if self.fallback is None or not _is_rate_limit(exc):
                raise
            logger.warning(
                "Primary LLM rate-limited; falling back to opencode-go: %s", exc
            )
            return await self.fallback._agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )


def _is_rate_limit(exc: BaseException) -> bool:
    """Best-effort detection of a 429 / quota / rate-limit exception.

    Heuristic, in order of confidence:

    1. `response.status_code == 429` (the gold standard; covers httpx/requests)
    2. The class name itself contains a telltale token
       (`ratelimit`, `toomanyrequests`).
    3. The exception *message* contains a telltale token, but we look only at
       the message via `args`, NOT at the full `repr(exc)` — otherwise a
       function name like `test_is_rate_limit_...` would match itself.

    Library-specific subclass checks would couple us to internals; this is
    conservative and explicit about what triggers the swap.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        return True

    cls_name = type(exc).__name__.lower()
    if "ratelimit" in cls_name or "toomanyrequests" in cls_name:
        return True

    # Look at the message args only — NOT the repr — to avoid matching on
    # our own function name. Also strip non-alphanumerics so "rate-limit",
    # "rate_limit", and "rate limit" all collapse to "ratelimit".
    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "", s.lower())

    for arg in getattr(exc, "args", ()):
        if not isinstance(arg, str):
            continue
        n = _norm(arg)
        if "ratelimit" in n or "quota" in n or "429" in n:
            return True

    return False


def with_fallback(primary: BaseChatModel, fallback: BaseChatModel | None) -> BaseChatModel:
    """Wrap `primary` with `fallback` for rate-limit swap. If `fallback` is
    `None`, returns `primary` unchanged. Always returns a `BaseChatModel`."""
    if fallback is None:
        return primary
    return _FallbackRouter(primary=primary, fallback=fallback)


def build_chat_model_with_fallback(settings: Settings) -> BaseChatModel:
    """Convenience: build primary + optional fallback and wrap them."""
    primary = build_chat_model(settings)
    fallback = build_fallback_model(settings)
    return with_fallback(primary, fallback)


__all__ = [
    "AIMessage",
    "BaseChatModel",
    "build_chat_model",
    "build_chat_model_with_fallback",
    "build_fallback_model",
    "with_fallback",
    "ChatGeneration",
    "ChatResult",
]
