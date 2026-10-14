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

from fastapi import APIRouter, Depends, HTTPException, Request
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
from app.skills.remediation_conclusion import materialize_remediation_conclusion

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
    """整改措施填报/更正。三个可选字段的缺省语义是**不改**，不是"清空"。

    - ``containment_action``（临时措施）：指派时可填，但指派端点只接受
      ``isolated``（见 assign 的状态门），而 close 硬要求它非空 —— 指派时漏填
      必须有补救入口，否则该异常永远关不掉、批次永远放不了行（2026-10-15）。
    - ``verified`` 缺省 ``None`` = 保持原值：复检通过后该标记由系统自动置位，
      局部更新（如只补临时措施）不得把它悄悄刷成 false。
    """

    confirmed_root_cause: str
    corrective_action: str
    verified: bool | None = None
    containment_action: str | None = None


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


#: FR-3.6 / §11.3 规定的人工三选一：采纳、驳回、**修改**。
#: ``defer``（搁置）不是三选一之一——它只是"暂时不定"，不写任何业务字段，
#: 保留它是因为"先放一放"是真实需求，但它不计入 FR 的三选一。
ATTRIBUTION_DECISIONS = ("adopt", "reject", "modify", "defer")

#: 会写入 ``confirmed_root_cause`` 的决策（FR §8.3：确认根因必须来自
#: 「人工采纳的 AI 假设」或「人工填写」）。
ROOT_CAUSE_DECISIONS = ("adopt", "modify")


class AttributeRequest(BaseModel):
    """AI attribution request (D9 3-way; cross-request since 2026-10-14).

    ``decision`` omitted -> *start* phase: run the attribution Skill through
    the incident subgraph and park at the ``human_adopt`` interrupt, returning
    the hypotheses for a human to review on their own time.
    ``decision`` present -> legacy single-shot: generate **and** record the
    decision in one request (kept for compatibility / scripted flows).
    """

    decision: str | None = None  # ATTRIBUTION_DECISIONS
    cause: str | None = None
    #: decision == "modify" 时必填：人工修订后的根因文本。
    modified_cause: str | None = None
    #: 被采纳/被修改的那条假设在归因结果里的下标（用于命中率统计，FR §11.4）。
    hypothesis_index: int | None = None


