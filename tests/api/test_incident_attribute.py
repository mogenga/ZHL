"""Attribute endpoint tests (T-1 Task 3 — single-request 3-way attribution).

httpx ASGITransport + SQLite session factory bound via dependency_overrides
(same pattern as tests/api/test_incidents.py). The attribution LLM client and
evidence store are injected as additional dependencies so the router is
testable without a live model or a real data/evidence dir.

Pins:
- adopt writes confirmed_root_cause + persists root_cause_hypotheses
- D8: archived batch -> 409
- unknown incident id (no mock fixture key) -> 200 + empty hypotheses + hint
- D2: no-evidence hypotheses dropped
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import incidents as incidents_router
from app.infra.models import (
    Base,
    DeliveryBatch,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
)

# Same canonical UUID key Task 2 added to the shared fixture. The fixture's
# hypotheses reference "EV-ATT-*" labels; the D2 gate resolves them because
# the test seeds those labels THROUGH the endpoint's overridden
# FileEvidenceStore (save() populates the in-memory ref table the gate's
# store.get reads). The endpoint returns the LLM fixture's label ids.
_ATTRIBUTION_INCIDENT = "8f6e2a10-0000-4000-8000-0000000000aa"
_ATTRIBUTION_INCIDENT_ID = UUID(_ATTRIBUTION_INCIDENT)
# An incident whose UUID has no mock fixture key -> 200 + empty + hint.
_UNKNOWN_INCIDENT_ID = UUID("99999999-1111-2222-3333-444444444409")
_BATCH = UUID("bbbbbbbb-1111-2222-3333-444444444410")
_RULE = UUID("bbbbbbbb-1111-2222-3333-444444444411")
_INSP1 = UUID("bbbbbbbb-1111-2222-3333-444444444412")
_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)


def _seed(session: Any, *, batch_status: str = "isolated") -> None:
    """Seed a minimal batch + module inspection + incidents for the attribute
    endpoint. The attribution incident uses the Task-2 UUID key; the unknown
    incident uses a UUID with no fixture key.
    """
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
        batch_no="B-ATTR-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=100,
        rule_version_id=_RULE,
        status=batch_status,
    )
    ins = ModuleInspection(
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
    session.add_all([rule, batch, ins])

    attribution_incident = QualityIncident(
        incident_id=_ATTRIBUTION_INCIDENT_ID,
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[],
        status="isolated",
    )
    unknown_incident = QualityIncident(
        incident_id=_UNKNOWN_INCIDENT_ID,
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[],
        status="isolated",
    )
    session.add_all([attribution_incident, unknown_incident])
    session.commit()


@pytest.fixture()
def api_client(tmp_path: Any) -> Any:
    from app.plugins.evidence_store import FileEvidenceStore
    from app.plugins.llm_client import MockLLMClient

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()

    fixtures_dir = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    llm = MockLLMClient(fixtures_dir)
    evidence_store = FileEvidenceStore(tmp_path, db=session)
    # Seed the fixture evidence ids THROUGH the store so the D2 gate (the
    # endpoint's overridden FileEvidenceStore) finds them; the store is
    # DB-bound so the same save also INSERTs the evidence rows.
    for _eid in ("EV-ATT-001", "EV-ATT-002", "EV-ATT-003", "EV-ATT-009"):
        evidence_store.save(
            "attribution_fixture",
            {"kind": "fixture", "id": _eid},
            content=b"t",
        )
    session.commit()

    app = FastAPI()
    app.include_router(incidents_router.router)
    app.dependency_overrides[incidents_router.get_session] = lambda: session
    app.dependency_overrides[incidents_router.get_llm] = lambda: llm
    app.dependency_overrides[incidents_router.get_evidence_store] = lambda: evidence_store

    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_attribute_adopt_writes_confirmed_cause(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute",
        json={"decision": "adopt", "cause": "焊接虚焊，建议重焊"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "adopt"
    assert body["incident_id"] == str(_ATTRIBUTION_INCIDENT_ID)
    # Hypotheses persisted to the incident row.
    incident = session.get(QualityIncident, _ATTRIBUTION_INCIDENT_ID)
    assert incident.confirmed_root_cause == "焊接虚焊，建议重焊"
    assert incident.root_cause_hypotheses is not None
    hypos = incident.root_cause_hypotheses
    assert isinstance(hypos, list) and hypos, "D2: at least one with-evidence hypothesis"


def test_attribute_d8_archived_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, batch_status="archived")
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute",
        json={"decision": "adopt", "cause": "x"},
    )
    assert resp.status_code == 409
    assert "archived" in str(resp.json().get("detail", ""))


def test_attribute_unknown_incident_empty_hypotheses(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_UNKNOWN_INCIDENT_ID}/attribute",
        json={"decision": "defer"},
    )
    # No mock fixture key for this UUID -> 200 + empty + hint, not 500/404.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hypotheses"] == []
    assert "hint" in body
    assert body["decision"] == "defer"
    incident = session.get(QualityIncident, _UNKNOWN_INCIDENT_ID)
    # defer must not write confirmed_root_cause.
    assert incident.confirmed_root_cause is None


def test_attribute_d2_no_evidence_dropped(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute",
        json={"decision": "defer"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # D2 gate: every surviving hypothesis carries >=1 evidence id.
    for h in body["hypotheses"]:
        assert h.get("evidence_ids"), "no-evidence hypotheses must be dropped (D2)"
