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

from app.api._archival import auto_archive_if_terminal
from app.api.guards import require_not_archived
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


def _reject_reason(session: Any, batch_id: uuid.UUID) -> str:
    """最近一次人工拒绝的 reason（记在 reject 轨迹行的 params_summary）。

    处置报告的拒因章节引用这份真实原因；查不到（非人工拒绝路径，如复检
    fail 转拒绝）时返回空串 —— 报告如实留空，不编造占位文本。
    """
    reason = ""
    for t in TraceStore().list_for_batch(session, batch_id):
        if t.skill_name == "reject":
            params = t.params_summary or {}
            reason = str(params.get("reason") or "")
    return reason


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

    # §4.5 闭环点：未放行批次只能生成处置报告。放行报告要求批次已处于
    # released —— 七项断言在放行时刻随证据行落库，报告才有真实结果可引用；
    # 判定中/隔离中的批次开"放行结论：通过"的报告是凭空编造。
    if payload.report_type == "release" and batch.status != "released":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "release_report_requires_released_batch",
                "status": batch.status,
                "hint": "未放行批次只能生成处置报告；放行报告在批次 released 后生成",
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
    refusal = _reject_reason(session, batch_id) if payload.report_type == "incident" else None
    report = skill.generate(
        session,
        batch_id,
        payload.report_type,
        generated_at=datetime.now(UTC),
        refusal_reason=refusal,
    )

    # D6a: auto-archive in the SAME transaction as the report write, via the
    # shared helper (single call site, also used by the reject route).
    archived = auto_archive_if_terminal(session, batch)

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
