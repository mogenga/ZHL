"""Regression guard: the LLM client is selected from the environment.

Background
----------
``app/main.py`` hard-coded ``MockLLMClient(_DEFAULT_FIXTURES)`` in both app
factories, so ``ZHL_LLM_API_KEY`` / ``ZHL_LLM_BASE_URL`` / ``ZHL_LLM_MODEL``
were never read in production — configuring a real model had no effect until
someone edited the source. The factories now delegate to
``_default_llm_client()``, which picks the real client only when a key is
present and otherwise falls back to the deterministic fixture mock.

Two failure modes are guarded here:

1. Wiring the real client unconditionally — tests and offline demos would hit
   the network (or fail construction with no key).
2. Dropping the *injected* ``llm=`` argument while doing so. That regression
   actually happened during this change: ``llm or MockLLMClient(...)`` became
   ``_default_llm_client()``, silently discarding the test doubles passed by
   ``test_batches.py`` and breaking 9 tests with a fixture ``KeyError``.
"""

from __future__ import annotations

import pytest

from app.main import (
    _default_checkpointer_mode,
    _default_llm_client,
    create_unified_app,
)
from app.plugins.llm_client import MockLLMClient, OpenAICompatClient


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from "no LLM configured" unless it says otherwise."""
    for var in ("ZHL_LLM_API_KEY", "ZHL_LLM_BASE_URL", "ZHL_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)


def test_no_key_falls_back_to_mock() -> None:
    """Zero-credential local runs and the whole test suite keep working."""
    assert isinstance(_default_llm_client(), MockLLMClient)


def test_key_present_selects_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHL_LLM_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("ZHL_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("ZHL_LLM_MODEL", "deepseek-v4-flash")

    client = _default_llm_client()
    assert isinstance(client, OpenAICompatClient)
    assert client.base_url == "https://api.deepseek.com"
    assert client.model == "deepseek-v4-flash"


def test_blank_key_is_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only value must not trigger a doomed real client."""
    monkeypatch.setenv("ZHL_LLM_API_KEY", "   ")
    assert isinstance(_default_llm_client(), MockLLMClient)


def test_factories_keep_the_injected_llm_short_circuit() -> None:
    """Both factories must keep ``llm or _default_llm_client()``.

    The concrete regression: rewriting that line as bare
    ``_default_llm_client()`` silently discarded the doubles injected by
    ``test_batches.py`` (9 failures, fixture ``KeyError`` on ``SN-A4-00``).
    A source-level guard is the only cheap way to keep that fixed — the
    behavioural symptom only shows up in whichever suite happens to inject.
    """
    import inspect

    from app import main as main_mod

    for factory in (main_mod.create_app, main_mod.create_unified_app):
        src = inspect.getsource(factory)
        assert "_default_llm_client()" in src, f"{factory.__name__} lost the env selector"
        if "llm" in inspect.signature(factory).parameters:
            assert (
                "llm or _default_llm_client()" in src
            ), f"{factory.__name__} no longer honours the injected llm argument"


def test_unified_app_uses_env_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unified app (what uvicorn serves) goes through the same selector."""
    monkeypatch.setenv("ZHL_LLM_API_KEY", "sk-env")
    # Construction must not raise: a real client is built eagerly here.
    app = create_unified_app()
    assert app is not None


def test_checkpointer_defaults_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHL_CHECKPOINTER", raising=False)
    assert _default_checkpointer_mode() == "memory"


def test_checkpointer_reads_pg_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHL_CHECKPOINTER", "PG")
    assert _default_checkpointer_mode() == "pg"


def test_checkpointer_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHL_CHECKPOINTER", "sqlite")
    with pytest.raises(ValueError, match="must be 'pg' or 'memory'"):
        _default_checkpointer_mode()