class AdoptRequest(BaseModel):
    """Resume a parked attribution run with the reviewer's 3-way decision."""

    decision: str  # ATTRIBUTION_DECISIONS
    cause: str | None = None
    #: decision == "modify" 时必填：人工修订后的根因文本（原假设由其下标指认）。
    modified_cause: str | None = None
    hypothesis_index: int | None = None


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

    Delegates to ``app.main._default_llm_client`` — the single selection
    point (env key -> real client wrapped in the mock fallback, absent ->
    fixture mock). Keeping a second copy of this logic here meant the
    ``ZHL_LLM_MODE=mock`` downgrade did not apply to attribution: the demo
    switch was advertised as one flag but only governed the main chain.
    """
    from app.main import _default_llm_client

    return _default_llm_client()


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


def get_incident_checkpointer(request: Any) -> Any:
    """The subgraph checkpointer for cross-request attribution runs.

    Owned by the app factory (``app.state.incident_checkpointer``) so the
    ``human_adopt`` interrupt survives between the start request and the
    adopt request — the whole point of a real HITL over the old
    self-answered one. When the app did not wire one (bare test apps that
    include this router directly), a per-app ``MemorySaver`` is created on
    first use and cached on ``app.state`` — the right scope: the parked run
    must be visible to later requests on the same app, and to no other app.
    """
    checkpointer = getattr(request.app.state, "incident_checkpointer", None)
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
        request.app.state.incident_checkpointer = checkpointer
    return checkpointer


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


def _apply_optional_capa_fields(incident: QualityIncident, payload: IncidentCapa) -> None:
    """写两个**可缺省**字段，缺省 = 不改（见 ``IncidentCapa`` 的语义说明）。

    ``verified`` 缺省时保持原值：复检通过后它由系统自动置位，若这里按
    pydantic 默认值写 false，只补临时措施的一次更新就会把已验证洗掉，
    关闭条件重新变缺——前端漏传一个字段就复发卡死。
    """
    if payload.verified is not None:
        incident.capa_verified = payload.verified
    if payload.containment_action is not None:
        incident.containment_action = payload.containment_action.strip()


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
        _apply_optional_capa_fields(incident, payload)
        _record_trace(
            session,
            incident,
            "incident_capa",
            {
                "verified": payload.verified,
                "containment_action": payload.containment_action,
                "update": True,
            },
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
    _apply_optional_capa_fields(incident, payload)
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

        # 闭环成立 → 补写第 2 轮抽样结论（复检闭环的结论侧回写）。
        # 不做这一步，抽样 fail 的批次会永远卡在断言②：结论表里只有第 1 轮
        # 那条 fail，而复检通过/异常关闭都不落任何"结论"事实。闭环不成立时
        # 本调用不写任何行，断言②继续阻断（D9：无默认放行）。
        outcome = materialize_remediation_conclusion(session, batch)
        remediation_trace = {
            "sample_round": outcome.sample_round,
            "sample_result": outcome.result,
            "written": outcome.written,
            "skipped": outcome.skipped,
            "reason": outcome.reason,
        }
    else:
        remediation_trace = None

    _record_trace(
        session,
        incident,
        "incident_close",
        {},
        {
            "incident_status": "closed",
            "batch_status": batch_status,
            "remediation_conclusion": remediation_trace,
        },
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
    request: Request,
) -> dict:
    """AI attribution trigger (D9 3-way; cross-request HITL).

    Cross-request flow (the real HITL):

    1. ``POST .../attribute`` with **no** ``decision`` — generate hypotheses
       through a live ``AttributionSkill`` call, park the subgraph run at the
       ``human_adopt`` ``interrupt()``, and return the hypotheses. The parked
       thread lives in the app-owned checkpointer keyed by incident id.
    2. ``POST .../adopt`` with the reviewer's ``{"decision", "cause"}`` —
       resume the parked run and persist the decision. The graph node records
       the *fact* of the decision; the DB write (``confirmed_root_cause``)
       stays here, in the API layer, matching the main chain's split.

    ``decision`` present on this endpoint keeps the single-shot legacy shape
    (generate + decide in one request) for scripted callers.

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

    if payload.decision is None:
        return _start_attribution(
            incident,
            session,
            llm,
            evidence_store,
            checkpointer=get_incident_checkpointer(request),
        )
    return _single_shot_attribution(
        incident,
        session,
        llm,
        evidence_store,
        decision=payload.decision,
        cause=payload.cause,
        modified_cause=payload.modified_cause,
        hypothesis_index=payload.hypothesis_index,
    )


@router.post("/{incident_id}/adopt")
def adopt_attribution(
    incident_id: uuid.UUID,
    payload: AdoptRequest,
    session: SessionDep,
    llm: LLMDep,
    evidence_store: EvidenceStoreDep,
    request: Request,
) -> dict:
    """Resume a parked attribution run with the reviewer's 3-way decision.

    This is the *later request* half of the HITL: the hypotheses were
    generated (and parked) by ``POST .../attribute``; a human reviewed them
    out of band; this endpoint records the decision. Resuming a thread that
    has no parked run is a 409 — there is nothing to adopt.
    """
    from app.plugins.llm_client import FixtureMissError
    from app.workflow.incident_subgraph import build_incident_subgraph, resume_incident_run

    if payload.decision not in ATTRIBUTION_DECISIONS:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_decision", "allowed": list(ATTRIBUTION_DECISIONS)},
        )
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    require_not_archived(incident.batch_id, session)

    checkpointer = get_incident_checkpointer(request)
    graph = build_incident_subgraph(
        checkpointer,
        attribution_ctx=_attribution_ctx(incident, session, llm, evidence_store),
    )
    from app.workflow.incident_subgraph import incident_run_config

    parked = graph.get_state(incident_run_config(str(incident.incident_id)))
    if not parked.next:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_parked_attribution",
                "hint": "该异常没有待采纳的归因运行；请先调用 POST /attribute",
            },
        )
    hypotheses = parked.values.get("hypotheses") or []
    try:
        run = resume_incident_run(
            graph,
            str(incident.incident_id),
            {"decision": payload.decision, "cause": payload.cause},
        )
    except FixtureMissError:  # pragma: no cover — generation already happened
        session.rollback()
        raise HTTPException(status_code=409, detail={"error": "no_parked_attribution"}) from None

    _apply_attribution_decision(
        session,
        incident,
        payload.decision,
        payload.cause,
        list(hypotheses),
        modified_cause=payload.modified_cause,
        hypothesis_index=payload.hypothesis_index,
    )
    session.commit()
    session.refresh(incident)
    return {
        "incident_id": str(incident.incident_id),
        "decision": payload.decision,
        "human_decision": run["human_decision"],
        "hypotheses": hypotheses,
        "confirmed_root_cause": incident.confirmed_root_cause,
    }


