"""Report skill + report API tests (PB Task 7 — D6a auto-archive / D7 / D10 / D8).

Pins:
- released + release report: 201, version=1; repeat -> 200 idempotent same
  version (D10)
- rejected + incident report: 201; rejected + release report -> 409 (D7)
- auto-archive (D6a/D8): released batch after writing the release report
  becomes "archived" in the same transaction; calling the report API on an
  already-archived batch -> 409
- the report JSON carries the 7 assertion checks + the three-layer trace
  reference chain (agent_task_id / evidence id / report id)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import reports as report_api
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Report,
    SampleConclusion,
)

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_BATCH = UUID("dddddddd-1111-2222-3333-444444444401")
_RULE = UUID("dddddddd-1111-2222-3333-444444444402")


def _seed(
    session: Any,
    *,
    batch_status: str = "released",
    with_incident: bool = False,
) -> dict[str, Any]:
    rule = QualityRuleVersion(
        rule_version_id=_RULE,
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
        batch_id=_BATCH,
        batch_no="B-REP-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=100,
        rule_version_id=_RULE,
        status=batch_status,
    )
    ins = ModuleInspection(
        inspection_id=UUID("dddddddd-1111-2222-3333-444444444403"),
        batch_id=_BATCH,
        sn="SN-A",
        source_type="IV_CSV",
        captured_at=_TS,
        pmax=550.0,
        temp=25.0,
        isc=10.5,
        voc=0.5,
        vm=48.0,
        im=11.0,
        ff=0.75,
        appearance_result="pass",
    )
    session.add_all([rule, batch, ins])
    if with_incident:
        inc = QualityIncident(
            incident_id=UUID("dddddddd-1111-2222-3333-444444444404"),
            batch_id=_BATCH,
            trigger_type="rule_fail",
            severity="major",
            status="closed",
        )
        session.add(inc)
    session.commit()
    return {"batch": batch}


@pytest.fixture()
def api_client() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(report_api.router)
    app.dependency_overrides[report_api.get_session] = lambda: session
    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_release_report_201_version_1_then_idempotent(api_client: Any) -> None:
    client, session, _ = api_client
    # D10 idempotency on a status that does NOT auto-archive (disposing),
    # so the repeat call is a clean D10 hit (200) rather than an archived 409.
    _seed(session, batch_status="disposing")
    r1 = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "incident"})
    assert r1.status_code == 201, r1.text
    assert r1.json()["version"] == 1
    # D10: repeat call returns the existing version, no new row
    r2 = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "incident"})
    assert r2.status_code == 200
    assert r2.json()["version"] == 1
    rows = (
        session.query(Report)
        .filter(Report.batch_id == _BATCH, Report.report_type == "incident")
        .all()
    )
    assert len(rows) == 1


def test_rejected_incident_report_201_and_release_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, batch_status="rejected")
    ok = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "incident"})
    assert ok.status_code == 201, ok.text
    denied = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    assert denied.status_code == 409  # D7: rejected only allows incident reports


def test_auto_archive_after_release_report(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, batch_status="released")
    resp = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    assert resp.status_code == 201
    # D6a/D8: same transaction archives the batch
    assert session.get(DeliveryBatch, _BATCH).status == "archived"
    # archived batch now rejects further report calls
    again = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    assert again.status_code == 409


def test_report_json_has_seven_assertions_and_trace_chain(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, batch_status="released")
    resp = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    assert resp.status_code == 201
    body = resp.json()
    # three-layer trace chain: agent_task_id -> evidence (report_snapshot) -> report
    assert body["agent_task_id"]
    report_id = body["report_id"]
    report_row = session.get(Report, UUID(report_id))
    assert report_row.source_snapshot_id  # evidence id
    ev = session.get(Evidence, report_row.source_snapshot_id)
    assert ev.evidence_type == "report_snapshot"
    # structured JSON carries 7 assertion checks
    checks = report_row.content_json.get("assertions", {}).get("checks")
    assert len(checks) == 7


# ---------------------------------------------------------------------------
# 报告可信度回归：真实断言引用 / 状态门 / 判定统计 / 真实拒因
# ---------------------------------------------------------------------------


def test_release_report_requires_released_batch(api_client: Any) -> None:
    """§4.5 闭环点：未放行批次不能开出「放行结论：通过」的报告。"""
    client, session, _ = api_client
    for status in ("pending_judge", "isolated", "pending_release", "disposing"):
        _seed(session, batch_status=status)
        resp = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
        assert resp.status_code == 409, (status, resp.text)
        assert resp.json()["detail"]["error"] == "release_report_requires_released_batch"
        # 清空全部种子（fixture 每用例独立 DB，但循环内 _BATCH/_RULE 固定，
        # 不清会在下一轮撞主键）
        session.query(Report).filter(Report.batch_id == _BATCH).delete()
        session.query(Evidence).delete()
        session.query(ModuleInspection).filter(ModuleInspection.batch_id == _BATCH).delete()
        session.query(QualityIncident).filter(QualityIncident.batch_id == _BATCH).delete()
        session.query(DeliveryBatch).filter(DeliveryBatch.batch_id == _BATCH).delete()
        session.query(QualityRuleVersion).filter(
            QualityRuleVersion.rule_version_id == _RULE
        ).delete()
        session.commit()


def test_release_report_quotes_recorded_assertions(api_client: Any) -> None:
    """放行报告引用放行时刻随证据行落库的真实 AssertionResult，不重算不占位。"""
    client, session, _ = api_client
    seeded = _seed(session, batch_status="released")
    batch = seeded["batch"]
    recorded = {
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
    }
    ev = Evidence(
        evidence_id=UUID("dddddddd-1111-2222-3333-444444444405"),
        evidence_type="human_confirmation",
        object_ref={"kind": "batch", "id": "B-REP-001", "approved_by": "ZHL-QA"},
        content_hash="sha256:" + "0" * 64,
        content={"approved_by": "ZHL-QA", "assertions": recorded},
        generated_at=_TS,
        generated_by="ZHL-QA",
    )
    session.add(ev)
    session.flush()
    batch.release_evidence_id = ev.evidence_id
    session.commit()

    resp = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    assert resp.status_code == 201, resp.text
    report_row = session.get(Report, UUID(resp.json()["report_id"]))
    assertions = report_row.content_json["assertions"]
    assert assertions["passed"] is True
    assert [c["name"] for c in assertions["checks"]] == [c["name"] for c in recorded["checks"]]
    # Markdown 引用的是同一份结果：结论通过、七项在正文
    assert "放行结论：**通过**" in report_row.content_md
    assert "[PASS] state" in report_row.content_md


def test_released_without_release_evidence_reports_honest_fail(api_client: Any) -> None:
    """released 但没有放行证据 → 报告如实报「无记录」，绝不渲染全部通过。"""
    client, session, _ = api_client
    _seed(session, batch_status="released")
    resp = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    assert resp.status_code == 201, resp.text
    report_row = session.get(Report, UUID(resp.json()["report_id"]))
    assertions = report_row.content_json["assertions"]
    assert assertions["passed"] is False
    assert all(c["passed"] is False for c in assertions["checks"])
    assert "no recorded release evidence" in assertions["checks"][0]["reason"]
    assert "放行结论：**通过**" not in report_row.content_md
    assert "放行结论：**未通过**" in report_row.content_md


def test_report_decisions_carries_judgment_stats(api_client: Any) -> None:
    """decisions 是真实判定统计（Pass/Fail 计数 + 抽样接收拒收数），不再恒空。"""
    client, session, _ = api_client
    _seed(session, batch_status="released")
    session.add(
        ModuleInspection(
            inspection_id=UUID("dddddddd-1111-2222-3333-444444444406"),
            batch_id=_BATCH,
            sn="SN-B",
            source_type="IV_CSV",
            captured_at=_TS,
            pmax=549.0,
            temp=25.0,
            isc=10.5,
            voc=0.5,
            vm=48.0,
            im=11.0,
            ff=0.75,
            appearance_result="pass",
            decision_status="fail",
        )
    )
    session.add(
        SampleConclusion(
            conclusion_id=UUID("dddddddd-1111-2222-3333-444444444407"),
            batch_id=_BATCH,
            inspection_type="iv",
            rule_version_id=_RULE,
            sample_round=1,
            n=5,
            ac=0,
            re=1,
            result="fail",
        )
    )
    session.commit()

    resp = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    assert resp.status_code == 201, resp.text
    report_row = session.get(Report, UUID(resp.json()["report_id"]))
    decisions = report_row.content_json["decisions"]
    assert decisions["total_inspections"] == 2  # seed 的 SN-A(pending) + SN-B(fail)
    assert decisions["decision_counts"] == {"pending": 1, "fail": 1}
    assert decisions["sample_conclusions"] == [
        {
            "inspection_type": "iv",
            "sample_round": 1,
            "n": 5,
            "ac": 0,
            "re": 1,
            "result": "fail",
        }
    ]
    # 判定统计进 Markdown 正文（§5.10 模板要求）
    assert "## 判定统计" in report_row.content_md
