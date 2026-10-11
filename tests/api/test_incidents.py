"""Incident API tests (PB Task 6).

httpx ASGITransport + SQLite session factory bound via dependency_overrides.
Pins:
- create 201 + incident_sn_links row count == len(affected_sns)
- evidence_ids containing a nonexistent id -> 400
- batch not isolated -> 409
- assign: dual transition succeeds; double assign -> 409
- capa verified=true with no reinspection record -> 400; with pass
  reinspection -> 200 + capa_verified column set
- GET detail includes root_cause_hypotheses
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from app.api import incidents as incidents_router
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    IncidentSnLink,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Reinspection,
)

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_BATCH = UUID("bbbbbbbb-1111-2222-3333-444444444401")
_RULE = UUID("bbbbbbbb-1111-2222-3333-444444444402")
_INSP1 = UUID("bbbbbbbb-1111-2222-3333-444444444403")
_INSP2 = UUID("bbbbbbbb-1111-2222-3333-444444444404")
_EV1 = UUID("bbbbbbbb-1111-2222-3333-444444444405")
_RE1 = UUID("bbbbbbbb-1111-2222-3333-444444444406")


def _seed(
    session: Any,
    *,
    batch_status: str = "isolated",
    with_reinspection_pass: bool = False,
    reinspection_id: UUID | None = None,
) -> dict[str, Any]:
    """Seed a minimal batch + module inspections + optional reinspection."""
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
        batch_no="B-API-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=100,
        rule_version_id=_RULE,
        status=batch_status,
    )
    ins1 = ModuleInspection(
        inspection_id=_INSP1,
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
    ins2 = ModuleInspection(
        inspection_id=_INSP2,
        batch_id=_BATCH,
        sn="SN-B",
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
        evidence_id=_EV1,
        evidence_type="decision_snapshot",
        object_ref={"kind": "module", "id": "SN-A"},
        content_hash="sha256:000",
        generated_at=_TS,
        generated_by="test",
    )
    session.add_all([rule, batch, ins1, ins2, ev])

    incident = QualityIncident(
        incident_id=UUID("bbbbbbbb-1111-2222-3333-444444444407"),
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[str(_EV1)],
        status="new",
    )
    session.add(incident)
    session.add(IncidentSnLink(incident_id=incident.incident_id, inspection_id=_INSP1))
    session.add(IncidentSnLink(incident_id=incident.incident_id, inspection_id=_INSP2))

    if with_reinspection_pass:
        re_ = Reinspection(
            reinspection_id=reinspection_id or _RE1,
            incident_id=incident.incident_id,
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
        )
        session.add(re_)
        incident.verification_id = re_.reinspection_id
    session.commit()
    return {"incident": incident, "batch": batch, "reinspection_id": _RE1}


@pytest.fixture()
def api_client() -> Any:
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()

    app = FastAPI()
    app.include_router(incidents_router.router)
    app.dependency_overrides[incidents_router.get_session] = lambda: session

    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_create_201_and_sn_links(api_client: Any) -> None:
    client, session, _engine = api_client
    _seed(session, batch_status="isolated")
    resp = client.post(
        "/api/incidents",
        json={
            "batch_id": str(_BATCH),
            "trigger_type": "rule_fail",
            "severity": "major",
            "affected_sns": ["SN-A", "SN-B"],
            "evidence_ids": [str(_EV1)],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "new"
    new_inc = (
        session.query(QualityIncident)
        .filter(QualityIncident.incident_id == uuid.UUID(body["incident_id"]))
        .one()
    )
    links = (
        session.query(func.count(IncidentSnLink.incident_id))
        .filter(IncidentSnLink.incident_id == new_inc.incident_id)
        .scalar()
    )
    assert links == 2  # == len(affected_sns)


def test_create_evidence_missing_400(api_client: Any) -> None:
    client, session, _engine = api_client
    _seed(session, batch_status="isolated")
    resp = client.post(
        "/api/incidents",
        json={
            "batch_id": str(_BATCH),
            "trigger_type": "rule_fail",
            "severity": "major",
            "affected_sns": ["SN-A"],
            "evidence_ids": [str(UUID("cccccccc-0000-1111-2222-333333333333"))],  # ghost
        },
    )
    assert resp.status_code == 400


def test_create_batch_not_isolated_409(api_client: Any) -> None:
    client, session, _engine = api_client
    _seed(session, batch_status="pending_judge")  # not isolated
    resp = client.post(
        "/api/incidents",
        json={
            "batch_id": str(_BATCH),
            "trigger_type": "rule_fail",
            "severity": "major",
            "affected_sns": ["SN-A"],
        },
    )
    assert resp.status_code == 409


def test_assign_dual_transition_then_409(api_client: Any) -> None:
    client, session, _engine = api_client
    seeded = _seed(session, batch_status="isolated")
    iid = seeded["incident"].incident_id
    # Promote the incident to isolated (new -> isolated requires sn_linked)
    inc = session.get(QualityIncident, iid)
    inc.status = "isolated"
    session.commit()

    resp = client.post(
        f"/api/incidents/{iid}/assign",
        json={
            "owner": "ZHL-QA",
            "due_at": "2026-06-15T00:00:00Z",
            "containment_action": "quarantine",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "assigned"
    batch = session.get(DeliveryBatch, _BATCH)
    assert batch.status == "disposing"  # dual transition in one tx

    # Double assign -> 409 (incident already assigned)
    resp2 = client.post(
        f"/api/incidents/{iid}/assign",
        json={"owner": "ZHL-QA", "due_at": "2026-06-15T00:00:00Z"},
    )
    assert resp2.status_code == 409


def test_capa_verified_requires_pass_reinspection(api_client: Any) -> None:
    client, session, _engine = api_client
    seeded = _seed(session, batch_status="isolated")
    iid = seeded["incident"].incident_id
    inc = session.get(QualityIncident, iid)
    inc.status = "assigned"
    inc.owner = "ZHL-QA"
    inc.due_at = _TS
    session.get(DeliveryBatch, _BATCH).status = "disposing"
    session.commit()

    # No reinspection record -> verified=true must 400
    resp = client.post(
        f"/api/incidents/{iid}/capa",
        json={
            "confirmed_root_cause": "cell mismatch",
            "corrective_action": "replace",
            "verified": True,
        },
    )
    assert resp.status_code == 400


def test_capa_with_pass_reinspection_sets_flag(api_client: Any) -> None:
    client, session, _engine = api_client
    seeded = _seed(
        session, batch_status="isolated", with_reinspection_pass=True, reinspection_id=_RE1
    )
    iid = seeded["incident"].incident_id
    inc = session.get(QualityIncident, iid)
    inc.status = "assigned"
    inc.owner = "ZHL-QA"
    inc.due_at = _TS
    session.get(DeliveryBatch, _BATCH).status = "disposing"
    session.commit()

    resp = client.post(
        f"/api/incidents/{iid}/capa",
        json={
            "confirmed_root_cause": "cell mismatch",
            "corrective_action": "replace",
            "verified": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capa_verified"] is True
    assert body["status"] == "pending_recheck"
    assert session.get(DeliveryBatch, _BATCH).status == "pending_recheck"


def test_get_detail_includes_hypotheses(api_client: Any) -> None:
    client, session, _engine = api_client
    seeded = _seed(session, batch_status="isolated")
    iid = seeded["incident"].incident_id
    inc = session.get(QualityIncident, iid)
    inc.root_cause_hypotheses = [
        {"cause": "cell mismatch", "confidence": 0.87, "rank": 1, "evidence_ids": [str(_EV1)]}
    ]
    session.commit()

    resp = client.get(f"/api/incidents/{iid}")
    assert resp.status_code == 200
    assert resp.json()["root_cause_hypotheses"]
