"""Shared API guards (PB Task 9 D8 archived terminal-state guard).

Lives in its own module so the routers and the app factory can both import
``require_not_archived`` without a circular ``app.api.main -> release ->
app.api.main`` cycle.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID as _UUID

from fastapi import HTTPException

from app.infra.models import DeliveryBatch

#: The D8 guard's stable 409 payload (Review Focus #4 — same text on every
#: write endpoint so clients can key off it).
ARCHIVED_ERROR = "batch archived, terminal state"


def require_not_archived(batch_id: Any, session: Any) -> None:
    """D8 archived terminal guard.

    A shared dependency that the batch-scoped write endpoints (release,
    report) inject. It loads the batch and 409s when ``status == "archived"``.
    The batch id may be a ``uuid.UUID`` (path param) or a string; both are
    normalised here. Non-UUID / unknown ids fall through to the endpoint's
    own 404.
    """
    key = batch_id if isinstance(batch_id, str) else str(batch_id)
    try:
        batch = session.get(DeliveryBatch, _UUID(key))
    except (ValueError, TypeError):
        return
    if batch is not None and batch.status == "archived":
        raise HTTPException(status_code=409, detail={"error": ARCHIVED_ERROR})


__all__ = ["require_not_archived", "ARCHIVED_ERROR"]
