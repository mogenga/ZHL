"""E2E: acceptance sample 3 — incident closure full chain (PB Task 10).

Mock-driven (no Phase A dependency, no live DB): the batch is seeded
``isolated`` with one out-of-band module, and the whole PB flow is driven
through the assembled app (Task 9 ``create_app``) with a SQLite session:

    create incident -> assign -> attribution (Mock LLM, PB O-2) ->
    reinspection (minimal scope, pass) -> CAPA (verified=true) ->
    pending_release -> release (all 7 items pass; item 7 is vacuous here —
    every incident is already closed, so there is no scope row to check) ->
    released -> report (release type, from terminal state) -> auto-archived.

Each step asserts the three-layer audit trail (trace / evidence / report
rows exist) and that the state-machine edges the chain uses are legal
(domain ``transition`` dry-runs, P0 contract).

P0 缺口 #1 修复后（2026-10-14）：复检通过后经 close 端点人工签署关闭异常
（五条件门禁前移到 close），批次转 pending_release——不再直接写库绕过 API。

Fixtures: tests/fixtures/acceptance_incident.json (domain data) +
mock_llm_attribution.json (PB L1a, the attribution mock — its routing key is
INC-001, the fixture's incident id; the E2E routes the mock on that key and
aliases the fixture's short evidence ids EV-ATT-001/002/003 onto real seeded
UUID evidence rows so the D2 evidence gate still pins Review Focus #1/#5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.domain.state_machine import transition
from app.domain.types import IsoRow
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    IsoSamplingRow,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Report,
    SampleConclusion,
)
from app.plugins.attribution import AttributionSkill
from app.plugins.llm_client import MockLLMClient
from app.plugins.sampling import run_sampling
from app.plugins.trace_store import TraceStore
from tests.fixtures.loader import load_acceptance

_FIXDIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)

# --- deterministic ids -------------------------------------------------------
B1 = UUID("ab000000-1111-2222-3333-444444444401")
RULE = UUID("ab000000-1111-2222-3333-444444444402")
EV1 = UUID("ab000000-1111-2222-3333-444444444403")
EV2 = UUID("ab000000-1111-2222-3333-444444444404")
EV3 = UUID("ab000000-1111-2222-3333-444444444405")
CONC1 = UUID("ab000000-1111-2222-3333-444444444406")
_INSP_OK = UUID("ab000000-0000-0000-0000-000000000001")
_INSP_OODB = UUID("ab000000-0000-0000-0000-000000000002")


def _mk_rule(session: Any, sample: dict) -> None:
    raw = sample["raw"]["rules"]  # the raw dict (rules is a RuleSnapshotIn dataclass)
    session.add(
        QualityRuleVersion(
            rule_version_id=RULE,
            project_id=raw["project_id"],
            model=raw["model"],
            name=raw.get("name", "rv-1"),
            nameplate_power=raw["nameplate_power"],
            power_tolerance=raw["power_tolerance"],
            parameter_standards=raw["parameter_standards"],
            current_bands=raw["current_bands"],
            sample_plan=raw["sample_plan"],
            effective_at=_TS,
            version_no=raw["version_no"],
        )
    )


def _seed_batch(session: Any, sample: dict, batch_no: str) -> None:
    # Deterministic inspection ids derived from the SN (E2E-only; the test
    # never persists to a shared DB).
    insp_ids = {
        "SN-I-OK": UUID("ab000000-0000-0000-0000-000000000001"),
        "SN-I-OODB": UUID("ab000000-0000-0000-0000-000000000002"),
    }
    _mk_rule(session, sample)
    # 规则版本必须先落库：批次/检验行引用它（测试已按 Postgres 强制外键）
    session.flush()
    session.add(
        DeliveryBatch(
            batch_id=B1,
            batch_no=batch_no,
            project_id=sample["batch"]["project"],
            model=sample["batch"]["model"],
            planned_quantity=sample["batch"]["lot_size"],
            actual_quantity=sample["batch"]["lot_size"],
            rule_version_id=RULE,
            status=sample["batch"]["status"],
        )
    )
    session.flush()  # 批次先落库，检验行才能引用
    for ins in sample["inspections"]:  # ModuleInspectionIn dataclasses
        session.add(
            ModuleInspection(
                inspection_id=insp_ids[ins.sn],
                batch_id=B1,
                sn=ins.sn,
                source_type="IV_CSV",
                captured_at=_TS,
                pmax=ins.pmax,
                temp=ins.temp,
                isc=ins.isc,
                voc=ins.voc,
                vm=ins.vm,
                im=ins.im,
                ff=ins.ff,
                appearance_result=ins.appearance_result,
                intake_status="confirmed",
                decision_status="pass",
            )
        )
    # ISO preset row in the shape the rule's sample_plan actually declares
    # (iv / normal / AQL 1.0 — see 001_init.sql), so the release API's ISO
    # resolution has something real to resolve against.
    #
    # Honest note on coverage: on this path the batch only reaches
    # pending_release *after* every incident is closed, so at release time
    # item 7 has no open incident and no scope row to check — it passes
    # without consulting the ISO table at all. Item 7's real coverage lives
    # in tests/api/test_release_api.py. This row is kept so the seed matches
    # production rather than the implementation's old hardcoded combo.
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
    # Evidence rows: decision snapshot (item 5) + attribution evidence
    # (EV-ATT-001/002/003 — the mock fixture's hypothesis evidence ids, so
    # the D2 gate keeps two of the three hypotheses).
    # EVIDENCE_TYPE enum (P0 frozen): source_file / extraction_result /
    # human_confirmation / decision_snapshot / task_trace / report_snapshot.
    # Attribution findings are model output -> extraction_result.
    for eid, etype in (
        (EV1, "decision_snapshot"),
        (EV2, "extraction_result"),
        (EV3, "extraction_result"),
    ):
        session.add(
            Evidence(
                evidence_id=eid,
                evidence_type=etype,
                object_ref={"kind": "batch", "id": batch_no},
                content_hash=f"sha256:e2e-{eid.hex[:8]}",
                generated_at=_TS,
                generated_by="seed",
            )
        )
    session.commit()


_INS_OK = UUID("ab000000-0000-0000-0000-000000000001")
_INS_OODB = UUID("ab000000-0000-0000-0000-000000000002")


def _client() -> tuple[TestClient, Any, Any]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = create_app(session_factory=lambda: session, own_session=False)
    return TestClient(app), session, engine


def _edge_legal(kind: str, frm: str, to: str, preconditions: dict | None = None) -> bool:
    """Domain dry-run: is the (kind, frm -> to) edge in the state machine?

    NB: ``transition`` takes preconditions positionally (frozen P0 API)."""
    return transition(frm, to, preconditions or {}, kind=kind).accepted


class _EvidenceStore:
    """EvidenceStore for AttributionSkill: resolves evidence ids in the test
    session. The attribution fixture's hypotheses reference the fixture's
    own short evidence ids (EV-ATT-001/002/003); the E2E aliases them onto
    real seeded UUID evidence rows so the D2 gate (Review Focus #5) keeps
    exactly the hypotheses whose ids all resolve. Unknown ids -> KeyError
    (the gate drops those hypotheses)."""

    _ALIAS = {"EV-ATT-001": EV2, "EV-ATT-002": EV3, "EV-ATT-003": EV1}

    def __init__(self, session: Any) -> None:
        self._session = session

    def get(self, evidence_id: str) -> Any:
        row = self._session.get(Evidence, self._ALIAS.get(evidence_id, evidence_id))
        if row is None:
            raise KeyError(evidence_id)
        return row


def test_sample3_incident_closure_full_chain() -> None:
    sample = load_acceptance("incident")
    client, session, engine = _client()
    try:
        _seed_batch(session, sample, batch_no=sample["batch"]["batch_id"])

        # state-machine edges this chain will use (domain dry-runs, P0
        # contract — the API re-checks them at runtime).
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
        assert _edge_legal(
            kind="batch",
            frm="pending_recheck",
            to="pending_release",
            preconditions={"recheck_passed": True, "measures_effective": True},
        )
        assert _edge_legal(
            kind="batch",
            frm="pending_release",
            to="released",
            preconditions={"assertions_passed": True, "human_approved": True},
        )
        assert _edge_legal(kind="batch", frm="released", to="archived")
        # API extension: assign_incident implements new->assigned (the P0
        # path is new->isolated->assigned; the endpoint adopts incidents that
        # operators treat as assigned-from-creation — Task 6 spec review).
        assert not _edge_legal(
            kind="incident", frm="new", to="assigned"
        ), "P0 table must NOT allow the direct new->assigned edge"

        # 1) create incident (batch is isolated -> 201; SN-I-OODB is linked).
        r = client.post(
            "/api/incidents",
            json={
                "batch_id": str(B1),
                "trigger_type": "rule_fail",  # TRIGGER_TYPE enum (data_consistency etc.)
                "severity": "major",
                "affected_sns": [sample["incidents"][0]["affected_sns"][0]],
                "evidence_ids": [str(EV1)],
            },
        )
        assert r.status_code == 201, r.text
        inc_id = UUID(r.json()["incident_id"])
        inc = session.get(QualityIncident, inc_id)
        assert inc.status == "new"

        # 2a) route the incident down the P0 edge new -> isolated via the
        #     real R2 API endpoint (the sn is linked by step 1's
        #     incident_sn_links, so the sn_linked precondition holds) — no
        #     more manual DB write (Review Focus: closure not bypassed).
        iso_r = client.post(f"/api/incidents/{inc_id}/isolate")
        assert iso_r.status_code == 200, iso_r.text
        assert iso_r.json()["status"] == "isolated"
        assert session.get(QualityIncident, inc_id).status == "isolated"

        # 2b) assign (incident isolated -> assigned; batch isolated -> disposing).
        r = client.post(
            f"/api/incidents/{inc_id}/assign",
            json={
                "owner": "ZHL-QA",
                "due_at": "2026-06-05T00:00:00+00:00",
                "containment_action": "isolate SN-I-OODB from lot",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "assigned"
        assert session.get(DeliveryBatch, B1).status == "disposing"

        # 3) attribution via the mock LLM (PB O-2). The prompt routes on the
        #    incident's UUID; the fixture answers with 3 hypotheses, one of
        #    which carries no evidence ids -> the D2 gate drops it (Review
        #    Focus #1), leaving two ranked hypotheses. The DB incident_id is
        #    a UUID column, so the prompt routing id is supplied via a
        #    duck-typed incident view (the skill only reads .incident_id /
        #    .evidence_ids) — no PK mutation, no UUID->str PK rewrite.
        mock_llm = MockLLMClient(_FIXDIR / "mock_llm_attribution.json")

        class _PromptIncident:
            incident_id = "INC-001"
            evidence_ids = [str(EV1), str(EV2), str(EV3)]
            severity = "major"
            trigger_type = "rule_fail"
            # the trace row is filed under the batch the incident belongs to.
            batch_id = B1

        skill = AttributionSkill(llm=mock_llm, evidence_store=_EvidenceStore(session))
        hypotheses = skill.run(incident=_PromptIncident(), trace=TraceStore(), session=session)
        inc.root_cause_hypotheses = hypotheses
        session.commit()
        assert len(hypotheses) == 2, f"D2 gate should keep 2 of 3: {hypotheses}"
        assert [h["rank"] for h in hypotheses] == [1, 2]
        assert hypotheses[0]["confidence"] > hypotheses[1]["confidence"]

        # 4) reinspection (minimal scope, pass) — writes the Reinspection row
        #    and the incident's verification_id.
        r = client.post(
            f"/api/incidents/{inc_id}/reinspections",
            json={
                "inspection_id": str(_INS_OODB),
                "pmax": 550.0,
                "temp": 25.0,
                "isc": 10.5,
                "voc": 48.0,
                "vm": 40.0,
                "im": 13.0,
                "ff": 0.80,
                "result": "pass",
                "executor": "ZHL-QA",
                "executed_at": "2026-06-02T00:00:00+00:00",
                "scope_type": "minimal",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["passed"] is True

        # 5) CAPA (verified=true; L3 requires the pass reinspection).
        r = client.post(
            f"/api/incidents/{inc_id}/capa",
            json={
                "confirmed_root_cause": "cell mismatch -> Impp drift",
                "corrective_action": "replace lead cell",
                "verified": True,
            },
        )
        assert r.status_code == 200, r.text
        assert session.get(DeliveryBatch, B1).status == "pending_recheck"

        # 6) batch -> pending_release via the close endpoint (P0 缺口 #1 已修：
        #    不再直接写库绕过 API；recheck-passed edge 的 dry-run 已断言)。
        batch = session.get(DeliveryBatch, B1)
        assert _edge_legal(
            kind="batch",
            frm=batch.status,
            to="pending_release",
            preconditions={"recheck_passed": True, "measures_effective": True},
        )
        r = client.post(f"/api/incidents/{inc_id}/close")
        assert r.status_code == 200, r.text
        assert r.json()["incident_status"] == "closed"
        assert r.json()["batch_status"] == "pending_release"
        assert session.get(QualityIncident, inc_id).status == "closed"

        # 7) the batch needs a passing sample conclusion for release item 2 —
        #    produced by the REAL sampling path, not hand-inserted. (The
        #    previous version of this test wrote the SampleConclusion row
        #    directly with the session, which is exactly how the "结论侧没有
        #    任何人回写" defect stayed invisible: the suite forged the very
        #    row production could not produce.)
        sample_result = run_sampling(
            session,
            [
                IsoRow(
                    inspection_type="iv",
                    inspection_level="normal",
                    aql=1.0,
                    lot_size_min=1,
                    lot_size_max=100,
                    n=2,
                    ac=0,
                    re=1,
                )
            ],
            B1,
            inspection_type="iv",
            aql=1.0,
            lot_size=sample["batch"]["lot_size"],
            sns=[insp.sn for insp in sample["inspections"]],
            seed=str(B1),
        )
        session.commit()
        assert sample_result["result"] == "pass"
        assert session.scalars(
            select(SampleConclusion).where(SampleConclusion.batch_id == B1)
        ).all()

        # 8) release passes all 7 items（闭环修复后 CAPA 门禁前移到 close 的
        #    五条件校验；closed 异常不再参与断言 ⑥。item 7：复检覆盖 = 异常
        #    sn links 全集；iso_lookup 经预置行解析）。
        r = client.post(f"/api/batches/{B1}/release", json={"approved_by": "ZHL-ENG"})
        assert r.status_code == 200, r.text
        assert r.json()["passed"] is True, r.json()
        assert all(c["passed"] for c in r.json()["checks"])
        assert session.get(DeliveryBatch, B1).status == "released"

        # 10) report (release type, from terminal state) -> auto-archived.
        r = client.post(f"/api/batches/{B1}/report", json={"report_type": "release"})
        assert r.status_code == 201, r.text
        assert session.get(DeliveryBatch, B1).status == "archived"

        # three-layer audit trail (D1): trace / evidence / report rows.
        traces = TraceStore().list_for_batch(session, B1)
        skill_names = {t.skill_name for t in traces}
        assert "attribution" in skill_names
        assert "release" in skill_names
        assert "report_api" in skill_names
        assert (
            session.query(Evidence).filter(Evidence.evidence_type == "human_confirmation").count()
            >= 1
        )
        assert session.query(Report).filter(Report.batch_id == B1).count() >= 1
    finally:
        session.close()
        engine.dispose()
