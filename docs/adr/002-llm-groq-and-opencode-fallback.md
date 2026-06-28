# ADR-002 — LLM: Groq primary + opencode-go (glm) fallback

- Status: Accepted (Sprint 1)
- Date: 2026-06-27

## Context

The reasoning LLM is the most expensive runtime dependency. We need:

- Fast inference for the daily run.
- Resilience to rate limits (a single hard-fail on the daily loop is
  unacceptable for a personal cron-driven agent).
- A single, well-typed interface inside the agent so swapping providers is
  transparent to callers.

## Decision

- **Primary:** `langchain-groq` (Groq's OpenAI-compatible chat endpoint).
- **Fallback:** `langchain-openai` pointed at the opencode-go endpoint
  (GLM-class model), configured via `OPENCODE_BASE_URL` /
  `OPENCODE_API_KEY` / `OPENCODE_MODEL`.
- The swap triggers when Groq returns a rate-limit / quota error. The
  factory lives in `llm.py` and the agent only sees a chat model.

## Consequences

- Two providers means two env-var blocks in `.env.example`. They are loaded
  by `pydantic-settings` and validated up front.
- The fallback is *not* silent: calls and failures are LangSmith-traced, so
  we can see when the swap is happening and why.
- The opencode-go endpoint URL and API key can be auto-discovered from the
  opencode environment (when the agent runs inside opencode). We respect
  whatever is already set in the environment; `.env` only fills in blanks.
- Cost: the fallback is OpenAI-compatible, so pricing is whatever the
  opencode-go plan charges. We accept this for resilience.
