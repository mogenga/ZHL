"""Intake routes: trigger the main-chain graph + confirm/reject (PA plan Task 8).

§5.6 / Q1–Q3 contract, implemented against the Task 7 graph:

- ``POST /api/intake`` streams the graph to its (always-firing) interrupt.
  On auto-confirm (``confidence >= 0.8``, Review Focus #1 boundary) the
  route *is* the confirmer: it persists the whole-batch confirmation record
  and resumes immediately; low confidence returns ``pending`` and the batch
  waits for the confirm endpoint.
- ``POST /api/intake/{task_id}/confirm`` writes the record BEFORE
  ``Command(resume=...)`` (graph nodes never write DB state, spec §2.3),
  so the judge node's A4 guard always sees a confirmed DB state.
- ``task_id`` = ``thread_id`` = ``str(batch_id)`` (Q2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infra.models import DeliveryBatch, ModuleInspection
from app.infra.repositories import CrossBatchSnConflict
from app.workflow.main_chain import build_main_chain_graph


def _get_batch_by_id(s: Session, batch_id: str, label: str) -> DeliveryBatch:
    """Load a batch by UUID path param; malformed → 404 (B3), not 500."""
    try:
        batch_uuid = uuid.UUID(batch_id)
    except ValueError:
        raise HTTPException(status_code=404, detail={label: batch_id}) from None
    batch = s.get(DeliveryBatch, batch_uuid)
    if batch is None:
        raise HTTPException(status_code=404, detail={label: batch_id})
    return batch


#: Measured-column whitelist (D7 audit surface). D7 locks the original
# inspection values (Postgres column-level REVOKE; uniform across engines),
# so human overrides are NEVER written back to the measured columns.
# This set documents the field names a confirmer may surface; it is used for
# audit / validation only, not applied to the row.
_CONFIRMABLE = frozenset({"pmax", "temp", "isc", "voc", "vm", "im", "ff", "appearance_result"})


class IntakeRequestIn(BaseModel):
    """POST /api/intake body: JSON inline (A2).

    The ``inspections`` rows are the dicts the Task 7 ``intake_node``
    persists; ``fields`` carries optional human overrides applied on
    auto-confirm (§5.6 Q3, symmetric with the confirm endpoint).
    """

    batch_id: str
    source_type: str = "IV_CSV"
    inspections: list[dict] = Field(default_factory=list)
    fields: dict | None = None


class IntakeConfirmIn(BaseModel):
    """POST /api/intake/{task_id}/confirm body ({action, fields?}, §5.6 Q3)."""

    action: str
    fields: dict | None = None


def _parse_iso_utc(value: str):
    """ISO-8601 string (with or without Z suffix) → aware UTC datetime."""
    s = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _graph_for(d: object, batch: DeliveryBatch) -> object:
    """Compile the main-chain graph over the request's collaborators."""
    return build_main_chain_graph(
        {
            "session": d.session,
            "inspect_repo": d.inspect_repo,
            "llm": d.llm,
            "evidence": d.evidence,
            "iso_table": d.iso_table,
            "rule_version_id": batch.rule_version_id,
            "inspection_type": "iv",
        },
        checkpointer=d.checkpointer,
    )


