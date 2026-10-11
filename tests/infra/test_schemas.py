"""Pydantic schemas self-check (Task 7, P0 spec §5.3).

The Phase-0 schemas are skeletons; this test only proves they import, build
valid models, and enforce the obvious field constraints. It deliberately does
NOT test API routes (Phase A/B).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.types import Decision, RuleSnapshotIn
from app.infra.schemas import (
    IntakeRequest,
    IntakeResponse,
    JudgeRequest,
    ReleaseRequest,
    TransitionRequest,
)


def test_intake_request_minimal() -> None:
    req = IntakeRequest(batch_id="B-1", source_type="IV_CSV")
    assert req.source_type == "IV_CSV"
    assert req.file_path is None


def test_intake_request_bad_source_type() -> None:
    with pytest.raises(ValidationError):
        IntakeRequest(batch_id="B-1", source_type="TELEPATHY")


def test_intake_response_confidence_bounds() -> None:
    IntakeResponse(task_id="T-1", status="pending", confidence=0.9)
    with pytest.raises(ValidationError):
        IntakeResponse(task_id="T-1", status="pending", confidence=1.5)


def test_judge_request_defaults() -> None:
    req = JudgeRequest(batch_id="B-1")
    assert req.operator == "quality-engineer"


def test_release_request() -> None:
    req = ReleaseRequest(batch_id="B-1")
    assert req.operator


def test_transition_request_kind_constraint() -> None:
    TransitionRequest(batch_id="B-1", target_state="pending_release", kind="batch")
    with pytest.raises(ValidationError):
        TransitionRequest(batch_id="B-1", target_state="x", kind="widget")


def test_schemas_reuse_domain_decision_type() -> None:
    """JudgeResponse embeds the domain Decision — the contract is shared."""
    snap = RuleSnapshotIn(nameplate_power=550.0, power_tolerance=0.03)
    d = Decision(result="Pass", checks=(), rule_snapshot=snap, batch_id="B-1", sn="SN-1")
    from app.infra.schemas import JudgeResponse

    resp = JudgeResponse(batch_id="B-1", decision=d)
    assert resp.decision.result == "Pass"
