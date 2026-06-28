"""Shared pytest fixtures and fakes for unit + integration tests."""

from __future__ import annotations

import os

import pytest

# Ensure tests don't try to talk to a real LLM. The orchestrator and tools
# read these env vars; we leave the keys empty and short-circuit in fakes.
os.environ.setdefault("GROQ_API_KEY", "test-groq")
os.environ.setdefault("OPENCODE_API_KEY", "test-opencode")
os.environ.setdefault("LANGSMITH_API_KEY", "test-langsmith")
os.environ.setdefault("LANGSMITH_TRACING", "false")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