def _attribution_ctx(incident: Any, session: Any, llm: Any, evidence_store: Any) -> dict:
    """Live deps for the subgraph closure (never serialized into state)."""
    from app.plugins.trace_store import TraceStore

    return {
        "incident": incident,
        "session": session,
        "llm": llm,
        "evidence_store": evidence_store,
        "trace": TraceStore(),
    }


def _attribution_prompt_context(session: Any, incident: Any) -> dict:
    """The concrete failure the model should reason about.

    Without this a live model answers in generalities ("rule mismatch",
    "data drift"); with it the hypotheses name the actual failing modules and
    their measured values — the difference between attribution that reads as
    analysis and attribution that reads as boilerplate.
    """
    context: dict = {}
    try:
        batch = session.get(DeliveryBatch, incident.batch_id)
        if batch is not None:
            context["batch_no"] = batch.batch_no
            context["model"] = batch.model
        links = (
            session.query(IncidentSnLink)
            .filter(IncidentSnLink.incident_id == incident.incident_id)
            .all()
        )
        rows = [session.get(ModuleInspection, link.inspection_id) for link in links]
        rows = [r for r in rows if r is not None]
        context["failing_sns"] = [r.sn for r in rows]
        context["rule_findings"] = [
            {
                "sn": r.sn,
                "pmax": float(r.pmax),
                "im": float(r.im),
                "ff": float(r.ff),
                "appearance_result": r.appearance_result,
            }
            for r in rows[:20]
        ]
    except Exception:  # noqa: BLE001 — context is an enhancement, never a blocker
        return context
    return context


def _generate_hypotheses_persisted(
    incident: Any, session: Any, llm: Any, evidence_store: Any
) -> list[dict]:
    """Run the attribution Skill once and return its validated hypotheses."""
    from app.plugins.attribution import AttributionSkill

    return AttributionSkill(llm, evidence_store).run(
        incident,
        trace=TraceStore(),
        session=session,
        prompt_context=_attribution_prompt_context(session, incident),
    )


def _start_attribution(
    incident: Any, session: Any, llm: Any, evidence_store: Any, *, checkpointer: Any
) -> dict:
    """Start phase: generate + park at the ``human_adopt`` interrupt."""
    from app.plugins.llm_client import FixtureMissError
    from app.workflow.incident_subgraph import build_incident_subgraph, start_incident_run

    try:
        hypotheses = _generate_hypotheses_persisted(incident, session, llm, evidence_store)
    except FixtureMissError:
        session.rollback()
        return {
            "incident_id": str(incident.incident_id),
            "status": "no_fixture",
            "hypotheses": [],
            "confirmed_root_cause": incident.confirmed_root_cause,
            "hint": "该 incident UUID 在归因 Mock 中无 fixture 键（未知 incident）",
        }

    graph = build_incident_subgraph(
        checkpointer,
        attribution_ctx=_attribution_ctx(incident, session, llm, evidence_store),
    )
    # Seed the hypotheses that were just generated: the subgraph's
    # generate_hypotheses node passes a seeded list straight through, so the
    # model is not asked the same question twice (two calls would also write
    # two attribution traces for one business action).
    run = start_incident_run(
        graph,
        str(incident.incident_id),
        seed={
            "hypotheses": hypotheses,
            # The list came from a real AttributionSkill run (above), so the
            # audit marker must say so; the seeded pass-through would
            # otherwise stamp it "stub" and understate the validation that
            # actually ran (D2 gate + trace).
            "hypotheses_source": "attribution",
            "evidence_validation": "skill",
        },
    )
    session.commit()
    session.refresh(incident)
    return {
        "incident_id": str(incident.incident_id),
        "status": "awaiting_human" if run["pending"] else "completed",
        "hypotheses": hypotheses,
        "hypotheses_source": run["hypotheses_source"],
        "evidence_validation": run["evidence_validation"],
        "confirmed_root_cause": incident.confirmed_root_cause,
        "hint": (
            None
            if hypotheses
            else "归因假设均未通过证据校验（D2），请人工填写根因；确认后调用 /adopt"
        ),
    }


