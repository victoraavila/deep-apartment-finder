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
    # Sprint 3 — per-portal delay override. The Idealista search page is
    # DataDome-protected, so the scraper must appear less aggressive than
    # the Fotocasa scraper. 2.0s matches ADR-011.
    idealista_scraper_delay_seconds: float = 2.0
    # `curl_cffi` impersonation target for the Idealista scraper. Tested
    # values: `chrome131` and `chrome124` pass DataDome; `chrome142` does
    # not. Pinned so the operator doesn't have to discover the working
    # profile.
    idealista_impersonate: str = "chrome131"
    # Set to `false` to skip Idealista entirely (e.g. if DataDome starts
    # blocking the working profile, or for tests).
    idealista_enabled: bool = True

    # --- Researcher web search ------------------------------------------
    exa_api_key: str | None = None

    # --- Notification (Gmail SMTP) ---------------------------------------
    # The App Password is generated in the operator's Google account
    # security settings (2FA must be on). The address is the Gmail
    # login that owns the App Password.
    gmail_smtp_host: str = "smtp.gmail.com"
    gmail_smtp_port: int = 465
    gmail_smtp_address: str | None = None
    gmail_smtp_app_password: str | None = None
    notify_to_address: str | None = None

    # --- Ranking weights (soft criteria) --------------------------------
    # Final score = sum(weight_i * score_i) / sum(weight_i).
    # Defaults match `SPRINT2.md`. Tune here, not in code.
    rank_weight_distance: float = 0.5
    rank_weight_pet_policy: float = 0.3
    rank_weight_furnished: float = 0.2
    rank_max_distance_m: float = 2000.0
    rank_top_n: int = 5

    @property
    def has_opencode_primary(self) -> bool:
        # The base URL is optional at the env level; the LLM factory
        # falls back to the canonical OpenCode Go endpoint when blank.
        return bool(self.opencode_api_key)

    @property
    def has_groq_fallback(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def has_gmail_smtp(self) -> bool:
        return bool(self.gmail_smtp_address) and bool(self.gmail_smtp_app_password)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Tests that need to override env vars should
    call `Settings.model_validate({...})` directly and not use this cache."""
    return Settings()
