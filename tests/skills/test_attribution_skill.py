"""AttributionSkill integration tests (PB Task 5, D2/D3; AGENT.md §4.3).

Runs the full Skill path on SQLite: MockLLMClient (routing on
``incident_id`` extracted from the prompt) + FileEvidenceStore + the
trace store. Pins:

- ``run`` writes ``quality_incidents.root_cause_hypotheses`` (the D2/D3
  validated list) and leaves a ``agent_task_traces`` row
  (``skill_name="attribution"``).
- The LLM call happens OUTSIDE the business transaction; the trace
  ``params_summary`` records only a prompt digest (prompt length + first
  64 chars), never the full prompt / any secret material.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.infra.models import (
    Base,
    DeliveryBatch,
    QualityIncident,
    QualityRuleVersion,
)
from app.plugins.attribution import AttributionSkill
from app.plugins.evidence_store import FileEvidenceStore
from app.plugins.llm_client import MockLLMClient
from app.plugins.trace_store import TraceStore

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_INCIDENT_UUID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeffff0001")
_BATCH_UUID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeffff0002")
_RULE_UUID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeffff0003")


@pytest.fixture()
def sqlite_session() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    rule = QualityRuleVersion(
        rule_version_id=_RULE_UUID,
        project_id="P-1",
        model="JH-550W",
        name="rv-1",
        nameplate_power=550.0,
        power_tolerance=0.5,
        parameter_standards={},
        current_bands={},
        sample_plan={},
        effective_at=_TS,
        version_no=1,
    )
    batch = DeliveryBatch(
        batch_id=_BATCH_UUID,
        batch_no="B-AT-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=100,
        rule_version_id=_RULE_UUID,
        status="isolated",
    )
    incident = QualityIncident(
        incident_id=_INCIDENT_UUID,
        batch_id=_BATCH_UUID,
        trigger_type="rule_fail",
        severity="major",
        status="isolated",
        evidence_ids=["EV-ATT-001", "EV-ATT-002"],
    )
    session.add(rule)
    session.flush()
    session.add(batch)
    session.flush()
    session.add(incident)
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _make_skill(tmp_path: Any) -> AttributionSkill:
    root = tmp_path / "evidence"
    store = FileEvidenceStore(root=root)
    store.save("attribution", {"kind": "incident", "id": "INC-001"}, content=b"ev-1")
    store.save("attribution", {"kind": "incident", "id": "INC-002"}, content=b"ev-2")
    # The mock routes on incident_id; our incident UUID is the routing key.
    import json

    fixture = tmp_path / "attribution.json"
    fixture.write_text(
        json.dumps(
            {
                str(_INCIDENT_UUID): {
                    "hypotheses": [
                        {
                            "cause": "cell mismatch",
                            "confidence": 0.87,
                            "evidence_ids": [
                                store._by_id and list(store._by_id)[0],
                                list(store._by_id)[1],
                            ],
                            "recommended_action": "re-test",
                        },
                        {
                            "cause": "no-evidence cause",
                            "confidence": 0.40,
                            "evidence_ids": [],
                            "recommended_action": "visual",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    llm = MockLLMClient(fixture_path=fixture)
    return AttributionSkill(llm=llm, evidence_store=store)


def test_skill_writes_hypotheses_and_trace(sqlite_session: Any, tmp_path: Any) -> None:
    skill = _make_skill(tmp_path)
    trace = TraceStore()
    incident: QualityIncident = sqlite_session.get(
        QualityIncident, _INCIDENT_UUID
    )  # type: ignore[union-attr]
    result = skill.run(
        incident,
        trace=trace,
        session=sqlite_session,  # type: ignore[arg-type]
    )
    # run returns the validated hypotheses list (not a dict)
    assert result  # non-empty
    # D2 gate: the no-evidence 0.40 hypothesis was dropped
    assert [h["confidence"] for h in result] == [0.87]
    assert [h["rank"] for h in result] == [1]
    # DB write landed
    incident = sqlite_session.get(QualityIncident, _INCIDENT_UUID)  # type: ignore[union-attr]
    assert incident.root_cause_hypotheses  # non-empty JSONB
    # trace row exists
    rows = trace.list_for_batch(sqlite_session, _BATCH_UUID)  # type: ignore[arg-type]
    assert any(r.skill_name == "attribution" for r in rows)
    assert any(r.structured_output and r.structured_output.get("hypotheses") for r in rows)


def test_skill_trace_records_prompt_digest_not_secret(sqlite_session: Any, tmp_path: Any) -> None:
    skill = _make_skill(tmp_path)
    trace = TraceStore()
    incident: QualityIncident = sqlite_session.get(
        QualityIncident, _INCIDENT_UUID
    )  # type: ignore[union-attr]
    skill.run(incident, trace=trace, session=sqlite_session)  # type: ignore[arg-type]
    rows = trace.list_for_batch(sqlite_session, _BATCH_UUID)  # type: ignore[arg-type]
    att = [r for r in rows if r.skill_name == "attribution"]
    assert att
    summary = att[0].params_summary or {}
    # digest recorded: prompt length + truncated head only
    assert "prompt_len" in summary
    assert "prompt_head" in summary
    assert len(summary["prompt_head"]) <= 64
    # never the full prompt text
    assert "incident_id" not in summary.get("prompt_full", "")
    assert summary.get("model") == "mock"
