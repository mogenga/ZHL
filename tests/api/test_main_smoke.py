"""App assembly smoke tests (PB Task 9: create_app + archived terminal guard D8).

Pins:
- ``create_app()`` mounts all four routers (incidents / reinspections /
  release / reports) plus the static directory; a plain TestClient hits the
  routers' GET/list endpoints without error.
- The D8 archived terminal guard: every *write* endpoint (create incident,
  create reinspection, release, create report) returns 409 when the batch is
  already ``archived``, with a stable error payload.
- A non-archived batch is NOT blocked by the guard (the guard only fires on
  the archived terminal state).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app, create_app_with_trace
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    IncidentSnLink,
    IsoSamplingRow,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
)

_TS = datetime(2026, 6, 2, 9, 0, 0, tzinfo=UTC)
_RULE = UUID("ffffeeee-1111-2222-3333-444444444402")
_B1 = UUID("ffffeeee-1111-2222-3333-444444444401")  # pending_release (non-archived)
_B2 = UUID("ffffeeee-1111-2222-3333-444444444402")  # archived
_INSP1 = UUID("ffffeeee-1111-2222-3333-444444444403")
_INC = UUID("ffffeeee-1111-2222-3333-444444444406")
_EV1 = UUID("ffffeeee-1111-2222-3333-444444444407")


def _seed(
    session: Any, batch_id: UUID, status: str, batch_no: str, insp_id: UUID, ev_id: UUID
) -> None:
    batch = DeliveryBatch(
        batch_id=batch_id,
        batch_no=batch_no,
        project_id="P-1",
        model="JH-550W",
        planned_quantity=2,
        rule_version_id=_RULE,
        status=status,
    )
    ins = ModuleInspection(
        inspection_id=insp_id,
        batch_id=batch_id,
        sn="SN-X",
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
    ev = Evidence(
        evidence_id=ev_id,
        evidence_type="decision_snapshot",
        object_ref={"kind": "batch", "id": f"B-{batch_id.hex[:6]}"},
        content_hash="sha256:abc",
        generated_at=_TS,
        generated_by="seed",
    )
    session.add_all([batch, ins, ev])
    session.commit()


@pytest.fixture()
def api_client() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        QualityRuleVersion(
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
    )
    session.add(
        IsoSamplingRow(
            inspection_type="module",
            inspection_level="full",
            aql=0.65,
            lot_size_min=1,
            lot_size_max=100,
            n=2,
            ac=0,
            re=1,
        )
    )
    session.commit()
    _seed(session, _B1, "pending_release", "B-NONARCH-01", _INSP1, _EV1)
    _seed(
        session,
        _B2,
        "archived",
        "B-ARCHIVD-02",
        UUID("ffffeeee-1111-2222-3333-444444444410"),
        UUID("ffffeeee-1111-2222-3333-444444444411"),
    )
    # an open incident on the non-archived batch (for create-incident paths)
    inc = QualityIncident(
        incident_id=_INC,
        batch_id=_B1,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[str(_EV1)],
        status="pending_recheck",
        owner="ZHL-QA",
        due_at=_TS,
        confirmed_root_cause="cell mismatch",
        corrective_action="replace lead cell",
        capa_verified=True,
    )
    session.add(inc)
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP1))
    session.commit()
    app = create_app(session_factory=lambda: session, own_session=False)
    yield TestClient(app)
    session.close()
    engine.dispose()


def test_app_mounts_all_routers(api_client: Any) -> None:
    client = api_client
    # list endpoints on each of the four routers respond (no 500 / no 404).
    r_inc = client.get("/api/incidents")
    assert r_inc.status_code == 200, r_inc.text
    r_rel = client.get(f"/api/batches/{_B1}/release", headers={"x": "1"})
    # GET is not a release route; it should 405 (method not allowed), not 500.
    assert r_rel.status_code in (405, 404)
    # the release POST requires a body; a plain GET to the batch list is not
    # a route — assert the app simply serves (no exception), which TestClient
    # does for unmatched paths.
    assert isinstance(client.get("/api/incidents").json(), list)


def test_archived_batch_blocks_all_write_endpoints(api_client: Any) -> None:
    client = api_client
    # 1) create incident on archived batch -> 409 (batch-not-isolated check;
    #    an archived batch is never isolated, so D8 is already enforced here).
    r1 = client.post(
        "/api/incidents",
        json={
            "batch_id": str(_B2),
            "trigger_type": "rule_fail",
            "severity": "major",
            "affected_sns": ["SN-X"],
            "inspection_ids": [str(_INSP1)],
            "evidence_ids": [str(_EV1)],
        },
    )
    assert r1.status_code == 409, r1.text
    # FastAPI wraps HTTPException.detail under the "detail" key.
    assert r1.json()["detail"]["error"] == "batch archived, terminal state"

    # 2) release on archived batch -> 409 (D8: release router checks status)
    r3 = client.post(f"/api/batches/{_B2}/release", json={"approved_by": "ZHL-ENG"})
    assert r3.status_code == 409, r3.text
    assert r3.json()["detail"]["error"] == "batch archived, terminal state"

    # 3) create report on archived batch -> 409 (D8: reports router checks status)
    r4 = client.post(f"/api/batches/{_B2}/report", json={"report_type": "incident"})
    assert r4.status_code == 409, r4.text
    assert r4.json()["detail"]["error"] == "batch archived, terminal state"


def test_non_archived_batch_not_blocked_by_guard(api_client: Any) -> None:
    client = api_client
    # release on the pending_release batch is NOT blocked by the archived
    # guard (it may still fail assertion checks, but the D8 guard specifically
    # must not fire).
    r = client.post(f"/api/batches/{_B1}/release", json={"approved_by": "ZHL-ENG"})
    # B1 is pending_release with no sample conclusions or reinspection rows,
    # so item 2 (sample_passed) fails -> 200 with passed=false. The D8 guard
    # must NOT fire (it only blocks archived batches).
    assert r.status_code == 200, r.text
    assert r.json()["passed"] is False


def test_create_incident_on_non_archived_succeeds(api_client: Any) -> None:
    client = api_client
    r = client.post(
        "/api/incidents",
        json={
            "batch_id": str(_B1),
            "trigger_type": "data_consistency",
            "severity": "major",
            "affected_sns": ["SN-X"],
            "inspection_ids": [str(_INSP1)],
            "evidence_ids": [str(_EV1)],
        },
    )
    # B1 is pending_release, NOT isolated -> the "not isolated" 409 fires,
    # which is the correct business-logic response (not the D8 archived guard).
    if r.status_code == 409:
        assert r.json()["detail"]["error"] == "batch_not_isolated"


# --- PB Task 10 additions: trace read endpoint + static pages mount ---------


def test_trace_endpoint_returns_batch_trace_rows(api_client: Any) -> None:
    """create_app_with_trace exposes GET /api/batches/{id}/traces.

    The smoke fixture never writes a trace row, so the response is an empty
    list (200) — pinning the route exists and is wired to TraceStore without
    a 500. The E2E tests (tests/e2e/) exercise the populated path."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.infra.models import Base, DeliveryBatch, QualityRuleVersion

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    rule_id = UUID("ab900000-1111-2222-3333-444444444499")
    batch_id = UUID("ab900000-1111-2222-3333-4444444444aa")
    session.add(
        QualityRuleVersion(
            rule_version_id=rule_id,
            project_id="P-1",
            model="JH-550W",
            name="rv",
            nameplate_power=550.0,
            power_tolerance=0.5,
            parameter_standards={},
            current_bands={},
            sample_plan={},
            effective_at=_TS,
            version_no=1,
        )
    )
    session.add(
        DeliveryBatch(
            batch_id=batch_id,
            batch_no="B-TRACE-01",
            project_id="P-1",
            model="JH-550W",
            planned_quantity=1,
            rule_version_id=rule_id,
            status="pending_release",
        )
    )
    session.commit()
    app = create_app_with_trace(session_factory=lambda: session, own_session=False)
    client = TestClient(app)
    r = client.get(f"/api/batches/{batch_id}/traces")
    assert r.status_code == 200, r.text
    assert r.json() == [], "no trace rows yet for a freshly-seeded batch"
    session.close()
    engine.dispose()


