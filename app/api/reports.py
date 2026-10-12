"""Report API (PB Task 7 — POST /api/batches/{id}/report + GET reports).

Contract (plan Task 7):
- ``POST /api/batches/{id}/report`` — ``{report_type}``; a
  ``rejected`` batch only allows the ``incident`` report type (409
  otherwise, D7); an ``archived`` batch is 409 (D8); after writing the
  report, the *same transaction* checks auto-archive (D6a): batch status
  in {released, rejected} and the target report type has version >= 1 ->
  ``transition(batch, "archived")`` (state machine edge, Phase 0) + trace.
  A first write returns 201; an idempotent re-call on an unchanged batch
  returns 200 with the existing version (D10).
- ``GET /api/batches/{id}/reports`` / ``GET /api/batches/{id}/reports/{report_id}``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.guards import require_not_archived
from app.domain.state_machine import transition
from app.infra.models import (
    DeliveryBatch,
    Report,
)
from app.plugins.trace_store import TraceStore
from app.skills.report_skill import ReportSkill

router = APIRouter(prefix="/api/batches", tags=["reports"])


class ReportCreate(BaseModel):
    report_type: str


class ReportOut(BaseModel):
    report_id: uuid.UUID
    batch_id: uuid.UUID
    report_type: str
    version: int
    content_md: str
    content_json: dict
    source_snapshot_id: uuid.UUID
    agent_task_id: uuid.UUID | None
    generated_at: str


def get_session() -> Any:
    raise RuntimeError("get_session not wired — use the app factory (Task 9)")


SessionDep = Annotated[Any, Depends(get_session)]


def _to_out(r: Report) -> ReportOut:
    return ReportOut(
        report_id=r.report_id,
        batch_id=r.batch_id,
        report_type=r.report_type,
        version=r.version,
        content_md=r.content_md,
        content_json=r.content_json,
        source_snapshot_id=r.source_snapshot_id,
        agent_task_id=r.agent_task_id,
        generated_at=r.generated_at.isoformat() if r.generated_at else "",
    )


def _record_trace(
    session: Any,
    batch_id: uuid.UUID,
    report_type: str,
    version: int,
    archived: bool,
) -> None:
    TraceStore().record(
        session,
        task_id=str(uuid.uuid4()),
        batch_id=batch_id,
        skill_name="report_api",
        input_ref={"kind": "batch", "report_type": report_type},
        plugins_invoked=["report_skill"],
        params_summary={"report_type": report_type, "version": version, "auto_archived": archived},
        model_version=None,
        structured_output={"report_type": report_type, "version": version},
        validation_result="pass",
        failure_reason=None,
        human_action=None,
        started_at=datetime.now(UTC),
        finished_at=None,
    )


@router.post("/{batch_id}/report", status_code=201, response_model=ReportOut)
def create_report(batch_id: uuid.UUID, payload: ReportCreate, session: SessionDep) -> ReportOut:
    batch = session.get(DeliveryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")

    # D8: archived batches cannot receive new reports — the shared guard
    # (guards.require_not_archived) fires first with the stable ARCHIVED_ERROR 409.
    require_not_archived(batch_id, session)

    # D7: rejected batches only allow the incident (disposal) report type.
    if batch.status == "rejected" and payload.report_type != "incident":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "rejected_batch_only_incident_report",
                "report_type": payload.report_type,
            },
        )

    # D10: idempotent re-render — if a version already exists for this
    # batch+type, return the latest one without bumping the version.
    existing = (
        session.query(Report)
        .filter(Report.batch_id == batch_id, Report.report_type == payload.report_type)
        .order_by(Report.version.desc())
        .first()
    )
    if existing is not None:
        # Same-transaction idempotent hit: keep the caller's status 200.
        from starlette.responses import JSONResponse

        return JSONResponse(status_code=200, content=_to_out(existing).model_dump(mode="json"))

    skill = ReportSkill()
    report = skill.generate(
        session,
        batch_id,
        payload.report_type,
        generated_at=datetime.now(UTC),
    )

    # D6a: auto-archive in the SAME transaction as the report write.
    #
    # D6a invariant: the ("released","archived") / ("rejected","archived")
    # state-machine edges MUST keep an EMPTY precondition. If a precondition
    # is ever added to these edges in app/domain/state_machine.py, the
    # transition(..., "archived", {}, ...) call below will return
    # accepted=False, the batch will silently stay non-archived, and the
    # report will still commit with no error raised. Keep this comment in
    # sync with any future Phase 0 state_machine.py change.
    archived = False
    if batch.status in ("released", "rejected"):
        res = transition(batch.status, "archived", {}, kind="batch")
        if res.accepted:
            batch.status = "archived"
            archived = True

    _record_trace(session, batch_id, payload.report_type, report.version, archived)
    session.commit()
    session.refresh(report)
    return _to_out(report)


@router.get("/{batch_id}/reports", response_model=list[ReportOut])
def list_reports(batch_id: uuid.UUID, session: SessionDep) -> list[ReportOut]:
    batch = session.get(DeliveryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")
    rows = (
        session.query(Report)
        .filter(Report.batch_id == batch_id)
        .order_by(Report.report_type, Report.version)
        .all()
    )
    return [_to_out(r) for r in rows]


@router.get("/{batch_id}/reports/{report_id}", response_model=ReportOut)
def get_report(batch_id: uuid.UUID, report_id: uuid.UUID, session: SessionDep) -> ReportOut:
    report = session.get(Report, report_id)
    if report is None or report.batch_id != batch_id:
        raise HTTPException(status_code=404, detail="report_not_found")
    return _to_out(report)
