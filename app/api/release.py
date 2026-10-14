"""Release endpoint (PB Task 8, D4 fail-keeps-state; D7-7 item 7).

Contract (plan Task 8):

- ``POST /api/batches/{id}/release`` — body ``{approved_by}``. The seven
  ``assert_release`` inputs are assembled from the DB by
  ``app.skills.release_inputs`` — the same assembly the report layer
  quotes, so a release report can never show a second, diverging set of
  assertion results:

  1. batch status (``pending_release``)
  2. the batch's latest ``SampleConclusion`` result
  3. the batch's open incidents
  4. isolated SNs (module_inspections rows with intake isolated)
  5. the batch's required evidence set
  6. CAPA verification state (per open incident, the ``capa_verified`` flag)
  7. **reinspection scope rows** (one ``ReinspectionScopeIn`` per open
     incident: the incident's full affected-SN set from
     ``incident_sn_links``, plus the four ISO values resolved from
     ``module_inspections`` + the latest sample) and the preset ISO table.

  All-pass: write a release (human-confirmation) evidence row — carrying
  the full ``AssertionResult`` so the release report can cite the recorded,
  signed result — transition the batch ``pending_release -> released``
  (preconditions assertions_passed + human_approved), and record a trace —
  all in one transaction. The auto-archive is NOT done here (D6a: archiving
  happens at the report call, which is what makes the batch terminal). Any
  assertion fail: **no state transition, no ``release_decision`` write, no
  DB mutation beyond the read-only assertion** — respond 200 with the full
  ``AssertionResult`` (per-item + reasons naming the offending incident /
  SN / evidence id) and a "assertion failed, retriable" trace marker.

- batch not ``pending_release`` -> 409; ``archived`` -> 409 (D8).

Purity: ``app.skills.release_inputs`` is the only place that resolves the
ISO values and the incident required-SN sets from the DB and feeds them to
the domain function, keeping ``app/domain`` read-free (P0 contract).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api._archival import auto_archive_if_terminal
from app.api.guards import require_not_archived
from app.domain.state_machine import transition
from app.infra.models import DeliveryBatch, Evidence
from app.plugins.trace_store import TraceStore
from app.skills.release_inputs import assert_batch_release
from app.skills.remediation_conclusion import materialize_remediation_conclusion
from app.skills.report_skill import ReportSkill

router = APIRouter(prefix="/api/batches", tags=["release"])


class ReleaseRequest(BaseModel):
    approved_by: str


class ReleaseResponse(BaseModel):
    passed: bool
    batch_id: uuid.UUID
    status: str
    release_decision: str
    checks: list[dict]


class RejectRequest(BaseModel):
    reason: str
    rejected_by: str


class RejectResponse(BaseModel):
    batch_id: uuid.UUID
    status: str
    auto_archived: bool
    reason: str
    report_id: uuid.UUID | None


class RecheckResponse(BaseModel):
    batch_id: uuid.UUID
    sample_round: int
    result: str
    reason: str = ""
    written: bool
    skipped: str = ""


def get_session() -> Any:
    raise RuntimeError("get_session not wired — use the app factory (Task 9)")


SessionDep = Annotated[Any, Depends(get_session)]


# --- endpoint ---------------------------------------------------------------


@router.post("/{batch_id}/release", response_model=ReleaseResponse)
def release_batch(
    batch_id: uuid.UUID, payload: ReleaseRequest, session: SessionDep
) -> ReleaseResponse:
    batch = session.get(DeliveryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")

    # D8: archived is terminal — the shared guard (guards.require_not_archived)
    # loads the batch and 409s with the stable ARCHIVED_ERROR payload.
    require_not_archived(batch_id, session)
    # Must be in pending_release to release.
    if batch.status != "pending_release":
        raise HTTPException(
            status_code=409,
            detail={"error": "batch_not_pending_release", "status": batch.status},
        )

    # 七项断言的 DB→view 装配收敛在 app/skills/release_inputs.py：放行端点与
    # 放行报告共用同一份装配、同一次口径（报告引用真实断言，不再渲染占位）。
    result = assert_batch_release(session, batch)

    if result.passed:
        # All-pass: write release evidence + transition + trace in one tx.
        # C8: content_hash is a real sha256 over the canonical JSON of the
        # evidence content (sort_keys=True for stability) — not a random uuid.
        _release_content = {"approved_by": payload.approved_by, "assertions": result.to_dict()}
        _release_hash = hashlib.sha256(
            json.dumps(_release_content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        release_ev = Evidence(
            evidence_id=uuid.uuid4(),
            evidence_type="human_confirmation",
            object_ref={"kind": "batch", "id": batch.batch_no, "approved_by": payload.approved_by},
            content_hash=f"sha256:{_release_hash}",
            content=_release_content,
            generated_at=datetime.now(UTC),
            generated_by=payload.approved_by,
        )
        session.add(release_ev)
        session.flush()  # 先把证据行落库，release_evidence_id 外键才指向存在的证据
        batch.release_evidence_id = release_ev.evidence_id
        batch.status = "released"
        batch.release_decision = "released"
        TraceStore().record(
            session,
            task_id=str(uuid.uuid4()),
            batch_id=batch_id,
            skill_name="release",
            input_ref={"kind": "batch", "id": batch.batch_no},
            plugins_invoked=["assert_release"],
            params_summary={"approved_by": payload.approved_by, "assertions_passed": True},
            model_version=None,
            structured_output={"status": "released"},
            validation_result="pass",
            failure_reason=None,
            human_action=None,
            started_at=datetime.now(UTC),
            finished_at=None,
        )
        session.commit()
        return ReleaseResponse(
            passed=True,
            batch_id=batch_id,
            status="released",
            release_decision="released",
            checks=[
                {"name": c.name, "passed": c.passed, "reason": c.reason} for c in result.checks
            ],
        )

    # Any fail: no state transition, no release_decision write. Record the
    # retriable assertion-failure trace, commit only that read-only marker.
    TraceStore().record(
        session,
        task_id=str(uuid.uuid4()),
        batch_id=batch_id,
        skill_name="release",
        input_ref={"kind": "batch", "id": batch.batch_no},
        plugins_invoked=["assert_release"],
        params_summary={"approved_by": payload.approved_by, "assertions_passed": False},
        model_version=None,
        structured_output={
            "status": "pending_release",
            "failed": [c.name for c in result.checks if not c.passed],
        },
        validation_result="fail",
        failure_reason="assertion failed, retriable",
        human_action=None,
        started_at=datetime.now(UTC),
        finished_at=None,
    )
    session.commit()
    return ReleaseResponse(
        passed=False,
        batch_id=batch_id,
        status=batch.status,
        release_decision=batch.release_decision,
        checks=[{"name": c.name, "passed": c.passed, "reason": c.reason} for c in result.checks],
    )


@router.post("/{batch_id}/recheck", response_model=RecheckResponse)
def recheck_batch(batch_id: uuid.UUID, session: SessionDep) -> RecheckResponse:
    """复检闭环结论重算：把"复检已通过并关闭"落成第 2 轮抽样结论。

    架构文档 §9.2 早已列出 ``POST /api/batches/{id}/recheck``（复检），但此前
    只实现了异常级的 ``POST /api/incidents/{id}/reinspections`` —— 复检记录
    写得进去，结论侧却没人回写，抽样 fail 的批次于是永久卡在放行断言②。

    关闭端点（``incidents.close``）在批次转入 ``pending_release`` 的同一事务里
    已自动补写；本端点面向**在此之前就已关闭完**的历史批次（补写一次即可），
    以及需要按需重算的场景。

    幂等：已有第 2 轮结论时 ``written=false`` 并回报既有结果。闭环不成立时
    只回报推导结果（``result="fail"`` + 指名未解除的 SN），**不写任何结论行**
    —— 断言②继续阻断，不存在"重算一下就放行"的旁路。
    """
    batch = session.get(DeliveryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")

    # D8: archived is terminal.
    require_not_archived(batch_id, session)
    if batch.status != "pending_release":
        raise HTTPException(
            status_code=409,
            detail={"error": "batch_not_pending_release", "status": batch.status},
        )

    outcome = materialize_remediation_conclusion(session, batch)
    TraceStore().record(
        session,
        task_id=str(uuid.uuid4()),
        batch_id=batch_id,
        skill_name="recheck",
        input_ref={"kind": "batch", "id": batch.batch_no},
        plugins_invoked=["remediation_conclusion"],
        params_summary={
            "sample_round": outcome.sample_round,
            "written": outcome.written,
            "skipped": outcome.skipped,
        },
        model_version=None,
        structured_output={"result": outcome.result, "reason": outcome.reason},
        validation_result="pass" if outcome.result == "pass" else "fail",
        failure_reason=None if outcome.result == "pass" else outcome.reason,
        human_action=None,
        started_at=datetime.now(UTC),
        finished_at=None,
    )
    session.commit()
    return RecheckResponse(
        batch_id=batch_id,
        sample_round=outcome.sample_round,
        result=outcome.result,
        reason=outcome.reason,
        written=outcome.written,
        skipped=outcome.skipped,
    )


@router.post("/{batch_id}/reject", response_model=RejectResponse)
def reject_batch(
    batch_id: uuid.UUID, payload: RejectRequest, session: SessionDep
) -> RejectResponse:
    """Manual reject (D7 + D6a): record the rejection, generate the incident
    disposal report and auto-archive the batch — all in one transaction.

    D8: an already-archived batch is 409. Any other non-archived batch can be
    rejected; the state-machine ``rejected`` edge then (D6a, empty
    precondition) auto-transitions it to ``archived`` in the same tx.
    """
    batch = session.get(DeliveryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")

    # D8: archived is terminal.
    require_not_archived(batch_id, session)

    # D7: a rejected batch only allows the incident (disposal) report type —
    # the reject action itself is that incident report, so D7 is satisfied by
    # construction.
    res = transition(batch.status, "rejected", {"human_rejected": True}, kind="batch")
    if not res.accepted:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "batch_reject_not_allowed",
                "from_status": batch.status,
                "reason": res.reason,
            },
        )
    batch.status = "rejected"

    # D7: generate the incident disposal report in the SAME transaction.
    # The operator's rejection reason is the report's 拒因 section — the real
    # reason, not a fabricated "incident ... status=..." placeholder.
    skill = ReportSkill()
    report = skill.generate(
        session,
        batch_id,
        "incident",
        generated_at=datetime.now(UTC),
        refusal_reason=payload.reason,
    )

    # D6a: auto-archive the now-rejected batch in the SAME transaction.
    auto_archived = auto_archive_if_terminal(session, batch)

    # D1: the three-layer trace (trace row + the report's evidence + report).
    TraceStore().record(
        session,
        task_id=str(uuid.uuid4()),
        batch_id=batch_id,
        skill_name="reject",
        input_ref={"kind": "batch", "id": batch.batch_no},
        plugins_invoked=["report_skill", "auto_archive"],
        params_summary={
            "reason": payload.reason,
            "rejected_by": payload.rejected_by,
            "auto_archived": auto_archived,
        },
        model_version=None,
        structured_output={"status": batch.status, "report_id": str(report.report_id)},
        validation_result="pass",
        failure_reason=None,
        human_action="rejected",
        started_at=datetime.now(UTC),
        finished_at=None,
    )
    session.commit()
    session.refresh(report)
    return RejectResponse(
        batch_id=batch_id,
        status=batch.status,
        auto_archived=auto_archived,
        reason=payload.reason,
        report_id=report.report_id,
    )
