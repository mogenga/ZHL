"""MockLLMClient unit tests (PA plan Task 3).

MockLLMClient reads a fixed JSON fixture (``tests/fixtures/mock_llm_outputs.json``)
and routes by a stable key embedded in the prompt (B1: ``[SOURCE_SN:<sn>]``
marker, parsed into the ``extraction`` block). No network, no env vars (R4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plugins.llm_client import LLMClient, MockLLMClient

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


@pytest.fixture()
def client() -> MockLLMClient:
    return MockLLMClient(_FIXTURES)


def test_chat_returns_extraction_fixture(client: MockLLMClient) -> None:
    prompt = "Extract parameters for SN-I-OODB [SOURCE_SN:SN-I-OODB]"
    result = client.chat(prompt, {})
    assert isinstance(result, dict)
    # Return the raw fixture ``extraction`` block
    expected = json.loads((_FIXTURES / "mock_llm_outputs.json").read_text(encoding="utf-8"))[
        "extraction"
    ]
    assert result == expected
    assert result["confidence"] == 0.81
    assert "SN-I-OODB" in result["source_sn"]


def test_chat_deterministic(client: MockLLMClient) -> None:
    prompt = "Extract parameters for SN-I-OODB [SOURCE_SN:SN-I-OODB]"
    first = client.chat(prompt, {})
    second = client.chat(prompt, {})
    assert first == second


def test_chat_missing_key_raises(client: MockLLMClient) -> None:
    # No fixture entry for this SN
    prompt = "Extract parameters for SN-UNKNOWN [SOURCE_SN:SN-UNKNOWN]"
    with pytest.raises(KeyError):
        client.chat(prompt, {})


def test_no_source_sn_marker_raises(client: MockLLMClient) -> None:
    # Prompt without the required marker -> unparseable key
    prompt = "Extract parameters with no marker"
    with pytest.raises(KeyError):
        client.chat(prompt, {})


def test_no_env_vars_required(monkeypatch) -> None:
    """R4: MockLLMClient must not read any ZHL_LLM_* env var."""
    for var in ("ZHL_LLM_API_KEY", "ZHL_LLM_BASE_URL", "ZHL_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    client = MockLLMClient(_FIXTURES)
    prompt = "Extract parameters for SN-I-OODB [SOURCE_SN:SN-I-OODB]"
    result = client.chat(prompt, {})
    assert result["confidence"] == 0.81


def test_satisfies_llm_client_protocol(client: MockLLMClient) -> None:
    assert isinstance(client, LLMClient)
