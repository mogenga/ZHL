"""Rule version discovery API (read-only, frontend workbench).

Adds ``GET /api/quality-rule-versions`` so the frontend can populate the
rule-version dropdown in ``index.html`` instead of forcing users to paste a
UUID by hand. The list is the single source of truth for "which rule versions
exist in this deployment" — when the table is empty the frontend shows a
"seed first" hint and disables batch creation (no hard-coded UUIDs).

Read-only: no write endpoint, no D8 guard needed. The route is wired with the
request-scoped session the same way the Phase B routers are (``create_app``
overrides ``get_session`` with the live factory closure).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.infra.models import QualityRuleVersion

router = APIRouter(prefix="/api/quality-rule-versions", tags=["rule-versions"])


def get_session() -> Any:
    """Return the request-scoped SQLAlchemy session.

    The app factory overrides this with the real session closure; tests bind
    a SQLite session via ``app.dependency_overrides``.
    """
    raise RuntimeError("get_session not wired — use the app factory")


SessionDep = Annotated[Any, Depends(get_session)]


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


__all__ = ["router", "get_session"]
