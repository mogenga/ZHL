"""Reinspection scope API (PB Task 7, D5/D5a — Review Focus #2).

Contract (plan Task 7):
- ``POST /api/incidents/{id}/reinspections`` — the request body carries
  ``scope_type`` / ``scope_sns`` (default = ``minimal`` + the incident's
  full ``incident_sn_links`` SN set, computed server-side); ``expanded`` /
  ``full_lot`` require ``scope_sns`` that cover the minimal set else 400
  listing the gap; scope expansion writes ``params_summary.scope_expansion
  = true`` to the trace; an independent ``Reinspection`` row (the original
  ``module_inspections`` row is NOT mutated); the incident's
  ``verification_id`` is written back; ``result=fail`` transitions the
  incident ``pending_recheck -> rejected`` and the batch
  ``pending_recheck -> rejected`` in a single transaction + trace. The
  response body is ``{reinspection_id, incident_id, sn, passed,
  scope_applied}`` where ``sn`` is resolved from ``module_inspections.sn``
  via the ``inspection_id`` link (not a direct column on ``reinspections``).
- ``GET /api/incidents/{id}/reinspections``
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
    IncidentSnLink,
    ModuleInspection,
    QualityIncident,
    Reinspection,
)
from app.plugins.trace_store import TraceStore

router = APIRouter(prefix="/api/incidents", tags=["reinspections"])


# --- request / response models -------------------------------------------


class ReinspectionCreate(BaseModel):
    inspection_id: uuid.UUID
    pmax: float
    temp: float
    isc: float
    voc: float
    vm: float
    im: float
    ff: float
    result: str
    executor: str
    executed_at: str
    scope_type: str = "minimal"
    scope_sns: list[str] | None = None


class ReinspectionOut(BaseModel):
    reinspection_id: uuid.UUID
    incident_id: uuid.UUID
    sn: str
    passed: bool
    scope_applied: str
    scope_sns: list[str]
    result: str


# --- dependency (session) — injected by the app factory in Task 9 ----------


def get_session() -> Any:
    raise RuntimeError("get_session not wired — use the app factory (Task 9)")


SessionDep = Annotated[Any, Depends(get_session)]

#: 允许登记复检记录的异常状态。``assigned`` = 已指派、措施未确认（正常闭环
#: 里复检先于 CAPA）；``pending_recheck`` = CAPA 已确认、等待复检结论。
#: ``new``/``isolated`` 还没有处置责任人，``closed``/``rejected``/``scrapped``
#: 是终态——对终态追加复检记录会覆盖 ``verification_id``，把已签署的闭环
#: 结论事后改写。
REINSPECTABLE_STATUSES = ("assigned", "pending_recheck")


def _require_reinspectable(incident: QualityIncident) -> None:
    if incident.status not in REINSPECTABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "incident_not_reinspectable",
                "status": incident.status,
                "expected": list(REINSPECTABLE_STATUSES),
            },
        )


# --- helpers --------------------------------------------------------------


def _incident_sn_set(session: Any, incident_id: uuid.UUID) -> list[str]:
    """The incident's full affected-SN set (the minimal scope).

    Resolved via ``incident_sn_links`` -> ``module_inspections.sn``.
    """
    links = (
        session.query(IncidentSnLink.inspection_id)
        .filter(IncidentSnLink.incident_id == incident_id)
        .all()
    )
    inspection_ids = [row[0] for row in links]
    if not inspection_ids:
        return []
    rows = (
        session.query(ModuleInspection.sn)
        .filter(ModuleInspection.inspection_id.in_(inspection_ids))
        .all()
    )
    return [r[0] for r in rows]


def _resolve_sn(session: Any, inspection_id: uuid.UUID) -> str:
    row = session.get(ModuleInspection, inspection_id)
    if row is None:
        raise HTTPException(status_code=400, detail={"error": "inspection_not_found"})
    return row.sn


def _validate_scope(
    session: Any,
    incident_id: uuid.UUID,
    scope_type: str,
    scope_sns: list[str] | None,
) -> tuple[list[str], bool]:
    """Return (applied_scope_sns, is_expansion).

    - ``minimal`` (default): the server fills ``scope_sns`` from the
      incident's full SN set; the caller's value (if any) is ignored.
    - ``expanded`` / ``full_lot``: the caller MUST supply ``scope_sns`` and
      it must cover the incident's minimal SN set; otherwise 400 listing the
      gap (Review Focus #2).
    """
    minimal = _incident_sn_set(session, incident_id)
    if scope_type == "minimal":
        return list(minimal), False
    if scope_type in ("expanded", "full_lot"):
        if not scope_sns:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "scope_sns_required",
                    "hint": (
                        f"scope_type={scope_type} requires an explicit scope_sns "
                        "covering the minimal set"
                    ),
                },
            )
        missing = [sn for sn in minimal if sn not in scope_sns]
        if missing:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "scope_gap",
                    "missing": missing,
                    "hint": "scope_sns must cover the incident's full affected-SN set (minimal)",
                },
            )
        return list(scope_sns), True
    raise HTTPException(
        status_code=400, detail={"error": "unknown_scope_type", "scope_type": scope_type}
    )


def _record_trace(
    session: Any,
    batch_id: uuid.UUID,
    incident_id: uuid.UUID,
    structured: dict,
    params: dict,
) -> None:
    TraceStore().record(
        session,
        task_id=str(uuid.uuid4()),
        batch_id=batch_id,
        skill_name="reinspection",
        input_ref={"kind": "incident", "id": str(incident_id)},
        plugins_invoked=["state_machine"],
        params_summary=params,
        model_version=None,
        structured_output=structured,
        validation_result="pass",
        failure_reason=None,
        human_action=None,
        started_at=datetime.now(UTC),
        finished_at=None,
    )


# --- endpoints ------------------------------------------------------------


@router.post("/{incident_id}/reinspections", status_code=201, response_model=ReinspectionOut)
def create_reinspection(
    incident_id: uuid.UUID, payload: ReinspectionCreate, session: SessionDep
) -> ReinspectionOut:
    # M-7：行锁读取——复检 fail 转拒绝与 close 端点互斥，杜绝 last-write-wins
    incident = (
        session.query(QualityIncident)
        .filter(QualityIncident.incident_id == incident_id)
        .with_for_update()
        .first()
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")

    # B2: 只有处置中的异常能登记复检（终态一律只读）。
    _require_reinspectable(incident)

    # D5: scope validation (Review Focus #2)
    applied_sns, is_expansion = _validate_scope(
        session, incident_id, payload.scope_type, payload.scope_sns
    )

    # The reinspection references a module_inspection row (the SN being re-tested).
    sn = _resolve_sn(session, payload.inspection_id)
    batch = session.get(DeliveryBatch, incident.batch_id)
    # D8: archived terminal guard (shared).
    require_not_archived(incident.batch_id, session)
    rule_version_id = batch.rule_version_id

    re_ = Reinspection(
        reinspection_id=uuid.uuid4(),
        incident_id=incident_id,
        inspection_id=payload.inspection_id,
        rule_version_id=rule_version_id,
        pmax=payload.pmax,
        temp=payload.temp,
        isc=payload.isc,
        voc=payload.voc,
        vm=payload.vm,
        im=payload.im,
        ff=payload.ff,
        result=payload.result,
        executor=payload.executor,
        executed_at=datetime.fromisoformat(payload.executed_at),
        scope_type=payload.scope_type,
        scope_sns=applied_sns,
    )
    session.add(re_)
    # 循环外键（reinspections.incident_id → quality_incidents.verification_id
    # → reinspections，见 001_init.sql §5.1.6 注释）：ORM 的工作单元无法给这个
    # 环排序，于是默认先发 quality_incidents 的 UPDATE、后发 INSERT——Postgres
    # 报 fk_incident_verification 违约（SQLite 默认不强制外键，测试因此漏掉）。
    # 先显式 flush 把复检行落库，再回填 verification_id，UPDATE 就必然在
    # INSERT 之后。
    session.flush()

    # Write the incident's verification_id back (independent row, module_inspections untouched).
    incident.verification_id = re_.reinspection_id

    passed = payload.result == "pass"

    if not passed:
        # result=fail -> incident + batch both transition to rejected (single tx).
        inc_res = transition(incident.status, "rejected", {}, kind="incident")
        if not inc_res.accepted:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail={"error": "incident_transition_denied", "reason": inc_res.reason},
            )
        incident.status = "rejected"
        batch = session.get(DeliveryBatch, incident.batch_id)
        batch_res = transition(batch.status, "rejected", {}, kind="batch")
        if not batch_res.accepted:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail={"error": "batch_transition_denied", "reason": batch_res.reason},
            )
        batch.status = "rejected"

    _record_trace(
        session,
        incident.batch_id,
        incident_id,
        {"reinspection_id": str(re_.reinspection_id), "result": payload.result},
        {"scope_expansion": is_expansion, "scope_type": payload.scope_type},
    )
    session.flush()
    session.commit()

    return ReinspectionOut(
        reinspection_id=re_.reinspection_id,
        incident_id=incident_id,
        sn=sn,
        passed=passed,
        scope_applied=payload.scope_type,
        scope_sns=applied_sns,
        result=payload.result,
    )


@router.get("/{incident_id}/reinspections", response_model=list[ReinspectionOut])
def list_reinspections(incident_id: uuid.UUID, session: SessionDep) -> list[ReinspectionOut]:
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    rows = session.query(Reinspection).filter(Reinspection.incident_id == incident_id).all()
    out: list[ReinspectionOut] = []
    for re_ in rows:
        ins = session.get(ModuleInspection, re_.inspection_id)
        out.append(
            ReinspectionOut(
                reinspection_id=re_.reinspection_id,
                incident_id=incident_id,
                sn=ins.sn if ins else "",
                passed=re_.result == "pass",
                scope_applied=re_.scope_type,
                scope_sns=list(re_.scope_sns or []),
                result=re_.result,
            )
        )
    return out
