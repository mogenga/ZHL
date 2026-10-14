"""Rule version discovery + management API.

Adds ``GET /api/quality-rule-versions`` so the frontend can populate the
rule-version dropdown in ``index.html`` instead of forcing users to paste a
UUID by hand, plus write endpoints (create / publish / disable / audit
events) for the rule-version management workstream
(spec 2026-10-14-rule-version-management-design).

The list is the single source of truth for "which rule versions exist in this
deployment" — when the table is empty the frontend shows a "seed first" hint
and disables batch creation (no hard-coded UUIDs).

The routes are wired with the request-scoped session the same way the Phase B
routers are (``create_app`` overrides ``get_session`` with the live factory
closure).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.infra.models import QualityRuleVersion, RuleVersionEvent

router = APIRouter(prefix="/api/quality-rule-versions", tags=["rule-versions"])


def get_session() -> Any:
    """Return the request-scoped SQLAlchemy session.

    The app factory overrides this with the real session closure; tests bind
    a SQLite session via ``app.dependency_overrides``.
    """
    raise RuntimeError("get_session not wired — use the app factory")


SessionDep = Annotated[Any, Depends(get_session)]


class CreateRuleVersionRequest(BaseModel):
    project_id: str
    model: str
    name: str
    nameplate_power: float
    power_tolerance: float
    parameter_standards: dict
    current_bands: dict
    sample_plan: dict
    effective_at: datetime
    version_no: int
    source: Literal["manual", "recommended"] = "manual"
    recommendation_ref: dict | None = None


class CreateRuleVersionResponse(BaseModel):
    rule_version_id: UUID
    status: str
    version_no: int
    duplicate: bool
    event_id: UUID | None


@router.get("")
def list_rule_versions(session: SessionDep) -> dict:
    """Return rule versions as ``{items, available, reason}``.

    The wrapper shape lets the frontend distinguish "no rule versions yet"
    (seed first, available=True, empty items) from "the table/database is
    unavailable" (run the DDL, available=False). A 5xx is reserved for real
    backend faults; a missing table is a deploy-ops state.
    """
    from sqlalchemy.exc import OperationalError, ProgrammingError

    try:
        items = _list_rule_versions(session)
        return {"items": items, "available": True, "reason": None}
    except (OperationalError, ProgrammingError):
        # Table not created (001_init.sql not run) — a deploy-ops state.
        return {"items": [], "available": False, "reason": "table_missing"}


def _list_rule_versions(session: Any) -> list[dict]:
    rows = (
        session.query(QualityRuleVersion)
        .order_by(
            QualityRuleVersion.created_at.desc(),
            QualityRuleVersion.version_no.desc(),
        )
        .all()
    )
    return [
        {
            "rule_version_id": str(r.rule_version_id),
            "project_id": r.project_id,
            "model": r.model,
            "name": r.name,
            "status": r.status,
            "version_no": r.version_no,
            "effective_at": r.effective_at.isoformat() if r.effective_at else None,
        }
        for r in rows
    ]


def create_rule_version(
    body: CreateRuleVersionRequest, session: SessionDep
) -> CreateRuleVersionResponse:
    """建 draft 规则版本（幂等：同 project+model+version_no 已存在 → 200 duplicate）。"""
    existing = session.scalar(
        select(QualityRuleVersion).where(
            QualityRuleVersion.project_id == body.project_id,
            QualityRuleVersion.model == body.model,
            QualityRuleVersion.version_no == body.version_no,
        )
    )
    if existing is not None:
        session.commit()
        return CreateRuleVersionResponse(
            rule_version_id=existing.rule_version_id,
            status=existing.status,
            version_no=existing.version_no,
            duplicate=True,
            event_id=None,
        )
    rule = QualityRuleVersion(
        rule_version_id=uuid4(),
        project_id=body.project_id,
        model=body.model,
        name=body.name,
        nameplate_power=body.nameplate_power,
        power_tolerance=body.power_tolerance,
        parameter_standards=body.parameter_standards,
        current_bands=body.current_bands,
        sample_plan=body.sample_plan,
        effective_at=body.effective_at,
        status="draft",
        version_no=body.version_no,
    )
    session.add(rule)
    try:
        session.flush()  # 触发 UNIQUE 约束（竞态兜底）
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(QualityRuleVersion).where(
                QualityRuleVersion.project_id == body.project_id,
                QualityRuleVersion.model == body.model,
                QualityRuleVersion.version_no == body.version_no,
            )
        )
        assert existing is not None  # 竞态兜底：UNIQUE 冲突必有既存行
        return CreateRuleVersionResponse(
            rule_version_id=existing.rule_version_id,
            status=existing.status,
            version_no=existing.version_no,
            duplicate=True,
            event_id=None,
        )
    ev = RuleVersionEvent(
        event_id=uuid4(),
        rule_version_id=rule.rule_version_id,
        action="created",
        # 创建无独立 operator 字段；用 source 标记来源（manual/recommended）
        operator=body.source,
        detail={
            "version_no": body.version_no,
            "source": body.source,
            "recommendation_ref": body.recommendation_ref,
        },
    )
    session.add(ev)
    session.commit()
    session.refresh(rule)
    session.refresh(ev)
    return CreateRuleVersionResponse(
        rule_version_id=rule.rule_version_id,
        status=rule.status,
        version_no=rule.version_no,
        duplicate=False,
        event_id=ev.event_id,
    )


def _write_event(
    session: Any, rule_version_id: UUID, action: str, operator: str, detail: dict
) -> UUID:
    """写审计事件并 commit，返回新 event_id（供 publish/disable 复用）。"""
    ev = RuleVersionEvent(
        event_id=uuid4(),
        rule_version_id=rule_version_id,
        action=action,
        operator=operator,
        detail=detail,
    )
    session.add(ev)
    session.commit()
    return ev.event_id


def _load_rule_or_404(session: Any, rule_version_id: UUID) -> QualityRuleVersion:
    rule = session.get(QualityRuleVersion, rule_version_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule_version_not_found")
    return rule


@router.post("", response_model=CreateRuleVersionResponse)
def create_rule_version_endpoint(
    body: CreateRuleVersionRequest, session: SessionDep, response: Response
) -> CreateRuleVersionResponse:
    out = create_rule_version(body, session)
    response.status_code = 200 if out.duplicate else 201
    return out


__all__ = [
    "CreateRuleVersionRequest",
    "CreateRuleVersionResponse",
    "_load_rule_or_404",
    "_write_event",
    "create_rule_version",
    "get_session",
    "router",
]
