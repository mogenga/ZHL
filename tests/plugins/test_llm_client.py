"""LLMClient plugin tests (PB Task 3, L1/L1a, 总 spec §9.1).

Pins:
- ``MockLLMClient`` routes by the incident_id embedded in the prompt and
  returns the fixture's hypothesis dict; unknown incident_id -> KeyError.
- The attribution fixture pins Review Focus #1's *input*: at least one
  hypothesis carries an empty ``evidence_ids`` list, so the skill's
  evidence-admission filter has a deterministic no-evidence case to drop.
- ``LLMClient`` is a runtime-checkable Protocol; ``MockLLMClient`` conforms.
- ``OpenAICompatClient`` refuses to construct without an API key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plugins.llm_client import LLMClient, MockLLMClient, OpenAICompatClient

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
_ATTRIBUTION = _FIXTURES_DIR / "mock_llm_attribution.json"


def _prompt(incident_id: str) -> str:
    return (
        'Attribute root causes for the batch anomaly. Context: {"incident_id": "'
        + incident_id
        + '"}'
    )


def test_mock_routes_by_incident_id() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    out = client.chat(_prompt("INC-001"), schema={})
    fixture = json.loads(_ATTRIBUTION.read_text(encoding="utf-8"))
    assert out == fixture["INC-001"]


def test_mock_unknown_incident_raises() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    with pytest.raises(KeyError):
        client.chat(_prompt("INC-999"), schema={})


def test_mock_returns_hypotheses_shape() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    out = client.chat(_prompt("INC-004"), schema={})
    assert "hypotheses" in out
    for h in out["hypotheses"]:
        assert set(h) == {"cause", "confidence", "evidence_ids", "recommended_action"}


def test_fixture_pins_no_evidence_gate_input() -> None:
    """PB Review Focus #1: at least one hypothesis has empty evidence_ids so the
    skill-level admission filter (no-evidence -> drop) has a deterministic case."""
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


def test_protocol_conformance() -> None:
    client = MockLLMClient(fixture_path=_ATTRIBUTION)
    assert isinstance(client, LLMClient)


def test_openai_compat_client_requires_key() -> None:
    with pytest.raises(ValueError):
        OpenAICompatClient(api_key="", base_url="http://localhost:9001/v1", model="m")
