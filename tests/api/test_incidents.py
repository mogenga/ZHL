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
    session.add_all([rule, ev])
    session.flush()
    session.add(batch)
    session.flush()
    session.add_all([ins1, ins2])

    incident = QualityIncident(
        incident_id=UUID("bbbbbbbb-1111-2222-3333-444444444407"),
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[str(_EV1)],
        status="new",
    )
    session.add(incident)
    session.flush()
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
        # 循环外键：复检行必须先落库再回填 verification_id（同 reinspections.py）
        session.flush()
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


def test_assign_new_state_409_r3(api_client: Any) -> None:
    """R3 regression: assign must NOT work from 'new' state.

    P0 frozen incident state machine: new -> isolated -> assigned.
    The 'new' state must first be isolated (via POST /{id}/isolate, R2)
    before assign is accepted. A direct assign from 'new' returns 409 with
    a hint to isolate first.
    """
    client, session, _engine = api_client
    seeded = _seed(session, batch_status="isolated")
    iid = seeded["incident"].incident_id
    # Reset the incident to 'new' state (fresh create, not yet isolated)
    inc = session.get(QualityIncident, iid)
    inc.status = "new"
    session.commit()

    resp = client.post(
        f"/api/incidents/{iid}/assign",
        json={
            "owner": "ZHL-QA",
            "due_at": "2026-06-15T00:00:00Z",
            "containment_action": "quarantine",
        },
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "incident_not_isolated"
    assert "isolate" in body["detail"].get("hint", "")

    # Now isolate first, then assign should succeed
    iso_resp = client.post(f"/api/incidents/{iid}/isolate")
    assert iso_resp.status_code == 200, iso_resp.text
    assert iso_resp.json()["status"] == "isolated"

    ok_resp = client.post(
        f"/api/incidents/{iid}/assign",
        json={
            "owner": "ZHL-QA",
            "due_at": "2026-06-15T00:00:00Z",
            "containment_action": "quarantine",
        },
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert ok_resp.json()["status"] == "assigned"


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


def test_isolate_new_to_isolated(api_client: Any) -> None:
    """R2: POST /isolate drives the P0 edge new -> isolated (sn_linked holds
    because _seed writes incident_sn_links at create)."""
    client, session, _ = api_client
    seeded = _seed(session, batch_status="isolated")
    iid = seeded["incident"].incident_id
    assert session.get(QualityIncident, iid).status == "new"
    resp = client.post(f"/api/incidents/{iid}/isolate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "isolated"
    assert session.get(QualityIncident, iid).status == "isolated"


def test_isolate_not_new_409(api_client: Any) -> None:
    """R2: isolating an incident that already left ``new`` -> 409."""
    client, session, _ = api_client
    seeded = _seed(session, batch_status="isolated")
    iid = seeded["incident"].incident_id
    inc = session.get(QualityIncident, iid)
    inc.status = "isolated"
    session.commit()
    resp = client.post(f"/api/incidents/{iid}/isolate")
    assert resp.status_code == 409


def test_isolate_unknown_404(api_client: Any) -> None:
    client, _session, _ = api_client
    resp = client.post(f"/api/incidents/{UUID('00000000-0000-0000-0000-000000000099')}/isolate")
    assert resp.status_code == 404


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


def _pending_recheck_with_pass_reinspection(session: Any) -> Any:
    """造一个「已有通过复检、已验证、停在待复检」的异常（关闭前的真实态）。"""
    _seed(session, batch_status="isolated", with_reinspection_pass=True, reinspection_id=_RE1)
    inc = session.get(QualityIncident, UUID("bbbbbbbb-1111-2222-3333-444444444407"))
    inc.status = "pending_recheck"
    inc.capa_verified = True
    inc.confirmed_root_cause = "cell mismatch"
    inc.corrective_action = "replace lead cell"
    session.commit()
    return inc


def test_capa_fills_containment_action_at_pending_recheck(api_client: Any) -> None:
    """临时措施在待复检态可补填（2026-10-15 反馈：指派时漏填则永远关不掉）。

    指派端点只接受 ``isolated``（incidents.py 状态门），而 close 硬要求
    ``containment_action`` 非空——中间没有任何入口的话，漏填即等于该异常
    永远无法关闭、批次永远无法放行。
    """
    client, session, _engine = api_client
    inc = _pending_recheck_with_pass_reinspection(session)

    resp = client.post(
        f"/api/incidents/{inc.incident_id}/capa",
        json={
            "confirmed_root_cause": "cell mismatch",
            "corrective_action": "replace lead cell",
            "containment_action": "halt line, segregate lot",
        },
    )
    assert resp.status_code == 200, resp.text
    row = session.get(QualityIncident, inc.incident_id)
    assert row.containment_action == "halt line, segregate lot"
    assert row.status == "pending_recheck"  # 无状态转移


def test_capa_without_verified_leaves_the_flag_alone(api_client: Any) -> None:
    """未显式传 verified 时不得把它刷成 false。

    复检通过后该标记由系统自动置位；「补填临时措施」这类局部更新若默认带上
    ``verified=false``，就会把已验证悄悄洗掉，关闭条件重新变缺——前端漏传一个
    字段就复发卡死。缺省 = 不改。
    """
    client, session, _engine = api_client
    inc = _pending_recheck_with_pass_reinspection(session)

    resp = client.post(
        f"/api/incidents/{inc.incident_id}/capa",
        json={
            "confirmed_root_cause": "cell mismatch",
            "corrective_action": "replace lead cell",
            "containment_action": "halt line",
        },
    )
    assert resp.status_code == 200, resp.text
    assert session.get(QualityIncident, inc.incident_id).capa_verified is True
