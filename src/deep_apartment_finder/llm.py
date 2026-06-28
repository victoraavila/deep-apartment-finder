"""LLM factory: OpenCode Go primary, Groq fallback.

The agent code only sees a chat model. The provider swap is an adapter
detail (ADR-002).

Sprint 5 reorders the providers: the daily driver is the OpenCode Go
subscription (the provider that hosts the models in our agent's
model zoo) and Groq is the on-demand fallback for when Go is
unavailable. The router is the same shape as before — primary then
optional fallback — only the assignment flipped.

Two layers:
- `build_chat_model(settings)` returns the primary model.
- `with_fallback(primary, fallback, settings)` wraps them in a router that
  retries the fallback on rate-limit / 429 from the primary.

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

# Canonical OpenCode Go base URL. The provider's docs publish
# `https://opencode.ai/zen/go/v1`; OpenCode Go's OpenAI-compatible
# `/chat/completions` lives under it. `ChatOpenAI` appends
# `/chat/completions` itself, so we do NOT include a trailing slash
# (a double-slash yields a 404).
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Construct the primary chat model (OpenCode Go).

    Raises if `OPENCODE_API_KEY` or `OPENCODE_BASE_URL` is unset. The
    base URL defaults to the canonical Go endpoint when the env var
    is blank, so a user with just a key can run with no extra config.
    """
    if not settings.has_opencode_primary:
        raise RuntimeError(
            "OPENCODE_API_KEY is not set. Configure it in .env "
            "(see .env.example) before running the orchestrator."
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.opencode_model,
        api_key=_to_secret(settings.opencode_api_key),
        base_url=settings.opencode_base_url or OPENCODE_GO_BASE_URL,
        max_retries=0,  # We do our own retry on the fallback path.
    )


def build_fallback_model(settings: Settings) -> BaseChatModel | None:
    """Construct the Groq fallback model, or `None` if unconfigured."""
    if not settings.has_groq_fallback:
        return None
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=settings.groq_model,
        api_key=_to_secret(settings.groq_api_key),
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
                "Primary LLM rate-limited; falling back to groq: %s", exc
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
                "Primary LLM rate-limited; falling back to groq: %s", exc
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
    3. The exception *message* contains a telltale token. We inspect
       every `arg` of the exception recursively (dicts and lists are
       stringified), NOT the full `repr(exc)` — otherwise a function
       name like `test_is_rate_limit_...` would match itself. The
       provider SDKs (Groq, OpenAI) raise with `args=(error_dict,)`,
       and the human-readable text lives inside the dict under
       `error.message`; without the recursive scan we miss every
       dict-shaped exception.

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

    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "", s.lower())

    def _scan_message(message: str) -> bool:
        n = _norm(message)
        return "ratelimit" in n or "quota" in n or "429" in n

    def _walk(node: Any, depth: int) -> bool:
        if depth > 6:
            return False
        if isinstance(node, str):
            return _scan_message(node)
        if isinstance(node, dict):
            for v in node.values():
                if _walk(v, depth + 1):
                    return True
            return False
        if isinstance(node, (list, tuple)):
            for v in node:
                if _walk(v, depth + 1):
                    return True
            return False
        return False

    if _walk(getattr(exc, "args", ()), 0):
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
    "OPENCODE_GO_BASE_URL",
    "build_chat_model",
    "build_chat_model_with_fallback",
    "build_fallback_model",
    "with_fallback",
    "ChatGeneration",
    "ChatResult",
]
