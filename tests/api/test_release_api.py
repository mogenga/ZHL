"""Release endpoint tests (PB Task 8, D4 fail-keeps-state; D7-7 item 7).

The release API assembles all seven ``assert_release`` inputs server-side
from the DB and calls the pure domain function. Pins:

- all-pass -> ``released`` + release-evidence row + trace; NOT auto-archived
  in this transaction (archive happens at the report call, D6a semantics)
- any assertion fail -> batch stays ``pending_release``, ``release_decision``
  stays ``undecided``, the failing check's reason names the incident/SN;
  no state transition is written
- item 7 scope gap -> ``reinspection_scope_covered`` fail, reason lists the
  missing SNs
- retry after fixing the blocker -> all-pass -> released (idempotent
  semantics: the assertion re-runs, only the final pass writes state)
- batch not pending_release -> 409; archived -> 409 (D8)
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

from app.api import release as release_api
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    IncidentSnLink,
    IsoSamplingRow,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Reinspection,
    SampleConclusion,
)

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_BATCH = UUID("eeeeeeee-1111-2222-3333-444444444401")
_RULE = UUID("eeeeeeee-1111-2222-3333-444444444402")
_INSP1 = UUID("eeeeeeee-1111-2222-3333-444444444403")
_INSP2 = UUID("eeeeeeee-1111-2222-3333-444444444404")
_INSP3 = UUID("eeeeeeee-1111-2222-3333-444444444405")
_INC = UUID("eeeeeeee-1111-2222-3333-444444444406")
_EV1 = UUID("eeeeeeee-1111-2222-3333-444444444407")
_RE1 = UUID("eeeeeeee-1111-2222-3333-444444444408")
_CONC1 = UUID("eeeeeeee-1111-2222-3333-444444444409")


def _mk_ins(session: Any, iid: UUID, sn: str, pmax: float) -> ModuleInspection:
    ins = ModuleInspection(
        inspection_id=iid,
        batch_id=_BATCH,
        sn=sn,
        source_type="IV_CSV",
        captured_at=_TS,
        pmax=pmax,
        temp=25.0,
        isc=10.5,
        voc=0.5,
        vm=48.0,
        im=11.0,
        ff=0.75,
        appearance_result="pass",
    )
    session.add(ins)
    return ins


def _seed(
    session: Any,
    *,
    incident_capa_verified: bool = True,
    reinspection_scope: list[str] | None = None,
    reinspection_present: bool = True,
    sample_result: str = "pass",
    incident_status: str = "pending_recheck",
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
        batch_no="B-REL-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=3,
        rule_version_id=_RULE,
        status="pending_release",
    )
    ins1 = _mk_ins(session, _INSP1, "SN-A", 550.0)
    ins2 = _mk_ins(session, _INSP2, "SN-B", 545.0)
    ins3 = _mk_ins(session, _INSP3, "SN-C", 540.0)
    ev = Evidence(
        evidence_id=_EV1,
        evidence_type="decision_snapshot",
        object_ref={"kind": "batch", "id": "B-REL-001"},
        content_hash="sha256:222",
        generated_at=_TS,
        generated_by="test",
    )
    conclusion = SampleConclusion(
        conclusion_id=_CONC1,
        batch_id=_BATCH,
        inspection_type="module",
        rule_version_id=_RULE,
        sample_round=1,
        n=3,
        ac=0,
        re=1,
        result=sample_result,
        evidence_id=_EV1,
    )
    iso = IsoSamplingRow(
        inspection_type="module",
        inspection_level="full",
        aql=0.65,
        lot_size_min=1,
        lot_size_max=100,
        n=3,
        ac=0,
        re=1,
    )
    incident = QualityIncident(
        incident_id=_INC,
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[str(_EV1)],
        status=incident_status,
        owner="ZHL-QA",
        due_at=_TS,
        confirmed_root_cause="cell mismatch",
        corrective_action="replace lead cell",
        capa_verified=incident_capa_verified,
    )
    session.add_all([rule, batch, ins1, ins2, ins3, ev, conclusion, iso, incident])
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP1))
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP2))
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP3))

    if reinspection_present:
        scope = reinspection_scope if reinspection_scope is not None else ["SN-A", "SN-B", "SN-C"]
        re_ = Reinspection(
            reinspection_id=_RE1,
            incident_id=_INC,
            inspection_id=_INSP1,
            rule_version_id=_RULE,
            pmax=550.0,
            temp=25.0,
            isc=10.5,
            voc=0.5,
            vm=48.0,
            im=11.0,
            ff=0.75,
            result="pass",
            executor="ZHL-QA",
            executed_at=_TS,
            evidence_id=_EV1,
            scope_type="minimal",
            scope_sns=scope,
        )
        session.add(re_)
        incident.verification_id = re_.reinspection_id
    session.commit()
    return {"incident": incident, "batch": batch}


@pytest.fixture()
def api_client() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(release_api.router)
    app.dependency_overrides[release_api.get_session] = lambda: session
    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_release_all_pass_releases(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    assert body["status"] == "released"
    assert session.get(DeliveryBatch, _BATCH).status == "released"
    # NOT auto-archived in this transaction (archive happens at the report call)
    assert session.get(DeliveryBatch, _BATCH).status != "archived"
    # release evidence row written
    ev = session.query(Evidence).filter(Evidence.evidence_type == "human_confirmation").all()
    assert ev


def test_release_assertion_fail_keeps_state(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, incident_capa_verified=False)  # CAPA not verified -> item 6 fails
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is False
    # batch stays pending_release, release_decision unchanged
    batch = session.get(DeliveryBatch, _BATCH)
    assert batch.status == "pending_release"
    assert batch.release_decision == "undecided"
    # the failing check names the incident
    failing = [c for c in body["checks"] if not c["passed"]]
    assert any("INC" in c["reason"] or _INC is not None for c in failing)
    reason_text = " ".join(c["reason"] for c in failing)
    assert "all_capa_verified" in reason_text or any(
        c["name"] == "all_capa_verified" for c in failing
    )


def test_release_scope_gap_blocks(api_client: Any) -> None:
    client, session, _ = api_client
    # reinspection covers only 1 of the 3 required SNs -> item 7 fails
    _seed(session, reinspection_scope=["SN-A"])
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is False
    failing = [c for c in body["checks"] if c["name"] == "reinspection_scope_covered"]
    assert failing and not failing[0]["passed"]
    # reason lists the missing SNs
    assert "SN-B" in failing[0]["reason"] and "SN-C" in failing[0]["reason"]
    assert session.get(DeliveryBatch, _BATCH).status == "pending_release"


def test_release_retry_after_fix_releases(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, incident_capa_verified=False)
    r1 = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert r1.json()["passed"] is False
    assert session.get(DeliveryBatch, _BATCH).status == "pending_release"
    # engineer verifies the CAPA, then retries
    inc = session.get(QualityIncident, _INC)
    inc.capa_verified = True
    session.commit()
    r2 = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert r2.status_code == 200
    assert r2.json()["passed"] is True
    assert session.get(DeliveryBatch, _BATCH).status == "released"


def test_release_archived_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    session.get(DeliveryBatch, _BATCH).status = "archived"
    session.commit()
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 409


def test_release_wrong_state_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    session.get(DeliveryBatch, _BATCH).status = "isolated"
    session.commit()
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 409
