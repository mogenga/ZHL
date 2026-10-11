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

from app.domain.state_machine import transition
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


# --- dependency (session) — injected by the app factory in Task 9 ----------


def get_session() -> Any:
    """Return the request-scoped SQLAlchemy session.

    Task 9's FastAPI app factory overrides this with the real Postgres
    session; tests bind a SQLite session via ``app.dependency_overrides``.
    """
    raise RuntimeError("get_session not wired — use the app factory (Task 9)")


SessionDep = Annotated[Any, Depends(get_session)]


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
    session: Any, incident: QualityIncident, skill: str, params: dict, structured: dict
) -> None:
    """Write one trace row; the caller owns the commit boundary."""
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
        validation_result="pass",
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
    if incident.status not in ("new", "isolated"):
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_not_assignable", "status": incident.status},
        )

    due_at = datetime.fromisoformat(payload.due_at)
    owner_assigned = bool(payload.owner)
    due_set = due_at is not None

    # incident: new/isolated -> assigned (precondition owner_assigned)
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
    if incident.status != "assigned":
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_not_assigned", "status": incident.status},
        )

    # L3: verified=True requires a pass reinspection record
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

    incident.confirmed_root_cause = payload.confirmed_root_cause
    incident.corrective_action = payload.corrective_action
    incident.capa_verified = payload.verified
    incident.status = "pending_recheck"

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


@router.get("", response_model=list[IncidentOut])
def list_incidents(session: SessionDep) -> list[IncidentOut]:
    return [_to_out(r) for r in session.query(QualityIncident).all()]


@router.get("/{incident_id}", response_model=IncidentOut)
def get_incident(incident_id: uuid.UUID, session: SessionDep) -> IncidentOut:
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    return _to_out(incident)
