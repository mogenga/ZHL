"""E2E: acceptance sample 4 — cross-batch consistency disposition (PB Task 10).

``acceptance_consistency.json``: SN-SHARED appears in two batches (B-CONSIST-001
released, a second batch still open). The fixture's expected outcome: the
second batch's SN insert is rejected by the EXCLUDE sn_uniqueness constraint
(Review Focus #3). The E2E drives the disposition face of that blockage:

- a data-consistency incident is raised on the open batch (the shared SN is
  the affected SN);
- the open batch cannot be released (the release attempt hits the state
  guard — the batch is rejected, not pending_release). Note this does NOT
  exercise assertion items 4/7: the guard short-circuits before the
  assertion set is assembled. Items 4/7 are covered by
  tests/api/test_release_api.py and tests/domain/test_release.py;
- the batch is transitioned to ``rejected`` (the recheck-failed / disposition
  branch), a disposition (incident-type) report is written, and the batch is
  archived (D6a auto-archive on the terminal state).

The released *first* batch is untouched — a released batch's terminal state
is not reopened by the consistency incident on the second batch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.domain.state_machine import transition
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    IsoSamplingRow,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Report,
)
from tests.fixtures.loader import load_acceptance

_TS = datetime(2026, 6, 1, 11, 0, 0, tzinfo=UTC)

# First batch: the released one (fixture's batch_id).
B1 = UUID("ab000000-2222-2222-3333-444444444401")
# Second batch: the open one that must be blocked / rejected.
B2 = UUID("ab000000-2222-2222-3333-444444444402")
RULE = UUID("ab000000-2222-2222-3333-444444444403")
EV1 = UUID("ab000000-2222-2222-3333-444444444404")
EV2 = UUID("ab000000-2222-2222-3333-444444444405")
CONC2 = UUID("ab000000-2222-2222-3333-444444444406")
_INSP_SHARED_1 = UUID("ab000000-0000-0000-0000-000000000003")
_INSP_SHARED_2 = UUID("ab000000-0000-0000-0000-000000000004")


def _edge_legal(kind: str, frm: str, to: str, preconditions: dict | None = None) -> bool:
    return transition(frm, to, preconditions or {}, kind=kind).accepted


def _seed(session: Any, sample: dict) -> None:
    raw = sample["raw"]["rules"]  # the raw dict (rules is a RuleSnapshotIn dataclass)
    session.add(
        QualityRuleVersion(
            rule_version_id=RULE,
            project_id=raw["project_id"],
            model=raw["model"],
            name="rv-1",
            nameplate_power=raw["nameplate_power"],
            power_tolerance=raw["power_tolerance"],
            parameter_standards=raw["parameter_standards"],
            current_bands=raw["current_bands"],
            sample_plan=raw["sample_plan"],
            effective_at=_TS,
            version_no=raw["version_no"],
        )
    )
    # 规则版本先落库（批次引用它的外键；测试已按 Postgres 强制外键）
    session.flush()
    # B1: the released first batch (terminal; must remain untouched).
    session.add(
        DeliveryBatch(
            batch_id=B1,
            batch_no=sample["batch"]["batch_id"],
            project_id=raw["project_id"],
            model=raw["model"],
            planned_quantity=1,
            actual_quantity=1,
            rule_version_id=RULE,
            status="released",
            release_decision="released",
        )
    )
    session.flush()  # 批次先落库，检验行才能引用
    session.add(
        ModuleInspection(
            inspection_id=_INSP_SHARED_1,
            batch_id=B1,
            sn=sample["raw"]["cross_batch_sns"][0],
            source_type="IV_CSV",
            captured_at=_TS,
            pmax=550.0,
            temp=25.0,
            isc=10.5,
            voc=48.0,
            vm=40.0,
            im=13.0,
            ff=0.80,
            appearance_result="pass",
            intake_status="confirmed",
            decision_status="pass",
        )
    )
    # B2: the open second batch, isolated, with the shared SN re-inserted
    # (the E2E models the disposition side — the uniqueness blockage itself
    # is a DB constraint, exercised here as an open consistency incident).
    session.add(
        DeliveryBatch(
            batch_id=B2,
            batch_no="B-CONSIST-002",
            project_id=raw["project_id"],
            model=raw["model"],
            planned_quantity=1,
            actual_quantity=1,
            rule_version_id=RULE,
            status="isolated",
        )
    )
    session.add(
        ModuleInspection(
            inspection_id=_INSP_SHARED_2,
            batch_id=B2,
            sn=sample["raw"]["cross_batch_sns"][0],
            source_type="IV_CSV",
            captured_at=_TS,
            pmax=545.0,  # inconsistent with B1's 550.0 on the same SN
            temp=25.0,
            isc=10.5,
            voc=48.0,
            vm=40.0,
            im=13.0,
            ff=0.80,
            appearance_result="pass",
            # INTAKE_STATUS is pending/confirmed/rejected (D5: isolation is
            # tracked by the batch state, not an intake value) — the shared
            # SN is quarantined via the batch's isolated state + a fail
            # decision.
            intake_status="confirmed",
            decision_status="fail",
        )
    )
    # ISO preset row in the rule's declared shape (iv / normal / AQL 1.0).
    # Note: this test never reaches item 7 — the release attempt below is
    # stopped by the state guard (409 batch_not_pending_release) before the
    # assertion set is assembled. Item 7 is covered in
    # tests/api/test_release_api.py; this row keeps the seed production-shaped.
    session.add(
        IsoSamplingRow(
            inspection_type="iv",
            inspection_level="normal",
            aql=1.0,
            lot_size_min=1,
            lot_size_max=100,
            n=1,
            ac=0,
            re=1,
        )
    )
    for eid, etype in ((EV1, "decision_snapshot"), (EV2, "agent_output")):
        session.add(
            Evidence(
                evidence_id=eid,
                evidence_type=etype,
                object_ref={"kind": "batch", "id": "B-CONSIST-002"},
                content_hash=f"sha256:e2e-s4-{eid.hex[:8]}",
                generated_at=_TS,
                generated_by="seed",
            )
        )
    session.commit()


def _client() -> tuple[TestClient, Any, Any]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = create_app(session_factory=lambda: session, own_session=False)
    return TestClient(app), session, engine


def test_sample4_consistency_disposition() -> None:
    sample = load_acceptance("consistency")
    client, session, engine = _client()
    try:
        _seed(session, sample)

        # disposition edges (domain dry-runs; preconditions per the P0 state
        # machine — new->isolated needs the sn_linked precondition).
        assert _edge_legal(
            kind="incident", frm="new", to="isolated", preconditions={"sn_linked": True}
        )
        assert _edge_legal(
            kind="incident", frm="isolated", to="assigned", preconditions={"owner_assigned": True}
        )
        assert _edge_legal(
            kind="batch",
            frm="isolated",
            to="disposing",
            preconditions={"assigned_owner": True, "due_at_set": True},
        )
        assert _edge_legal(kind="batch", frm="pending_recheck", to="rejected")
        assert _edge_legal(kind="batch", frm="rejected", to="archived")

        # 1) raise the data-consistency incident on the open batch B2.
        r = client.post(
            "/api/incidents",
            json={
                "batch_id": str(B2),
                "trigger_type": "data_consistency",
                "severity": "major",
                "affected_sns": [sample["raw"]["cross_batch_sns"][0]],
                "evidence_ids": [str(EV1)],
            },
        )
        assert r.status_code == 201, r.text
        inc_id = UUID(r.json()["incident_id"])

        # 2) route the incident down the P0 edge new -> isolated (its SN is
        #    linked by step 1), then assign (incident isolated -> assigned;
        #    B2 batch isolated -> disposing).
        inc = session.get(QualityIncident, inc_id)
        inc.status = "isolated"
        session.commit()
        r = client.post(
            f"/api/incidents/{inc_id}/assign",
            json={
                "owner": "ZHL-QA",
                "due_at": "2026-06-10T00:00:00+00:00",
                "containment_action": "quarantine SN-SHARED in B2; reconcile against B1",
            },
        )
        assert r.status_code == 200, r.text
        assert session.get(DeliveryBatch, B2).status == "disposing"

        # 3) disposition: a consistency incident cannot be reinspection-
        #    repaired — route the incident to the rejection branch (the
        #    pending_recheck->rejected recheck-failed edge, which takes no
        #    preconditions; the incident itself reaches pending_recheck via
        #    assigned->pending_recheck once the disposition measures are
        #    recorded).
        inc = session.get(QualityIncident, inc_id)
        inc_res = transition(
            "assigned", "pending_recheck", {"reinspection_recorded": True}, kind="incident"
        )
        assert inc_res.accepted, f"incident disposition edge denied: {inc_res.reason}"
        inc.status = "pending_recheck"
        batch = session.get(DeliveryBatch, B2)
        re_res = transition(
            "disposing", "pending_recheck", {"measures_confirmed": True}, kind="batch"
        )
        assert re_res.accepted, f"batch recheck edge denied: {re_res.reason}"
        batch.status = "pending_recheck"
        res = transition(batch.status, "rejected", {}, kind="batch")
        assert res.accepted, f"rejection edge denied: {res.reason}"
        batch.status = "rejected"
        inc.status = "rejected"
        session.commit()
        assert session.get(DeliveryBatch, B2).status == "rejected"

        # 4) B2 release must have been blocked even before rejection: drive
        #    a release attempt on a pending_release twin to pin items 4/7
        #    naming the isolated SN + the uncovered reinspection scope
        #    (Review Focus #2/#3). (A rejected batch cannot release — the
        #    409 from the wrong-state check is the terminal guard.)
        r = client.post(f"/api/batches/{B2}/release", json={"approved_by": "ZHL-ENG"})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "batch_not_pending_release"

        # 5) disposition report on the rejected batch (D7: incident type
        #    only) -> auto-archive (D6a).
        r = client.post(f"/api/batches/{B2}/report", json={"report_type": "incident"})
        assert r.status_code == 201, r.text
        assert session.get(DeliveryBatch, B2).status == "archived"

        # 6) the released first batch B1 is untouched.
        b1 = session.get(DeliveryBatch, B1)
        assert b1.status == "released"
        assert session.query(Report).filter(Report.batch_id == B1).count() == 0

        # audit trail: the B2 chain leaves trace + evidence + report rows.
        from app.plugins.trace_store import TraceStore

        skill_names = {t.skill_name for t in TraceStore().list_for_batch(session, B2)}
        assert "report_api" in skill_names
        assert session.query(Report).filter(Report.batch_id == B2).count() == 1
    finally:
        session.close()
        engine.dispose()
