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
from app.infra.models import AgentTaskTrace, DeliveryBatch, ModuleInspection, SampleConclusion
from app.infra.repositories import BatchRepository
from app.skills.judge import UnconfirmedSnsError, judge_batch


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
        from app.infra.models import QualityRuleVersion

        rule = s.get(QualityRuleVersion, uuid.UUID(body.rule_version_id))
        if rule is None:
            raise HTTPException(
                status_code=400,
                detail={"rule_version_id": body.rule_version_id, "reason": "unknown rule version"},
            )
        batch = BatchRepository().create_batch(
            s,
            body.batch_no,
            body.project_id,
            body.model,
            uuid.UUID(body.rule_version_id),
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
        batch = s.get(DeliveryBatch, uuid.UUID(batch_id))
        if batch is None:
            raise HTTPException(status_code=404, detail={"batch_id": batch_id})
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
        batch = s.get(DeliveryBatch, uuid.UUID(batch_id))
        if batch is None:
            raise HTTPException(status_code=404, detail={"batch_id": batch_id})
        try:
            result = judge_batch(s, d.evidence, uuid.UUID(batch_id), batch.rule_version_id)
        except UnconfirmedSnsError as exc:
            # Review Focus #3 / A4: 400 + unconfirmed SN checklist (B3).
            raise HTTPException(
                status_code=400, detail={"unconfirmed_sns": exc.unconfirmed}
            ) from exc
        s.commit()
        return {
            "batch_id": batch_id,
            "result": result["result"],
            "counts": result["counts"],
            "fail_sns": result["fail_sns"],
            "per_sn": result["per_sn"],
        }

    return router


#: Forward type name so route annotations stay import-safe at module load.
AppDepsRef = object

__all__ = ["BatchCreate", "build_router"]
