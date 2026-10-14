"""Batch routes: create / detail / standalone judge (PA plan Task 8 Interfaces).

Layering (spec §3.2): routes marshal requests, run one request-scoped
transaction (AGENT.md §4.3), and translate skill/plugin exceptions into B3
responses (``{"detail": ...}``). Q1: the judge endpoint is an independent
Skill call — it never runs the graph.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: F401 (reserved)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.state_machine import transition
from app.infra.models import (
    AgentTaskTrace,
    DeliveryBatch,
    ModuleInspection,
    QualityRuleVersion,
    SampleConclusion,
)
from app.infra.repositories import BatchRepository
from app.skills.judge import RuleNotEffective, UnconfirmedSnsError, judge_batch


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    """B3: malformed UUID path/body param -> 404 detail, never a raw
    ``ValueError`` -> 500. Uniform with the intake route's ``_get_batch_by_id``
    guard (M3, intake.py) and ``_load_batch`` below: 404 across the board."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail={label: value}) from None


def _load_batch(s: Session, batch_id: str) -> DeliveryBatch:
    """Load a batch by UUID path param; malformed or unknown -> 404 (B3)."""
    try:
        batch_uuid = uuid.UUID(batch_id)
    except ValueError:
        raise HTTPException(status_code=404, detail={"batch_id": batch_id}) from None
    batch = s.get(DeliveryBatch, batch_uuid)
    if batch is None:
        raise HTTPException(status_code=404, detail={"batch_id": batch_id})
    return batch


#: Route-local body model (plan Task 8 Interfaces).
class BatchCreate(BaseModel):
    batch_no: str
    project_id: str
    model: str
    rule_version_id: str
    planned_quantity: int = Field(ge=1)


def build_router(get_deps: object) -> APIRouter:
    """Router bound to the app factory's per-request ``AppDeps`` (spec §8).

    ``get_deps`` is the closure ``create_app`` hands the router so the
    routes keep one request == one transaction (§4.3) and stay fully
    injectable for the in-memory SQLite API tests.
    """
    router = APIRouter(prefix="/api/batches", tags=["batches"])

    @router.post("", status_code=201)
    def create_batch(
        body: BatchCreate,
        d: AppDepsRef = Depends(get_deps),  # noqa: B008
    ) -> dict:
        """R5: insert the draft batch and advance it to ``pending_intake``."""
        s: Session = d.session
        rule_uuid = _parse_uuid(body.rule_version_id, "rule_version_id")
        rule = s.get(QualityRuleVersion, rule_uuid)
        if rule is None:
            raise HTTPException(
                status_code=400,
                detail={"rule_version_id": body.rule_version_id, "reason": "unknown rule version"},
            )
        if rule.status != "published":
            raise HTTPException(
                status_code=400,
                detail={
                    "rule_version_id": body.rule_version_id,
                    "reason": "rule version not published",
                    "status": rule.status,
                },
            )
        batch = BatchRepository().create_batch(
            s,
            body.batch_no,
            body.project_id,
            body.model,
            rule_uuid,
            body.planned_quantity,
        )
        tr = transition("draft", "pending_intake", {}, kind="batch")
        if not tr.accepted:
            raise HTTPException(status_code=400, detail={"reason": tr.reason})
        batch.status = "pending_intake"
        s.commit()
        return {"batch_id": str(batch.batch_id), "status": batch.status}

    @router.get("/{batch_id}")
    def get_batch_detail(
        batch_id: str,
        d: AppDepsRef = Depends(get_deps),  # noqa: B008
    ) -> dict:
        """A8/B2: batch + per-SN verdict detail + sample conclusion + timeline."""
        s: Session = d.session
        batch = _load_batch(s, batch_id)
        inspections = [
            {
                "sn": r.sn,
                "intake_status": r.intake_status,
                "decision_status": r.decision_status,
                "decision_evidence": r.decision_evidence,
            }
            for r in s.scalars(
                select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
            ).all()
        ]
        conclusion = s.scalars(
            select(SampleConclusion).where(SampleConclusion.batch_id == batch.batch_id)
        ).first()
        traces = s.scalars(
            select(AgentTaskTrace)
            .where(AgentTaskTrace.batch_id == batch.batch_id)
            .order_by(AgentTaskTrace.started_at)
        ).all()
        timeline = [
            {
                "skill_name": t.skill_name,
                "target": (t.params_summary or {}).get("target"),
                "accepted": (t.params_summary or {}).get("accepted"),
                "started_at": t.started_at.isoformat() if t.started_at else None,
            }
            for t in traces
        ]
        return {
            "batch_id": str(batch.batch_id),
            "batch_no": batch.batch_no,
            "status": batch.status,
            "planned_quantity": batch.planned_quantity,
            "rule_version_id": str(batch.rule_version_id),
            "inspections": inspections,
            "sample_result": conclusion.result if conclusion else None,
            "status_timeline": timeline,
        }

    @router.post("/{batch_id}/judge")
    def judge(
        batch_id: str,
        d: AppDepsRef = Depends(get_deps),  # noqa: B008
    ) -> dict:
        """Q1: independent judge-skill call for standalone re-judging."""
        s: Session = d.session
        batch = _load_batch(s, batch_id)
        try:
            result = judge_batch(s, d.evidence, batch.batch_id, batch.rule_version_id)
        except UnconfirmedSnsError as exc:
            # Review Focus #3 / A4: 400 + unconfirmed SN checklist (B3).
            raise HTTPException(
                status_code=400, detail={"unconfirmed_sns": exc.unconfirmed}
            ) from exc
        except RuleNotEffective as exc:
            # R1: the selected rule version had not yet taken effect when the
            # inspections happened. Map to the stable 400 reason aligned with
            # "unknown rule version" so the caller can fall back to a rule that
            # was actually in force (see the create endpoint's L83 shape).
            raise HTTPException(
                status_code=400,
                detail={
                    "rule_version_id": str(batch.rule_version_id),
                    "reason": "rule not effective yet",
                },
            ) from exc
        s.commit()
        return {
            "batch_id": batch_id,
            "result": result["result"],
            "counts": result["counts"],
            "fail_sns": result["fail_sns"],
            "per_sn": result["per_sn"],
        }

    @router.get("")
    def list_batches(
        status: str | None = None,
        d: AppDepsRef = Depends(get_deps),  # noqa: B008
    ) -> list[dict]:
        """批次列表（前端工作台主数据源）。

        返回 batch_id / batch_no / project_id / model / status /
        planned_quantity / created_at，按创建时间倒序；可选 status 过滤；
        不做分页（平台规模小，全量返回即可；若未来需要再加分页）。
        """
        s: Session = d.session
        stmt = select(DeliveryBatch)
        if status is not None:
            stmt = stmt.where(DeliveryBatch.status == status)
        # 按创建时间倒序；Postgres/SQLite DESC 默认 NULL 排最后（无需 NULLS LAST）。
        stmt = stmt.order_by(DeliveryBatch.created_at.desc())
        rows = s.scalars(stmt).all()
        return [
            {
                "batch_id": str(r.batch_id),
                "batch_no": r.batch_no,
                "project_id": r.project_id,
                "model": r.model,
                "status": r.status,
                "planned_quantity": r.planned_quantity,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    return router


#: Forward type name so route annotations stay import-safe at module load.
AppDepsRef = object

__all__ = ["BatchCreate", "build_router"]
