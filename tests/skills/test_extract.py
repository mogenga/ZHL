"""Extract Skill replay tests (PA plan Task 5 / AGENT.md §3.3 skills ≥ 60%).

Fixed MockLLMClient output (tests/fixtures/mock_llm_outputs.json, P0-frozen):
the extract Skill embeds the ``[SOURCE_SN:<sn>]`` marker (Task 3 B1) in the
prompt so the mock routes to the ``extraction`` block; the Skill itself
performs no business judgment — it returns fields + confidence + findings
and writes the ``extraction_result`` evidence record.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.plugins.evidence_store import FileEvidenceStore
from app.plugins.llm_client import MockLLMClient
from app.skills.extract import extract_inspection_data

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_SOURCE_SN = "SN-I-OODB"


@pytest.fixture()
def llm() -> MockLLMClient:
    return MockLLMClient(_FIXTURES)


@pytest.fixture()
def evidence(tmp_path: Path) -> FileEvidenceStore:
    return FileEvidenceStore(root=tmp_path, db=None)


def _payload() -> dict:
    return {"sn": _SOURCE_SN, "pmax": 550.0, "im": 16.5}


def test_extract_returns_confidence(llm: MockLLMClient, evidence: FileEvidenceStore) -> None:
    ref = {"kind": "batch", "id": "B-001"}
    result = extract_inspection_data(llm, "IV_CSV", _payload(), evidence, ref)
    assert result["confidence"] == 0.81
    expected_fields = json.loads((_FIXTURES / "mock_llm_outputs.json").read_text(encoding="utf-8"))[
        "extraction"
    ]["extracted_params"]
    assert result["fields"] == expected_fields
    assert result["appearance_findings"] == ["bypass-diode hot spot at cell 4"]


def test_extract_deterministic(llm: MockLLMClient, evidence: FileEvidenceStore) -> None:
    ref = {"kind": "batch", "id": "B-002"}
    a = extract_inspection_data(llm, "IV_CSV", _payload(), evidence, ref)
    b = extract_inspection_data(llm, "IV_CSV", _payload(), evidence, ref)
    assert a == b


def test_extract_writes_evidence(llm: MockLLMClient, evidence: FileEvidenceStore) -> None:
    ref = {"kind": "batch", "id": "B-003"}
    extract_inspection_data(llm, "IV_CSV", _payload(), evidence, ref)

    refs = evidence.list_by_object(ref)
    assert len(refs) == 1
    stored_ref = refs[0]
    assert stored_ref.evidence_type == "extraction_result"
    stored = evidence.get(stored_ref.evidence_id)
    # content_hash must equal the SHA-256 of the stored JSON bytes
    assert hashlib.sha256(stored.content).hexdigest() == stored_ref.content_hash
    # and the stored JSON is the fixture extraction block, verbatim
    expected = json.loads((_FIXTURES / "mock_llm_outputs.json").read_text(encoding="utf-8"))[
        "extraction"
    ]
    assert json.loads(stored.content.decode("utf-8")) == expected


def test_extract_min_confidence(llm: MockLLMClient, evidence: FileEvidenceStore) -> None:
    """Q3 input side: two extracts -> min() over the per-SN confidences.

    The Skill returns a single-SN confidence; batch-level min aggregation
    happens in the graph node (Task 7). This case verifies the returned
    value is directly consumable by ``min()``.
    """
    ref = {"kind": "batch", "id": "B-004"}
    r1 = extract_inspection_data(llm, "IV_CSV", _payload(), evidence, ref)
    r2 = extract_inspection_data(llm, "IV_CSV", _payload(), evidence, ref)
    batch_confidence = min(r1["confidence"], r2["confidence"])
    assert batch_confidence == 0.81
