"""E2E: the demo path — raw document in, governed release out.

This is the "答辩现场" chain, driven end to end through the assembled app:

    raw IV CSV text (POST /api/intake with raw_source)
      -> AI extraction (LLM proposes) + FR-1.4 validation (validator disposes)
      -> judged: two Pass + one Fail  ->  batch isolated + incident auto-created
      -> incident isolated -> AI attribution parks at human_adopt (request 1)
      -> a LATER request adopts one hypothesis (request 2, cross-request HITL)
      -> the attribution decision is persisted with a trace row.

The point of the test is the wiring, not the fixture: every step goes through
the real API + graph, and the assertions pin the parts a demo would visibly
break (per-SN confidence, the rejected raw row, the parked run surviving
between requests, the adopted root cause landing in the DB).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    IsoSamplingRow,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
)
from app.main import create_unified_app

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_RULE = UUID("cd000000-1111-2222-3333-444444444401")

#: The raw source a demo pastes: two good modules, one out-of-band power
#: (470 W against a 550 W nameplate at 3% tolerance -> Fail), one row with a
#: unit error that must be rejected rather than judged.
_RAW = """# 设备 UT-2200 导出 / 2026-06-01
SN,pmax,temp,isc,voc,vm,im,ff,appearance
SN-DEMO-001,551.2,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-DEMO-002,549.8,25.0,10.5,48.0,40.0,13.0,0.79,pass
SN-DEMO-003,470.0,25.0,10.5,47.2,40.0,12.4,0.74,fail
SN-DEMO-004,548500.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
"""


def _seed(session: Any) -> None:
    seed = json.loads((_FIXTURES / "rules_seed.json").read_text(encoding="utf-8"))["rules"][0]
    session.add(
        QualityRuleVersion(
            rule_version_id=_RULE,
            project_id=seed["project_id"],
            model=seed["model"],
            name="demo-rule",
            nameplate_power=seed["nameplate_power"],
            power_tolerance=seed["power_tolerance"],
            parameter_standards=seed["parameter_standards"],
            current_bands=seed["current_bands"],
            sample_plan=seed["sample_plan"],
            effective_at=_TS,
            status="published",
            version_no=seed["version_no"],
        )
    )
    session.add(
        IsoSamplingRow(
            inspection_type="iv",
            inspection_level="normal",
            aql=1.0,
            lot_size_min=1,
            lot_size_max=100,
            n=2,
            ac=0,
            re=1,
        )
    )
    session.commit()


def _client() -> tuple[TestClient, Any, Any]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = create_unified_app(session_factory=lambda: session, own_session=False)
    # Inject the deterministic mock explicitly. Without this the endpoint's
    # ``get_llm()`` reads ZHL_LLM_API_KEY from the developer's .env (app.main
    # calls load_dotenv) and would drive a LIVE model from a unit test —
    # slow, network-bound, and its answers cite evidence ids the D2 gate
    # rightfully rejects.
    from app.api import incidents as incidents_router
    from app.plugins.llm_client import MockLLMClient

    mock = MockLLMClient(_FIXTURES)
    app.dependency_overrides[incidents_router.get_llm] = lambda: mock
    return TestClient(app), session, engine


def _post_batch(client: TestClient, lot: int = 4) -> dict:
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": "B-DEMO-RAW-001",
            "project_id": "P-DEFAULT",
            "model": "JH-550W",
            "rule_version_id": str(_RULE),
            "planned_quantity": lot,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_demo_path_raw_document_to_governed_isolation() -> None:
    client, session, engine = _client()
    try:
        _seed(session)
        batch = _post_batch(client)

        # --- step 1: raw source -> AI extraction -> judgment ----------------
        resp = client.post(
            "/api/intake",
            json={
                "batch_id": batch["batch_id"],
                "source_type": "IV_CSV",
                "raw_source": _RAW,
                "raw_source_kind": "iv_csv",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # The unit-error row is reported as an issue, not judged as quality.
        issues = body["raw_parse"]["issues"]
        assert any(i["sn"] == "SN-DEMO-004" and i["field"] == "pmax" for i in issues)
        assert body["raw_parse"]["rows"] == 3

        # Per-SN extraction is visible to the caller (the UI renders it).
        extracted = body["extracted_fields"]
        assert set(extracted) == {"SN-DEMO-001", "SN-DEMO-002", "SN-DEMO-003"}
        assert all(0.0 <= r["confidence"] <= 1.0 for r in extracted.values())

        # --- step 2: the Fail row drove the batch to isolation -------------
        batch_row = session.get(DeliveryBatch, UUID(batch["batch_id"]))
        session.refresh(batch_row)
        assert batch_row.status == "isolated", batch_row.status

        incidents = session.query(QualityIncident).all()
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.trigger_type == "rule_fail"
        # The incident is linked to the failing SN, not the whole batch.
        link_sns = [
            session.get(ModuleInspection, link.inspection_id).sn
            for link in session.query(
                __import__("app.infra.models", fromlist=["IncidentSnLink"]).IncidentSnLink
            ).all()
        ]
        assert "SN-DEMO-003" in link_sns

        # Evidence chain: raw text + per-SN extraction all archived.
        types = [row.evidence_type for row in session.query(Evidence).all()]
        assert "source_file" in types
        assert types.count("extraction_result") >= 3
    finally:
        session.close()
        engine.dispose()


def test_demo_path_cross_request_attribution_hitl() -> None:
    """The HITL the demo shows: generate (request 1) -> adopt (request 2)."""
    client, session, engine = _client()
    try:
        _seed(session)
        batch = _post_batch(client)
        batch_uuid = UUID(batch["batch_id"])
        # Bring the batch to isolated via the raw path (same as the demo).
        client.post(
            "/api/intake",
            json={"batch_id": batch["batch_id"], "raw_source": _RAW},
        )
        incident = session.query(QualityIncident).one()
        # The incident must be isolated before attribution (state machine).
        resp = client.post(f"/api/incidents/{incident.incident_id}/isolate")
        assert resp.status_code == 200, resp.text
        session.refresh(incident)
        assert incident.status == "isolated"

        # Give the incident real evidence ids: the synthetic fallback cites
        # these in its hypotheses, and the D2 gate checks they exist.
        from app.plugins.evidence_store import make_evidence_id

        ev_ids = []
        for label in ("EV-DEMO-001", "EV-DEMO-002"):
            eid = make_evidence_id({"kind": "batch", "id": label}, "decision_snapshot")
            ev_ids.append(eid)
            session.add(
                Evidence(
                    evidence_id=UUID(eid),
                    evidence_type="decision_snapshot",
                    object_ref={"kind": "batch", "id": label},
                    content_hash=f"sha256:{label}",
                    generated_at=_TS,
                    generated_by="seed",
                )
            )
        # Stored on the incident row: this is what the prompt advertises.
        incident = session.get(QualityIncident, incident.incident_id)
        incident.evidence_ids = ev_ids
        session.commit()

        # --- request 1: generate + park -------------------------------------
        start = client.post(f"/api/incidents/{incident.incident_id}/attribute", json={})
        assert start.status_code == 200, start.text
        started = start.json()
        assert started["status"] == "awaiting_human"
        assert started["hypotheses"], started
        assert started["hypotheses_source"] == "attribution"
        session.refresh(incident)
        assert incident.confirmed_root_cause is None, "nothing adopted yet"

        # --- request 2 (a separate call): the human adopts -------------------
        cause = started["hypotheses"][0]["cause"]
        adopt = client.post(
            f"/api/incidents/{incident.incident_id}/adopt",
            json={"decision": "adopt", "cause": cause},
        )
        assert adopt.status_code == 200, adopt.text
        adopted = adopt.json()
        assert adopted["decision"] == "adopt"
        session.refresh(incident)
        assert incident.confirmed_root_cause == cause
        assert incident.root_cause_hypotheses, "hypotheses persisted for the audit trail"

        # The batch is still isolated: adopting a cause is not a release.
        batch_row = session.get(DeliveryBatch, batch_uuid)
        session.refresh(batch_row)
        assert batch_row.status == "isolated"
    finally:
        session.close()
        engine.dispose()


__all__ = [
    "test_demo_path_raw_document_to_governed_isolation",
    "test_demo_path_cross_request_attribution_hitl",
]
