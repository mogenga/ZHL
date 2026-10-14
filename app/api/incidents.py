"""Incident API router (PB Task 6 — create / assign / capa endpoints).

Contract (plan Task 6):
- ``POST /api/incidents`` — create: ``affected_sns`` non-empty + write
  ``incident_sn_links`` + evidence existence check (400) + batch must be
  ``isolated`` else 409; incident status starts at ``new``.
- ``POST /api/incidents/{id}/assign`` — ``{owner, due_at, containment_action}``
  -> incident ``new/isolated -> assigned`` (precondition ``owner_assigned``) +
  batch ``isolated -> disposing`` (preconditions ``assigned_owner``/``due_at_set``)
  in a **single transaction** + trace row.
- ``POST /api/incidents/{id}/capa`` — ``{confirmed_root_cause,
  corrective_action, verified}`` (L3: ``verified`` may only be ``true`` when a
  reinspection record with ``result=pass`` exists, else 400; writes the
  ``capa_verified`` column) -> incident ``assigned -> pending_recheck`` +
  batch ``disposing -> pending_recheck`` in one transaction + trace.
- ``GET /api/incidents`` / ``GET /api/incidents/{id}`` — the detail view
  includes ``root_cause_hypotheses``.

Session is provided by the application factory (Task 9 wires the real
Postgres engine; tests here bind a SQLite session to
``app.dependency_overrides`` so the router is testable without a live DB).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.guards import require_not_archived
from app.domain.state_machine import OPEN_INCIDENT_STATUSES, close_incident, transition
from app.domain.types import IncidentView, ReinspectionView
from app.infra.models import (
    DeliveryBatch,
    Evidence,
    IncidentSnLink,
    ModuleInspection,
    QualityIncident,
    Reinspection,
)
from app.plugins.trace_store import TraceStore

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


# --- request / response models -------------------------------------------


class IncidentCreate(BaseModel):
    batch_id: uuid.UUID
    trigger_type: str
    severity: str
    affected_sns: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class IncidentAssign(BaseModel):
    owner: str
    due_at: str
    containment_action: str | None = None


class IncidentCapa(BaseModel):
    confirmed_root_cause: str
    corrective_action: str
    verified: bool = False


class IncidentOut(BaseModel):
    incident_id: uuid.UUID
    batch_id: uuid.UUID
    trigger_type: str
    severity: str
    status: str
    owner: str | None
    due_at: str | None
    containment_action: str | None
    confirmed_root_cause: str | None
    corrective_action: str | None
    capa_verified: bool
    root_cause_hypotheses: dict | list | None
    verification_id: uuid.UUID | None


class AttributeRequest(BaseModel):
    """T-1 attribution 3-way request (D9 single-request sync; P1 cross-request)."""

    decision: str  # "adopt" | "reject" | "defer"
    cause: str | None = None


class IncidentCloseOut(BaseModel):
    incident_id: uuid.UUID
    incident_status: str
    batch_id: uuid.UUID
    batch_status: str


# --- dependency (session) — injected by the app factory in Task 9 ----------


def get_session() -> Any:
    """Return the request-scoped SQLAlchemy session.

    Task 9's FastAPI app factory overrides this with the real Postgres
    session; tests bind a SQLite session via ``app.dependency_overrides``.
    """
    raise RuntimeError("get_session not wired — use the app factory (Task 9)")


SessionDep = Annotated[Any, Depends(get_session)]


def get_llm() -> Any:
    """Attribution LLM client dependency (T-1).

    Mirrors ``app.main._default_llm_client``: env-configured OpenAI-compatible
    client when ``ZHL_LLM_API_KEY`` is set, otherwise the deterministic
    fixture-backed mock pointed at ``tests/fixtures``.
    """
    import os
    from pathlib import Path

    from app.plugins.llm_client import MockLLMClient, OpenAICompatClient

    fixtures = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    if os.environ.get("ZHL_LLM_API_KEY", "").strip():
        return OpenAICompatClient()
    return MockLLMClient(fixtures)


LLMDep = Annotated[Any, Depends(get_llm)]


def get_evidence_store(session: SessionDep) -> Any:
    """Request-scoped evidence store dependency (T-1).

    Bound to the session that owns the transaction (AGENT.md §4.3); the D2
    gate reads evidence existence through this store. The evidence root is
    the repo-relative ``data/evidence`` (the ``_DEFAULT_EVIDENCE_ROOT``
    app.main wires), computed here to avoid importing app.main (circular).
    """
    from pathlib import Path

    from app.plugins.evidence_store import FileEvidenceStore

    evidence_root = Path(__file__).resolve().parents[2] / "data" / "evidence"
    return FileEvidenceStore(evidence_root, db=session)


EvidenceStoreDep = Annotated[Any, Depends(get_evidence_store)]


# --- helpers --------------------------------------------------------------


def _resolve_sn_rows(session: Any, affected_sns: list[str]) -> list[ModuleInspection]:
    """Affected SNs must map to module_inspections rows (the link table's
    composite PK needs an inspection_id). Missing SNs -> 400."""
    rows = session.query(ModuleInspection).filter(ModuleInspection.sn.in_(affected_sns)).all()
    found = {r.sn for r in rows}
    missing = [sn for sn in affected_sns if sn not in found]
    if missing:
        raise HTTPException(status_code=400, detail={"error": "sn_not_found", "missing": missing})
    return rows


def _check_evidence_exists(session: Any, evidence_ids: list[str]) -> None:
    if not evidence_ids:
        return
    parsed_ids: list[Any] = []
    for eid in evidence_ids:
        try:
            parsed_ids.append(uuid.UUID(eid))
        except (ValueError, TypeError):
            parsed_ids.append(eid)  # keep the raw value; it will simply be "missing"
    rows = session.query(Evidence.evidence_id).filter(Evidence.evidence_id.in_(parsed_ids)).all()
    # `.in_` over a column attribute returns one-row tuples; unwrap.
    existing_ids = {str(row[0]) for row in rows}
    missing = [eid for eid in evidence_ids if eid not in existing_ids]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={"error": "evidence_not_found", "missing": missing},
        )


def _to_out(incident: QualityIncident) -> IncidentOut:
    return IncidentOut(
        incident_id=incident.incident_id,
        batch_id=incident.batch_id,
        trigger_type=incident.trigger_type,
        severity=incident.severity,
        status=incident.status,
        owner=incident.owner,
        due_at=incident.due_at.isoformat() if incident.due_at else None,
        containment_action=incident.containment_action,
        confirmed_root_cause=incident.confirmed_root_cause,
        corrective_action=incident.corrective_action,
        capa_verified=incident.capa_verified,
        root_cause_hypotheses=incident.root_cause_hypotheses,
        verification_id=incident.verification_id,
    )


def _record_trace(
    session: Any,
    incident: QualityIncident,
    skill: str,
    params: dict,
    structured: dict,
    validation_result: str = "pass",
) -> None:
    """Write one trace row; the caller owns the commit boundary.

    C9: ``validation_result`` defaults to "pass" (real transitions are
    verified by the P0 state machine). When a subgraph node is still a
    pass-through stub (no LLM / no evidence validation yet), the caller
    should pass "stub" so the audit trail does not claim a pass that
    was never actually validated.
    """
    TraceStore().record(
        session,
        task_id=str(uuid.uuid4()),
        batch_id=incident.batch_id,
        skill_name=skill,
        input_ref={"kind": "incident", "id": str(incident.incident_id)},
        plugins_invoked=["state_machine"],
        params_summary=params,
        model_version=None,
        structured_output=structured,
        validation_result=validation_result,
        failure_reason=None,
        human_action=None,
        started_at=datetime.now(UTC),
        finished_at=None,
    )


# --- endpoints ------------------------------------------------------------


@router.post("", status_code=201, response_model=IncidentOut)
def create_incident(payload: IncidentCreate, session: SessionDep) -> IncidentOut:
    batch = session.get(DeliveryBatch, payload.batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")
    # D8: archived terminal guard (shared) — fires before the not-isolated check.
    require_not_archived(payload.batch_id, session)
    if batch.status != "isolated":
        raise HTTPException(
            status_code=409,
            detail={"error": "batch_not_isolated", "status": batch.status},
        )

    _check_evidence_exists(session, payload.evidence_ids)
    sn_rows = _resolve_sn_rows(session, payload.affected_sns)

    incident = QualityIncident(
        incident_id=uuid.uuid4(),
        batch_id=payload.batch_id,
        trigger_type=payload.trigger_type,
        severity=payload.severity,
        evidence_ids=list(payload.evidence_ids),
        status="new",
    )
    session.add(incident)
    session.flush()  # incident_id available for the links
    for row in sn_rows:
        session.add(
            IncidentSnLink(incident_id=incident.incident_id, inspection_id=row.inspection_id)
        )
    # §8.2 组件级隔离：批次已隔离（守卫保证），受影响 SN 标记 isolated
    from app.plugins.auto_incident import mark_sns_isolated

    mark_sns_isolated(session, incident.batch_id, list(payload.affected_sns))
    session.commit()
    session.refresh(incident)
    return _to_out(incident)


@router.post("/{incident_id}/isolate", response_model=IncidentOut)
def isolate_incident(incident_id: uuid.UUID, session: SessionDep) -> IncidentOut:
    """incident new -> isolated (R2: P0 state machine new->isolated needs sn_linked).

    The create endpoint requires the batch to be already isolated and starts
    the incident at ``new``. The isolation action must be driven by an
    explicit API call (not a test manually mutating the row): this endpoint
    validates the ``new`` status + at least one ``incident_sn_links`` row
    (written at create), runs the state-machine edge, sets ``isolated`` and
    records a trace row.
    """
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    if incident.status != "new":
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_not_new", "status": incident.status},
        )
    links = session.query(IncidentSnLink).filter(IncidentSnLink.incident_id == incident_id).count()
    res = transition("new", "isolated", {"sn_linked": links > 0}, kind="incident")
    if not res.accepted:
        raise HTTPException(409, detail={"error": "isolate_denied", "reason": res.reason})
    incident.status = "isolated"
    _record_trace(session, incident, "incident_isolate", {}, {"incident_status": "isolated"})
    session.commit()
    session.refresh(incident)
    return _to_out(incident)


@router.post("/{incident_id}/assign", response_model=IncidentOut)
def assign_incident(
    incident_id: uuid.UUID, payload: IncidentAssign, session: SessionDep
) -> IncidentOut:
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    # R3: 认领须先隔离。P0 冻结异常状态机「新建 → 已隔离 → 已指派」，
    # `new` 态不可直接指派——须先调 `POST /{id}/isolate` 转为 `isolated`
    # 再认领。`new` 态请求返回 409（而非 404/静默成功）并提示先隔离。
    if incident.status == "new":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "incident_not_isolated",
                "status": incident.status,
                "hint": "请先调用 POST /api/incidents/{id}/isolate 完成隔离，再指派责任人",
            },
        )
    if incident.status not in ("isolated",):
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_not_assignable", "status": incident.status},
        )

    due_at = datetime.fromisoformat(payload.due_at)
    owner_assigned = bool(payload.owner)
    due_set = due_at is not None

    # incident: isolated -> assigned (precondition owner_assigned; R3: new 须先隔离)
    inc_res = transition(
        incident.status, "assigned", {"owner_assigned": owner_assigned}, kind="incident"
    )
    if not inc_res.accepted:
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_transition_denied", "reason": inc_res.reason},
        )
    incident.owner = payload.owner
    incident.due_at = due_at
    incident.containment_action = payload.containment_action
    incident.status = "assigned"

    # batch: isolated -> disposing (single transaction with the incident write)
    batch = session.get(DeliveryBatch, incident.batch_id)
    batch_res = transition(
        batch.status,
        "disposing",
        {"assigned_owner": owner_assigned, "due_at_set": due_set},
        kind="batch",
    )
    if not batch_res.accepted:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"error": "batch_transition_denied", "reason": batch_res.reason},
        )
    batch.status = "disposing"

    _record_trace(
        session,
        incident,
        "incident_assign",
        {"owner": payload.owner},
        {"incident_status": "assigned", "batch_status": "disposing"},
    )
    session.commit()
    session.refresh(incident)
    return _to_out(incident)


@router.post("/{incident_id}/capa", response_model=IncidentOut)
def incident_capa(
    incident_id: uuid.UUID, payload: IncidentCapa, session: SessionDep
) -> IncidentOut:
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")

    # L3: verified=True requires a pass reinspection record（两个分支共用）
    if payload.verified:
        re_ = (
            session.get(Reinspection, incident.verification_id)
            if incident.verification_id
            else None
        )
        if re_ is None or re_.result != "pass":
            raise HTTPException(
                status_code=400,
                detail={"error": "capa_verified_requires_pass_reinspection"},
            )

    # 复检通过后的 CAPA 补勾：pending_recheck 态允许更新字段与 verified 标记
    # （复检记录此时才存在，L3 门禁上方已校验），无状态转移。
    # D8：批次归档后只读（审查 M-8）——与 close 端点守卫一致。
    if incident.status == "pending_recheck":
        require_not_archived(incident.batch_id, session)
        incident.confirmed_root_cause = payload.confirmed_root_cause
        incident.corrective_action = payload.corrective_action
        incident.capa_verified = payload.verified
        _record_trace(
            session,
            incident,
            "incident_capa",
            {"verified": payload.verified, "update": True},
            {"incident_status": "pending_recheck"},
        )
        session.commit()
        session.refresh(incident)
        return _to_out(incident)

    if incident.status != "assigned":
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_not_assigned", "status": incident.status},
        )

    batch = session.get(DeliveryBatch, incident.batch_id)
    batch_res = transition(
        batch.status, "pending_recheck", {"measures_confirmed": True}, kind="batch"
    )
    if not batch_res.accepted:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"error": "batch_transition_denied", "reason": batch_res.reason},
        )

    # Validate-then-write (aligned with assign_incident): only mutate the incident
    # rows after the L3 gate and the batch transition have both passed, so a
    # failed batch transition rolls back cleanly without leaving the incident's
    # in-memory fields set.
    incident.confirmed_root_cause = payload.confirmed_root_cause
    incident.corrective_action = payload.corrective_action
    incident.capa_verified = payload.verified
    incident.status = "pending_recheck"
    batch.status = "pending_recheck"

    _record_trace(
        session,
        incident,
        "incident_capa",
        {"verified": payload.verified},
        {"incident_status": "pending_recheck", "batch_status": "pending_recheck"},
    )
    session.commit()
    session.refresh(incident)
    return _to_out(incident)


@router.post("/{incident_id}/close", response_model=IncidentCloseOut)
def close_incident_endpoint(incident_id: uuid.UUID, session: SessionDep) -> IncidentCloseOut:
    """复检通过后的闭环签署（P0 缺口 #1，设计文档 §9.8）。

    五条件硬校验（domain ``close_incident``：owner / CAPA 字段 / 独立复检
    记录 / 复检通过 / CAPA 已验证）→ 异常 ``pending_recheck -> closed``；
    仅当该批次**全部**异常处于终态时，批次 ``pending_recheck ->
    pending_release``（同事务 + 轨迹）。关闭是质量工程师的最终签署动作，
    复检通过不自动关闭（§4.4 闭环点）。
    """
    # M-7：行锁读取——与复检 fail 转拒绝路径互斥，杜绝 last-write-wins 竞态
    incident = (
        session.query(QualityIncident)
        .filter(QualityIncident.incident_id == incident_id)
        .with_for_update()
        .first()
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    require_not_archived(incident.batch_id, session)
    if incident.status != "pending_recheck":
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_not_closeable", "status": incident.status},
        )

    # 五条件（domain 纯函数）：缺失项逐条指名，不静默放行。
    re_row = (
        session.get(Reinspection, incident.verification_id) if incident.verification_id else None
    )
    view = IncidentView(
        incident_id=str(incident.incident_id),
        batch_id=str(incident.batch_id),
        trigger_type=incident.trigger_type,
        severity=incident.severity,
        status=incident.status,
        owner=incident.owner or "",
        confirmed_root_cause=incident.confirmed_root_cause or "",
        corrective_action=incident.corrective_action or "",
    )
    re_view = (
        ReinspectionView(
            reinspection_id=str(re_row.reinspection_id),
            incident_id=str(re_row.incident_id),
            sn="",
            result=re_row.result,
        )
        if re_row
        else None
    )
    gate = close_incident(view, re_view, incident.capa_verified)
    missing = list(gate.missing)

    # 需求五条件的领域函数未覆盖项（§5.2 ①②③）在此补齐：
    # ① 异常必须关联 SN；② 原始判定证据存在；③ 临时措施已填报。
    links = (
        session.query(IncidentSnLink)
        .filter(IncidentSnLink.incident_id == incident.incident_id)
        .count()
    )
    if links == 0:
        missing.append("sn_not_linked")
    declared = list(incident.evidence_ids or [])
    if not declared:
        missing.append("original_decision_evidence")
    else:
        ev_ids = []
        for e in declared:
            try:
                ev_ids.append(uuid.UUID(str(e)))
            except (ValueError, TypeError):
                pass
        ev_count = (
            session.query(Evidence).filter(Evidence.evidence_id.in_(ev_ids)).count()
            if ev_ids
            else 0
        )
        if ev_count == 0:
            missing.append("original_decision_evidence")
    if not (incident.containment_action or "").strip():
        missing.append("containment_action")

    if missing:
        raise HTTPException(
            status_code=409,
            detail={"error": "close_conditions_unmet", "missing": missing},
        )

    inc_res = transition(incident.status, "closed", {"close_conditions_met": True}, kind="incident")
    if not inc_res.accepted:
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_transition_denied", "reason": inc_res.reason},
        )
    incident.status = "closed"

    # §8.2 闭环：恢复被隔离的 SN（多异常重叠 SN 只在全部关闭后解除）
    from app.plugins.auto_incident import restore_sns_after_close

    restore_sns_after_close(session, incident)

    # 批次联动：全部异常终态（closed/rejected/scrapped）才推进到待放行。
    # 行锁串行化并发 close（I-2）：后到的事务在本行上等待，拿到锁后重读
    # open_count 才能看到前者已提交的 closed，避免批次永久卡死在待复检。
    batch = (
        session.query(DeliveryBatch)
        .filter(DeliveryBatch.batch_id == incident.batch_id)
        .with_for_update()
        .first()
    )
    batch_status = batch.status
    open_count = (
        session.query(QualityIncident)
        .filter(
            QualityIncident.batch_id == batch.batch_id,
            QualityIncident.status.in_(OPEN_INCIDENT_STATUSES),
        )
        .count()
    )
    if open_count == 0 and batch.status == "pending_recheck":
        batch_res = transition(
            batch.status,
            "pending_release",
            {"recheck_passed": True, "measures_effective": True},
            kind="batch",
        )
        if not batch_res.accepted:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail={"error": "batch_transition_denied", "reason": batch_res.reason},
            )
        batch.status = "pending_release"
        batch_status = "pending_release"

    _record_trace(
        session,
        incident,
        "incident_close",
        {},
        {"incident_status": "closed", "batch_status": batch_status},
    )
    session.commit()
    session.refresh(incident)
    return IncidentCloseOut(
        incident_id=incident.incident_id,
        incident_status=incident.status,
        batch_id=batch.batch_id,
        batch_status=batch_status,
    )


@router.get("", response_model=list[IncidentOut])
def list_incidents(session: SessionDep) -> list[IncidentOut]:
    return [_to_out(r) for r in session.query(QualityIncident).all()]


@router.get("/{incident_id}", response_model=IncidentOut)
def get_incident(incident_id: uuid.UUID, session: SessionDep) -> IncidentOut:
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    return _to_out(incident)


@router.post("/{incident_id}/attribute")
def attribute_incident(
    incident_id: uuid.UUID,
    payload: AttributeRequest,
    session: SessionDep,
    llm: LLMDep,
    evidence_store: EvidenceStoreDep,
) -> dict:
    """T-1 AI attribution trigger (single-request 3-way, D9; P1 cross-request).

    AGENT.md §4.3: the LLM call runs outside the transaction; the business
    write + trace row commit in one unit. The endpoint drives the
    ``AttributionSkill`` directly instead of running the incident subgraph:
    the subgraph's ``human_adopt`` interrupt was self-answered from the same
    request body (a fake HITL) and its ``run_reinspection`` node was a
    hardcoded ``{"result": "pass"}`` stub — neither had any real business
    effect here. Genuine reinspections go through
    ``POST /api/incidents/{id}/reinspections``; the subgraph itself remains a
    standalone topology scaffold for replay tests.

    Mock with no fixture key for this incident UUID -> 200 + empty + hint
    (spec §6 Q3: not a 404/500). Only ``FixtureMissError`` is treated as
    that business case — any other ``KeyError`` from the skill / evidence
    store / ORM propagates as a real 500 instead of being masked as
    "unknown incident".
    """
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    # D8: archived terminal guard (shared, single call site).
    require_not_archived(incident.batch_id, session)

    from app.plugins.attribution import AttributionSkill
    from app.plugins.llm_client import FixtureMissError

    try:
        hypotheses = AttributionSkill(llm, evidence_store).run(
            incident, trace=TraceStore(), session=session
        )
    except FixtureMissError:
        # Mock LLM has no fixture key for this incident UUID (spec §6 Q3):
        # graceful 200 + empty hypotheses + hint. The skill raises before any
        # write, so the rollback discards nothing of value.
        session.rollback()
        return {
            "incident_id": str(incident.incident_id),
            "decision": payload.decision,
            "hypotheses": [],
            "confirmed_root_cause": incident.confirmed_root_cause,
            "hint": "该 incident UUID 在归因 Mock 中无 fixture 键（未知 incident）",
        }

    # Business write: adopt persists the confirmed cause; one transaction.
    if payload.decision == "adopt":
        incident.confirmed_root_cause = payload.cause
    _record_trace(
        session,
        incident,
        "incident_attribute",
        {"decision": payload.decision, "cause": payload.cause},
        {
            "hypotheses": hypotheses,
            "hypotheses_source": "attribution" if hypotheses else "empty",
            "evidence_validation": "skill",
        },
        validation_result="pass" if hypotheses else "partial",
    )
    session.commit()
    session.refresh(incident)
    return {
        "incident_id": str(incident.incident_id),
        "decision": payload.decision,
        "hypotheses": hypotheses,
        "confirmed_root_cause": incident.confirmed_root_cause,
        "hint": None if hypotheses else "归因假设均未通过证据校验（D2），请人工填写根因",
    }
