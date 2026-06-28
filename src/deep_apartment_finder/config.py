"""Application settings, loaded from environment + .env.

Validated up front so `python -m deep_apartment_finder` fails fast with a
helpful error if the env is incomplete, rather than failing deep in
asyncpg or at the LLM call.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---------------------------------------------------------
    # The DSN is stored as a plain string because (a) asyncpg accepts a
    # `str` directly, and (b) pydantic's `PostgresDsn` type only enforces
    # syntactic validity on assignment, not on the .env file — and we
    # want to defer the validity check to the moment we open the pool.
    postgres_dsn: str = Field(
        default="postgresql://apartments:apartments@localhost:5432/apartments",
        description="asyncpg-compatible DSN",
    )

    # --- LLM primary (OpenCode Go, OpenAI-compatible) -------------------
    # Default model is qwen3.6-plus (closest analog to what we used to
    # run on Groq's free tier); users can override via OPENCODE_MODEL.
    # Available OpenAI-compatible Go models: glm-5, glm-5.1, kimi-k2.5,
    # kimi-k2.6, deepseek-v4-pro, deepseek-v4-flash, qwen3.5-plus,
    # qwen3.6-plus, mimo-v2-pro, mimo-v2-omni. (The MiniMax M2.x line
    # uses the Anthropic protocol and is not addressable here.)
    opencode_api_key: str | None = None
    opencode_base_url: str | None = None
    opencode_model: str = "qwen3.6-plus"

    # --- LLM fallback (Groq) --------------------------------------------
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"

    # --- Observability ----------------------------------------------------
    langsmith_api_key: str | None = None
    langsmith_tracing: bool = False
    langsmith_project: str = "deep-apartment-finder"

    # --- Run behaviour ----------------------------------------------------
    ingest_max_listings: int = 50
    scraper_delay_seconds: float = 1.5
    scraper_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    @property
    def has_opencode_primary(self) -> bool:
        # The base URL is optional at the env level; the LLM factory
        # falls back to the canonical OpenCode Go endpoint when blank.
        return bool(self.opencode_api_key)

    @property
    def has_groq_fallback(self) -> bool:
        return bool(self.groq_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Tests that need to override env vars should
    call `Settings.model_validate({...})` directly and not use this cache."""
    return Settings()