def _single_shot_attribution(
    incident: Any,
    session: Any,
    llm: Any,
    evidence_store: Any,
    *,
    decision: str,
    cause: str | None,
    modified_cause: str | None = None,
    hypothesis_index: int | None = None,
) -> dict:
    """Legacy single-shot: generate + record the decision in one request."""
    from app.plugins.llm_client import FixtureMissError

    try:
        hypotheses = _generate_hypotheses_persisted(incident, session, llm, evidence_store)
    except FixtureMissError:
        session.rollback()
        return {
            "incident_id": str(incident.incident_id),
            "decision": decision,
            "hypotheses": [],
            "confirmed_root_cause": incident.confirmed_root_cause,
            "hint": "该 incident UUID 在归因 Mock 中无 fixture 键（未知 incident）",
        }

    _apply_attribution_decision(
        session,
        incident,
        decision,
        cause,
        hypotheses,
        modified_cause=modified_cause,
        hypothesis_index=hypothesis_index,
    )
    session.commit()
    session.refresh(incident)
    return {
        "incident_id": str(incident.incident_id),
        "decision": decision,
        "hypotheses": hypotheses,
        "confirmed_root_cause": incident.confirmed_root_cause,
        "hint": None if hypotheses else "归因假设均未通过证据校验（D2），请人工填写根因",
    }


def _apply_attribution_decision(
    session: Any,
    incident: Any,
    decision: str,
    cause: str | None,
    hypotheses: list[dict],
    *,
    modified_cause: str | None = None,
    hypothesis_index: int | None = None,
) -> None:
    """The one place an attribution decision becomes business state.

    Both entry points (single-shot and ``/adopt`` resume) funnel here so the
    adopt semantics cannot drift between them.

    FR-3.6 / §11.3 三选一的业务结果（``defer`` 是保留的第四种"先搁置"）：

    - ``adopt``   → ``confirmed_root_cause`` = 被采纳假设的 cause；
    - ``modify``  → ``confirmed_root_cause`` = **人工修订后**的文本，且 trace
      同时留原假设与修订版（FR §9.2「AI 给建议，人工担责任」：事后必须能
      分辨是模型说错了还是人改对了）；
    - ``reject``  → 不写任何根因字段，异常保持原状态，需人工另填；
    - ``defer``   → 同 reject 不写业务字段，语义是"稍后再定"。

    审计要点：旧实现把三种情况一律记成 ``decision="adopt"`` + 一个自由文本，
    库里无法区分「照抄假设」「改过假设」「纯人工填写」——FR §8.3 的
    「必须来自人工采纳的 AI 假设或人工填写」因此无法证明，§11.4 的
    Top-1/Top-3 与模型改正率也统计不出来。现在 trace 显式记录
    ``root_cause_source``（hypothesis / modified_hypothesis / manual）
    与被选假设的下标。
    """
    root_cause_source: str | None = None
    chosen: dict | None = None
    if hypothesis_index is not None and 0 <= hypothesis_index < len(hypotheses):
        maybe = hypotheses[hypothesis_index]
        if isinstance(maybe, dict):
            chosen = maybe

    if decision == "adopt":
        # 采纳 = 照抄被选假设。优先用后端存下来的假设文本，避免前端传参
        # 被改写导致"采纳"写入了与原假设不同的内容（那就不是采纳了）。
        adopted = (chosen or {}).get("cause") if chosen else None
        text = (adopted or cause or "").strip()
        if not text:
            raise HTTPException(
                status_code=400,
                detail={"error": "adopt_requires_cause", "hint": "采纳假设必须给出根因描述"},
            )
        incident.confirmed_root_cause = text
        root_cause_source = "hypothesis" if adopted else "manual"
    elif decision == "modify":
        text = (modified_cause or "").strip()
        if not text:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "modify_requires_text",
                    "hint": "修改假设必须给出修订后的根因文本",
                },
            )
        incident.confirmed_root_cause = text
        root_cause_source = "modified_hypothesis"
    else:
        # reject / defer：不写业务字段（FR §8.3：未被采纳的内容不得成为确认根因）
        root_cause_source = None

    _record_trace(
        session,
        incident,
        "incident_attribute",
        {
            "decision": decision,
            "cause": incident.confirmed_root_cause if decision in ROOT_CAUSE_DECISIONS else cause,
            "root_cause_source": root_cause_source,
            "hypothesis_index": hypothesis_index,
            "hypothesis_cause": (chosen or {}).get("cause") if chosen else None,
        },
        {
            "hypotheses": hypotheses,
            "hypotheses_source": "attribution" if hypotheses else "empty",
            "evidence_validation": "skill",
        },
        validation_result="pass" if hypotheses else "partial",
    )
