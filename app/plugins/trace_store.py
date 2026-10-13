"""TraceStore plugin (PB Task 4, D1 三层留痕 — 轨迹层).

Wraps persistence of ``agent_task_traces`` rows. The store is a thin,
session-bound helper: it does NOT own the session or the commit boundary
(caller owns the transaction — AGENT.md §4.3: LLM call happens outside the
transaction, then the trace write commits in the same transaction as the
business write it accompanies).

``list_for_batch`` returns rows in ``started_at`` ascending order — this is
the trace timeline (Review Focus #3: state-audit leaves a readable sequence
of what each Skill / plugin did, in the order it happened).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infra.models import AgentTaskTrace

#: ``record`` signature fields (kept as a docstring-level contract; see
#: ``record`` for the full parameter list).
_RECORD_FIELDS = (
    "task_id",
    "batch_id",
    "skill_name",
    "input_ref",
    "plugins_invoked",
    "params_summary",
    "model_version",
    "structured_output",
    "validation_result",
    "failure_reason",
    "human_action",
    "started_at",
    "finished_at",
)


class TraceStore:
    """Persists and lists ``AgentTaskTrace`` rows on a SQLAlchemy session."""

    def record(self, session: Session, **kwargs: Any) -> AgentTaskTrace:
        """Insert one trace row.

        All ``agent_task_traces`` columns except the auto-filled
        ``task_id`` default are accepted as keyword arguments. ``task_id`` is
        auto-minted (uuid4) when omitted. ``validation_result`` defaults to
        ``"partial"`` (the ORM column default) when not supplied.

        The row is flushed (not committed) so the caller controls the
        transaction boundary.
        """
        # ``task_id`` / ``batch_id`` — the DDL columns are UUID (batch_id is
        # a UUID FK to delivery_batches). Accept either a uuid.UUID instance
        # or a UUID-parseable string and coerce to UUID; non-parseable values
        # pass through as-is (the DB layer will reject them, which is the
        # desired loud-failure behavior rather than a silent type error).
        for field_name in ("task_id", "batch_id"):
            value = kwargs.get(field_name)
            if value is not None and not isinstance(value, uuid.UUID):
                try:
                    kwargs[field_name] = uuid.UUID(str(value))
                except (ValueError, TypeError):
                    pass  # keep original; DB layer validates the type

        # Build the ORM object from the model's column names. C3: unknown
        # kwargs are NOT silently dropped (a typo'd trace field would
        # otherwise leave a gap in the audit trail while looking healthy);
        # they raise TypeError, matching the ORM __init__ contract.
        column_names = {c.name for c in AgentTaskTrace.__table__.columns}
        unknown = sorted(k for k in kwargs if k not in column_names)
        if unknown:
            raise TypeError(
                f"TraceStore.record: unknown field(s) {unknown}; "
                f"valid fields: {sorted(column_names)}"
            )
        init_kwargs = dict(kwargs)

        trace = AgentTaskTrace(**init_kwargs)
        session.add(trace)
        session.flush()
        return trace

    def list_for_batch(self, session: Session, batch_id: str | uuid.UUID) -> list[AgentTaskTrace]:
        """Return the batch's traces in ``started_at`` ascending order."""
        if isinstance(batch_id, str):
            batch_id = uuid.UUID(batch_id)
        stmt = select(AgentTaskTrace).where(AgentTaskTrace.batch_id == batch_id)
        stmt = stmt.order_by(AgentTaskTrace.started_at.asc())
        return list(session.scalars(stmt).all())
