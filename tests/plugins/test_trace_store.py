"""Trace-store + report-renderer plugin tests (PB Task 4, D1 三层留痕).

TraceStore: records AgentTaskTrace rows on a SQLite session; list_for_batch
returns them in started_at ascending order (the trace timeline).
ReportRenderer: pure, clock-free Markdown+JSON rendering of release /
incident reports from a fixed-shape context dict.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.infra.models import AgentTaskTrace, Base
from app.plugins.report_renderer import ReportRenderer
from app.plugins.trace_store import TraceStore

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)

# agent_task_traces.batch_id is a UUID FK to delivery_batches in the DDL, so
# the trace store must be fed real UUIDs. The fixture uses a fixed one.
_BATCH_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture()
def sqlite_session() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    # agent_task_traces.batch_id -> delivery_batches.batch_id is a real FK
    # (enforced in tests since the SQLite PRAGMA was turned on), so the
    # referenced batch must exist before any trace row is written.
    from app.infra.models import DeliveryBatch, QualityRuleVersion

    rule_id = uuid.UUID("11111111-2222-3333-4444-555555555501")
    session.add(
        QualityRuleVersion(
            rule_version_id=rule_id,
            project_id="P-1",
            model="JH-550W",
            name="rv-trace",
            nameplate_power=550.0,
            power_tolerance=0.03,
            parameter_standards={},
            current_bands={},
            sample_plan={},
            effective_at=_TS,
            version_no=1,
        )
    )
    session.flush()
    session.add(
        DeliveryBatch(
            batch_id=_BATCH_UUID,
            batch_no="B-TRACE-STORE-1",
            project_id="P-1",
            model="JH-550W",
            planned_quantity=1,
            rule_version_id=rule_id,
            status="isolated",
        )
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _record_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": str(uuid.uuid4()),
        "batch_id": _BATCH_UUID,
        "skill_name": "attribution",
        "input_ref": {"kind": "incident", "id": "INC-001"},
        "plugins_invoked": ["llm_client"],
        "params_summary": {"prompt_len": 42},
        "model_version": "mock-attribution-v1",
        "structured_output": {"hypotheses": []},
        "validation_result": "pass",
        "failure_reason": None,
        "human_action": None,
        "started_at": _TS,
        "finished_at": None,
    }
    base.update(overrides)
    return base


def test_trace_record_persists_and_lists_ascending(
    sqlite_session: Any,
) -> None:
    store = TraceStore()
    t1 = store.record(sqlite_session, **_record_kwargs(started_at=_TS))
    t2 = store.record(
        sqlite_session,
        **_record_kwargs(
            task_id=str(uuid.uuid4()),
            skill_name="report",
            started_at=_TS.replace(hour=11),
        ),
    )
    rows = store.list_for_batch(sqlite_session, _BATCH_UUID)
    assert [r.task_id for r in rows] == [t1.task_id, t2.task_id]
    assert all(isinstance(r, AgentTaskTrace) for r in rows)


def test_trace_validation_result_defaults_to_partial(
    sqlite_session: Any,
) -> None:
    store = TraceStore()
    kwargs = _record_kwargs()
    del kwargs["validation_result"]
    row = store.record(sqlite_session, **kwargs)
    assert row.validation_result == "partial"


# ---------------------------------------------------------------------------
# ReportRenderer
# ---------------------------------------------------------------------------


def _full_context() -> dict[str, Any]:
    return {
        "batch": {"batch_id": "B-INCIDENT-001", "status": "pending_release", "model": "JH-550W"},
        "decisions": [{"rule": "im_band", "passed": True, "detail": {}}],
        "incidents": [{"incident_id": "INC-001", "status": "assigned", "severity": "major"}],
        "reinspections": [{"reinspection_id": "RE-001", "result": "pass", "scope_type": "minimal"}],
        "assertions": {
            "passed": True,
            "checks": [
                {"name": "state", "passed": True, "reason": ""},
                {"name": "sample_passed", "passed": True, "reason": ""},
                {"name": "no_open_critical_incident", "passed": True, "reason": ""},
                {"name": "no_isolated_sn", "passed": True, "reason": ""},
                {"name": "all_evidence_present", "passed": True, "reason": ""},
                {"name": "all_capa_verified", "passed": True, "reason": ""},
                {"name": "reinspection_scope_covered", "passed": True, "reason": ""},
            ],
        },
        "refusal_reason": "",
        "trace_refs": ["task-uuid-1", "task-uuid-2"],
        "evidence_refs": ["EV-1", "EV-2"],
    }


def test_release_report_markdown_contains_batch_and_seven_assertions() -> None:
    ctx = _full_context()
    ctx["generated_at"] = "2026-06-01T12:00:00Z"
    md, js = ReportRenderer().render_release_report(ctx)
    assert "B-INCIDENT-001" in md
    for name in [c["name"] for c in ctx["assertions"]["checks"]]:
        assert name in md
    assert "task-uuid-1" in md and "EV-1" in md  # 三层留痕引用


def test_release_report_json_has_seven_checks() -> None:
    ctx = _full_context()
    ctx["generated_at"] = "2026-06-01T12:00:00Z"
    _md, js = ReportRenderer().render_release_report(ctx)
    parsed = json.loads(json.dumps(js))  # round-trip to prove serializability
    assert len(parsed["assertions"]["checks"]) == 7
    assert parsed["batch"]["batch_id"] == "B-INCIDENT-001"


def test_incident_report_includes_refusal_section_when_set() -> None:
    ctx = _full_context()
    ctx["refusal_reason"] = "unverified CAPA: INC-001"
    ctx["generated_at"] = "2026-06-01T12:00:00Z"
    md, js = ReportRenderer().render_incident_report(ctx)
    assert "拒因" in md or "refusal" in md.lower()
    assert "unverified CAPA: INC-001" in md


def test_renderer_is_deterministic_no_clock_read() -> None:
    ctx = _full_context()
    ctx["generated_at"] = "2026-06-01T12:00:00Z"
    r = ReportRenderer()
    md1, js1 = r.render_release_report(ctx)
    md2, js2 = r.render_release_report(ctx)
    assert md1 == md2
    assert js1 == js2
