# ADR-002 — LLM: OpenCode Go primary + Groq fallback

- Status: Accepted (Sprint 1); revised in Sprint 5 to swap provider order
- Date: 2026-06-27 (revised 2026-06-28)

## Context

The reasoning LLM is the most expensive runtime dependency. We need:

- Fast inference for the daily run.
- Resilience to rate limits (a single hard-fail on the daily loop is
  unacceptable for a personal cron-driven agent).
- A single, well-typed interface inside the agent so swapping providers is
  transparent to callers.
- A provider whose available model zoo matches the agent's tool-calling
  workload (function calls, structured output, JSON tool arguments).

## Decision

- **Primary:** `langchain-openai` pointed at the OpenCode Go endpoint
  (`https://opencode.ai/zen/go/v1`, OpenAI-compatible
  `/chat/completions`). The default model is `qwen3.6-plus`, override via
  `OPENCODE_MODEL`. Available models on the Go plan that speak the OpenAI
  protocol: `glm-5`, `glm-5.1`, `kimi-k2.5`, `kimi-k2.6`, `deepseek-v4-pro`,
  `deepseek-v4-flash`, `qwen3.5-plus`, `qwen3.6-plus`, `mimo-v2-pro`,
  `mimo-v2-omni`. The MiniMax M2.5 / M2.7 models on Go use the Anthropic
  protocol and are not addressable from the OpenAI-compatible adapter.
- **Fallback:** `langchain-groq` (Groq's OpenAI-compatible chat endpoint),
  triggered when the primary returns a rate-limit / quota error.
- The swap is detected by `_is_rate_limit` in `llm.py`, which scans
  `exc.args` recursively (the provider SDKs raise with
  `args=(error_dict,)` where the human-readable text lives under
  `error.message`; a string-only scan misses them). The agent only sees a
  chat model.

## Why the order changed (Sprint 5 revision)

The Sprint 1 order was Groq primary, opencode-go fallback. In practice:

- Groq's free tier imposes a tight TPM cap (8k for `qwen3.6-27b`) and the
  orchestrator's first turn (system prompt + subagent schema + tool
  schemas + planning prompt) routinely approaches it. A 429 on turn two
  is the default failure mode for a daily run.
- OpenCode Go is a subscription with substantially higher per-minute
  limits, and its hosted models cover the agent's needs. Putting it as
  primary makes the daily loop sustainable on its own; Groq only has to
  carry traffic when Go is unavailable.

## Consequences

- Two providers means two env-var blocks in `.env.example`. They are
  loaded by `pydantic-settings` and validated up front. The base URL is
  optional at the env level; `llm.py` defaults to the canonical
  `https://opencode.ai/zen/go/v1` when blank, so a key alone is enough
  to run.
- The fallback is *not* silent: calls and failures are LangSmith-traced,
  so we can see when the swap is happening and why.
- Cost: the primary is on a subscription, the fallback is pay-as-you-go.
  We accept this for resilience.
