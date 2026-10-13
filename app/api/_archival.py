"""D6a auto-archive shared helper (T-3 Task 5).

A single call site for the "transition a terminal-disposition batch to
``archived`` in the same transaction as the caller's business write" step
that both the report route (``app/api/reports.py``) and the manual-reject
route (``app/api/release.py``) need.
"""

from __future__ import annotations

from typing import Any

from app.domain.state_machine import transition
from app.infra.models import DeliveryBatch


def auto_archive_if_terminal(session: Any, batch: DeliveryBatch) -> bool:
    """Run the D6a auto-archive transition for ``batch`` in the caller's tx.

    When ``batch.status`` is a terminal disposition (``released`` /
    ``rejected``) the state-machine edge to ``archived`` is executed; on
    acceptance the in-memory ``batch.status`` is updated to ``archived``
    and True is returned. Otherwise the batch is left untouched and False
    is returned (including when it is already ``archived`` — a terminal
    state, the edge is a no-op by the D6a empty-precondition invariant).

    D6a invariant: the ``("released","archived")`` / ``("rejected",
    "archived")`` edges in ``app/domain/state_machine.py`` MUST keep an
    EMPTY precondition set. If a precondition is ever added, the
    ``transition(...)`` below returns ``accepted=False`` and the batch
    silently stays non-archived while the caller's business write still
    commits with no error raised. Keep this comment in sync with any
    future Phase 0 state-machine change.
    """
    if batch.status not in ("released", "rejected"):
        return False
    res = transition(batch.status, "archived", {}, kind="batch")
    if res.accepted:
        batch.status = "archived"
        session.flush()
    return res.accepted