def build_router(get_deps: object) -> APIRouter:
    """Router bound to the app factory's per-request ``AppDeps`` (spec §8)."""
    router = APIRouter(prefix="/api/intake", tags=["intake"])

    @router.post("")
    def intake(
        body: IntakeRequestIn,
        d: object = Depends(get_deps),  # noqa: B008
    ) -> dict:
        """Q1: trigger the full graph from ``intake`` to the interrupt point.

        The graph pauses at ``human_confirm`` on every run (spec §5.6). On
        auto-confirm the route writes the confirmation record and resumes;
        low confidence returns ``status=pending`` + ``low_conf_sns`` (Q3).
        ``task_id`` = ``str(batch_id)`` (Q2).
        """
        s: Session = d.session
        batch = _get_batch_by_id(s, body.batch_id, "batch_id")

        graph = _graph_for(d, batch)
        cfg = {"configurable": {"thread_id": body.batch_id}}
        rows = [
            {**row, "source_type": row.get("source_type", body.source_type)}
            for row in body.inspections
        ]
        # JSON payloads carry captured_at as an ISO-8601 string; the ORM
        # expects a datetime (B3: marshal at the boundary, not in domain).
        for row in rows:
            if isinstance(row.get("captured_at"), str):
                row["captured_at"] = _parse_iso_utc(row["captured_at"])
        initial = {"batch_id": body.batch_id, "inspections": rows}
        try:
            for _chunk in graph.stream(initial, config=cfg):
                pass
            s.commit()
        except CrossBatchSnConflict as exc:
            # D5/D17 data-consistency guard (sample 4): the same SN in two
            # batches is rejected by the repository's EXCLUDE fallback; the
            # graph's intake node propagates it, and this route translates it
            # into the B3 error response (400 + conflicting SN).
            s.rollback()
            raise HTTPException(
                status_code=400,
                detail={"reason": "cross_batch_sn_conflict", "error": str(exc)},
            ) from exc
        state = graph.get_state(cfg).values or {}
        pending = bool(graph.get_state(cfg).next)

        # §5.6 / Q3: on auto-confirm (confidence >= 0.8, boundary Review
        # Focus #1) this route is the confirmer: persist the whole-batch
        # record + human_confirmation evidence, then resume immediately.
        # The judge node's A4 guard therefore always sees a confirmed DB
        # state (§2.3: graph nodes never write DB state).
        if pending and float(state.get("confidence") or 0.0) >= 0.8:
            confirmed_rows = s.scalars(
                select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
            ).all()
            # Defensive guard (§2.3): the graph's intake node just flushed the
            # rows in this same session/transaction, so the select above is
            # expected to see exactly the batch's rows. If it sees a different
            # count, the confirmation write below would be a silent no-op that
            # still resumes the graph — exactly the "DB state never written,
            # graph keeps running" failure §2.3 is meant to prevent. This is a
            # correctness invariant (it guards the judge's A4 check), so it
            # must fail closed even under `python -O` — an explicit `raise`,
            # not an `assert` (which the optimizer strips out), and a B3
            # structured 500 consistent with the other B3 error responses here.
            expected = len(body.inspections)
            if len(confirmed_rows) != expected:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "reason": "auto_confirm_row_count_mismatch",
                        "expected": expected,
                        "found": len(confirmed_rows),
                    },
                )
            # D7: measured columns are locked — do NOT apply ``body.fields``
            # back to the row. The override is recorded in the audit evidence
            # meta (below, ``proposed_overrides``) instead.
            for row in confirmed_rows:
                row.intake_status = "confirmed"
            d.evidence.save(
                "human_confirmation",
                {"kind": "batch", "id": body.batch_id},
                content=body.batch_id.encode("utf-8"),
                meta={
                    "action": "confirm",
                    "auto": True,
                    "confidence": state.get("confidence"),
                    "proposed_overrides": {
                        k: v for k, v in (body.fields or {}).items() if k in _CONFIRMABLE
                    },
                },
            )
            s.commit()
            for _chunk in graph.stream(
                Command(resume={"action": "confirm", "fields": body.fields}), config=cfg
            ):
                pass
            s.commit()
            state = graph.get_state(cfg).values or {}
            pending = bool(graph.get_state(cfg).next)
        return {
            "task_id": body.batch_id,
            "status": "pending" if pending else "confirmed",
            "extracted_fields": state.get("extraction") or {},
            "confidence": float(state.get("confidence") or 0.0),
            "low_conf_sns": state.get("low_conf_sns") or [],
        }

    @router.post("/{task_id}/confirm")
    def confirm_intake(
        task_id: str,
        body: IntakeConfirmIn,
        d: object = Depends(get_deps),  # noqa: B008
    ) -> dict:
        """§5.6: persist the whole-batch confirmation, then resume the graph."""
        s: Session = d.session
        batch = _get_batch_by_id(s, task_id, "task_id")

        # §5.6 / Q3: whole-batch intake_status + human_confirmation evidence
        # BEFORE the resume (graph nodes must not write DB state, spec §2.3).
        new_status = "confirmed" if body.action == "confirm" else "rejected"
        for row in s.scalars(
            select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
        ).all():
            row.intake_status = new_status
            # D7: measured columns are locked — do NOT apply ``body.fields``
            # back to the row; overrides stay in the audit evidence meta below.
        d.evidence.save(
            "human_confirmation",
            {"kind": "batch", "id": task_id},
            content=str(task_id).encode("utf-8"),
            meta={
                "action": body.action,
                "proposed_overrides": {
                    k: v for k, v in (body.fields or {}).items() if k in _CONFIRMABLE
                },
            },
        )
        s.commit()

        graph = _graph_for(d, batch)
        cfg = {"configurable": {"thread_id": task_id}}
        for _chunk in graph.stream(
            Command(resume={"action": body.action, "fields": body.fields}), config=cfg
        ):
            pass
        s.commit()
        return {"task_id": task_id, "intake_status": new_status}

    return router


__all__ = ["IntakeConfirmIn", "IntakeRequestIn", "build_router"]
