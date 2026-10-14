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


def test_phase_a_unknown_sn_falls_back_to_synthetic_block(
    phase_a_client: MockLLMClient,
) -> None:
    """A demo must not die on SN-N-003 because the frozen fixture pinned one SN.

    Unknown SNs get a deterministically derived synthetic block that still
    looks like an extraction: the SN echoed back, the fixture's parameter
    shape, a stable confidence, and ``synthetic=True`` so the evidence meta
    can mark it as mock-derived rather than frozen fixture data.
    """
    prompt = "Extract parameters for SN-UNKNOWN [SOURCE_SN:SN-UNKNOWN]"
    block = phase_a_client.chat(prompt, {})
    assert block["source_sn"] == "SN-UNKNOWN"
    assert block["synthetic"] is True
    # Deterministic across calls, and distinct per SN.
    assert block == phase_a_client.chat(prompt, {})
    other = phase_a_client.chat("x [SOURCE_SN:SN-OTHER]", {})
    assert other["source_sn"] == "SN-OTHER"
    # Same parameter keys as the frozen fixture, values in plausible IV range.
    assert set(block["extracted_params"]) == {
        "pmax",
        "im",
        "voc",
        "isc",
        "ff",
    }
    assert 0.0 < block["confidence"] <= 1.0


def test_phase_a_low_sn_prefix_pins_low_confidence(phase_a_client: MockLLMClient) -> None:
    """The 0.8 auto-confirm boundary is demonstrable on demand.

    A demo needs both sides of ``confidence >= 0.8``: SN-LOW-* always lands
    below it (manual confirmation path), other SNs above it (auto-confirm).
    """
    low = phase_a_client.chat("x [SOURCE_SN:SN-LOW-001]", {})
    assert low["confidence"] < 0.8
    high = phase_a_client.chat("x [SOURCE_SN:SN-N-001]", {})
    assert high["confidence"] >= 0.8


def test_phase_a_frozen_sn_is_untouched(phase_a_client: MockLLMClient) -> None:
    """The fixture's own SN keeps the byte-identical frozen block (no
    ``synthetic`` marker, no re-derived values)."""
    out = phase_a_client.chat("x [SOURCE_SN:SN-I-OODB]", {})
    assert out["confidence"] == 0.81
    assert "synthetic" not in out


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


def test_phase_b_unknown_incident_gets_synthetic_hypotheses() -> None:
    """An incident with no fixture key still gets hypotheses.

    Every real incident gets a fresh UUID, so a fixture keyed by id can never
    cover a live run — raising here (the old behaviour) meant the demo died
    at the attribution step on any incident the fixture did not name. The
    synthetic block cites the evidence ids the *prompt* carries, so the D2
    gate still validates them against the real store.
    """
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    prompt = (
        "Root-cause attribution request.\n"
        "incident_id=INC-999\n"
        "severity=major\n"
        "trigger_type=rule_fail\n"
        "evidence_ids=['ev-a', 'ev-b']\n"
    )
    out = client.chat(prompt, schema={})
    assert out["synthetic"] is True
    hyps = out["hypotheses"]
    assert hyps and hyps[0]["evidence_ids"] == ["ev-a", "ev-b"]
    # One hypothesis deliberately carries no evidence: the "confidence orders,
    # evidence gates" demonstration survives on every incident.
    assert any(h["evidence_ids"] == [] for h in hyps)


def test_phase_b_synthetic_hypotheses_are_deterministic() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    prompt = "incident_id=INC-999\ntrigger_type=rule_fail\nevidence_ids=['ev-a']\n"
    assert client.chat(prompt, {}) == client.chat(prompt, {})


# T2 UUID-shaped fixture key: consumed by the T-1 attribute endpoint (Task 3)
# via a UUID incident, pin-verified here at the client routing level.
_T2_UUID_INCIDENT = "8f6e2a10-0000-4000-8000-0000000000aa"
# Frozen keys (must survive Task 2's add-only change untouched).
_FROZEN_KEYS = ["INC-001", "INC-004"]


def test_phase_b_mock_routes_by_uuid_incident() -> None:
    """Task 2: MockLLMClient routes a UUID incident_id to the UUID fixture key.

    Pin: incident_id=8f6e2a10-...-aa -> that key's hypothesis block; every
    returned hypothesis carries the {cause, confidence, evidence_ids,
    recommended_action} shape, and the D2 gate sees >=1 with-evidence
    hypothesis (the endpoint will rely on "not always empty").
    """
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    out = client.chat(_prompt(_T2_UUID_INCIDENT), schema={})
    assert "hypotheses" in out
    assert out["hypotheses"], "UUID key must carry >=1 hypothesis"
    for h in out["hypotheses"]:
        assert set(h) == {"cause", "confidence", "evidence_ids", "recommended_action"}


def test_phase_b_frozen_keys_survive_uuid_add() -> None:
    """Task 2 regression: the add-only UUID key must leave INC-001 / INC-004
    byte-identical (the frozen Phase B contract) and coexist with them."""
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    fixture = json.loads(_ATTRIBUTION.read_text(encoding="utf-8"))
    for key in _FROZEN_KEYS:
        assert key in fixture, f"frozen key vanished: {key!r}"
        out = client.chat(_prompt(key), schema={})
        assert out == fixture[key], f"frozen key {key!r} routing changed"
    # The UUID key coexists with the frozen keys.
    assert _T2_UUID_INCIDENT in fixture


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
