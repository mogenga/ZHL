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
