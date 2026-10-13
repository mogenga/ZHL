"""LLMClient union tests — Phase A extraction + Phase B attribution + OpenAI compat.

Phase A (mock_llm_outputs.json, P0-frozen): ``MockLLMClient(fixture_dir)``
routes ``[SOURCE_SN:<sn>]`` prompts into the fixture's ``extraction`` block.

Phase B (mock_llm_attribution.json, PB-new): ``MockLLMClient(fixture_path)``
routes ``incident_id`` prompts into per-incident hypothesis dicts.

Both routes coexist on one ``MockLLMClient`` (union) — see
``docs/superpowers/plans/2026-10-12-phaseb-merge-conflicts.md`` conflict 4.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plugins.llm_client import LLMClient, MockLLMClient, OpenAICompatClient

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_ATTRIBUTION = _FIXTURES / "mock_llm_attribution.json"


def _prompt(incident_id: str) -> str:
    return (
        'Attribute root causes for the batch anomaly. Context: {"incident_id": "'
        + incident_id
        + '"}'
    )


# ---------------------------------------------------------------------------
# Phase A — [SOURCE_SN] extraction routing
# ---------------------------------------------------------------------------


@pytest.fixture()
def phase_a_client() -> MockLLMClient:
    return MockLLMClient(_FIXTURES)


def test_phase_a_chat_returns_extraction_fixture(phase_a_client: MockLLMClient) -> None:
    prompt = "Extract parameters for SN-I-OODB [SOURCE_SN:SN-I-OODB]"
    result = phase_a_client.chat(prompt, {})
    assert isinstance(result, dict)
    expected = json.loads((_FIXTURES / "mock_llm_outputs.json").read_text(encoding="utf-8"))[
        "extraction"
    ]
    assert result == expected
    assert result["confidence"] == 0.81
    assert "SN-I-OODB" in result["source_sn"]


def test_phase_a_chat_deterministic(phase_a_client: MockLLMClient) -> None:
    prompt = "Extract parameters for SN-I-OODB [SOURCE_SN:SN-I-OODB]"
    assert phase_a_client.chat(prompt, {}) == phase_a_client.chat(prompt, {})


def test_phase_a_chat_missing_key_raises(phase_a_client: MockLLMClient) -> None:
    prompt = "Extract parameters for SN-UNKNOWN [SOURCE_SN:SN-UNKNOWN]"
    with pytest.raises(KeyError):
        phase_a_client.chat(prompt, {})


def test_phase_a_no_source_sn_marker_raises(phase_a_client: MockLLMClient) -> None:
    with pytest.raises(KeyError):
        phase_a_client.chat("Extract parameters with no marker", {})


def test_phase_a_no_env_vars_required(monkeypatch) -> None:
    """R4: MockLLMClient must not read any ZHL_LLM_* env var."""
    for var in ("ZHL_LLM_API_KEY", "ZHL_LLM_BASE_URL", "ZHL_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    client = MockLLMClient(_FIXTURES)
    prompt = "Extract parameters for SN-I-OODB [SOURCE_SN:SN-I-OODB]"
    assert client.chat(prompt, {})["confidence"] == 0.81


# ---------------------------------------------------------------------------
# Phase B — incident_id attribution routing
# ---------------------------------------------------------------------------


def test_phase_b_mock_routes_by_incident_id() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    out = client.chat(_prompt("INC-001"), schema={})
    fixture = json.loads(_ATTRIBUTION.read_text(encoding="utf-8"))
    assert out == fixture["INC-001"]


def test_phase_b_mock_unknown_incident_raises() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    with pytest.raises(KeyError):
        client.chat(_prompt("INC-999"), schema={})


def test_phase_b_mock_returns_hypotheses_shape() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    out = client.chat(_prompt("INC-004"), schema={})
    assert "hypotheses" in out
    for h in out["hypotheses"]:
        assert set(h) == {"cause", "confidence", "evidence_ids", "recommended_action"}


def test_phase_b_fixture_pins_no_evidence_gate_input() -> None:
    """PB Review Focus #1: at least one hypothesis has empty evidence_ids so
    the skill-level admission filter (no-evidence -> drop) has a
    deterministic case."""
    fixture = json.loads(_ATTRIBUTION.read_text(encoding="utf-8"))
    all_hyps = [
        h
        for entry in fixture.values()
        if isinstance(entry, dict)
        for h in entry.get("hypotheses", [])
    ]
    assert any(
        h["evidence_ids"] == [] for h in all_hyps
    ), "fixture must pin an empty-evidence hypothesis"
    assert any(
        h["evidence_ids"] for h in all_hyps
    ), "fixture must also pin a with-evidence hypothesis"


# ---------------------------------------------------------------------------
# Protocol + OpenAI compat (both phases)
# ---------------------------------------------------------------------------


def test_protocol_conformance() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    assert isinstance(client, LLMClient)


def test_openai_compat_client_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Must clear the env explicitly: the client falls back to
    # ``ZHL_LLM_API_KEY`` when the arg is empty, so a developer who configured
    # a real key in .env would silently turn this guard into a no-op.
    monkeypatch.delenv("ZHL_LLM_API_KEY", raising=False)
    with pytest.raises(ValueError):
        OpenAICompatClient(api_key="", base_url="http://localhost:9001/v1", model="m")