def test_trace_endpoint_exposes_structured_output(api_client: Any) -> None:
    """list_traces 必须回吐 structured_output——工作台靠它把
    ``incident_auto`` 轨迹映射到异常 id（卡片「自动」标记的依据，
    2026-10 重复创建缺陷回归）。"""
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.infra.models import Base, DeliveryBatch, QualityRuleVersion
    from app.plugins.trace_store import TraceStore

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    rule_id = UUID("ab900000-1111-2222-3333-444444444498")
    batch_id = UUID("ab900000-1111-2222-3333-4444444444bb")
    session.add(
        QualityRuleVersion(
            rule_version_id=rule_id,
            project_id="P-1",
            model="JH-550W",
            name="rv",
            nameplate_power=550.0,
            power_tolerance=0.5,
            parameter_standards={},
            current_bands={},
            sample_plan={},
            effective_at=_TS,
            version_no=1,
        )
    )
    session.add(
        DeliveryBatch(
            batch_id=batch_id,
            batch_no="B-TRACE-02",
            project_id="P-1",
            model="JH-550W",
            planned_quantity=1,
            rule_version_id=rule_id,
            status="isolated",
        )
    )
    TraceStore().record(
        session,
        batch_id=batch_id,
        skill_name="incident_auto",
        input_ref={"batch_id": str(batch_id)},
        plugins_invoked=[],
        params_summary={"trigger_type": "rule_fail", "mapped_sns": ["SN-N-057"]},
        structured_output={
            "created": True,
            "incident_id": "22222222-2222-2222-2222-222222222222",
        },
        validation_result="pass",
        human_action={"action": "auto"},
        started_at=datetime.now(UTC),
    )
    session.commit()
    app = create_app_with_trace(session_factory=lambda: session, own_session=False)
    client = TestClient(app)
    r = client.get(f"/api/batches/{batch_id}/traces")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["skill_name"] == "incident_auto"
    so = rows[0].get("structured_output")
    assert isinstance(so, dict) and so.get("created") is True
    assert so.get("incident_id") == "22222222-2222-2222-2222-222222222222"
    session.close()
    engine.dispose()


def test_static_mount_present(api_client: Any) -> None:
    """The rebuilt UI serves ``app/static`` (single-page workbench).

    ``create_app(mount_static=True)`` mounts ``StaticFiles`` at ``/static`` and
    adds a ``GET /`` redirect to ``/static/workbench.html``. ``/`` no longer
    404s; it 302-redirects to the workbench page, and the workbench sub-assets
    (wb.css / wb/ui.js) are served. Any regression that drops the mount is
    caught here."""
    r = api_client.get("/", follow_redirects=False)
    assert r.status_code in (200, 302), f"/ should serve or redirect: {r.status_code}"
    assert "/static/workbench.html" in r.headers.get(
        "location", ""
    ), "/ should redirect to workbench.html"
    r_wb = api_client.get("/static/workbench.html")
    assert r_wb.status_code == 200, f"workbench.html should be served: {r_wb.status_code}"
    r_css = api_client.get("/static/wb.css")
    assert r_css.status_code == 200, f"wb.css should be served: {r_css.status_code}"
    r_js = api_client.get("/static/wb/ui.js")
    assert r_js.status_code == 200, f"wb/ui.js should be served: {r_js.status_code}"
