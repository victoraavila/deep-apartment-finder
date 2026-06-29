"""Filesystem route wiring tests."""

from __future__ import annotations

import warnings

from langchain_core._api.deprecation import LangChainDeprecationWarning

from deep_apartment_finder.filesystem.routes import build_backend


def test_store_backend_uses_explicit_namespace_without_deprecation_warning() -> None:
    backend = build_backend()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = backend.write("/orchestrator/reports/test.txt", "ok")

    assert result.error is None
    assert not [
        w
        for w in caught
        if issubclass(w.category, LangChainDeprecationWarning)
        and "without an explicit `namespace`" in str(w.message)
    ]
