"""Release endpoint (PB Task 8, D4 fail-keeps-state; D7-7 item 7).

Contract (plan Task 8):

- ``POST /api/batches/{id}/release`` — body ``{approved_by}``. The server
  assembles all seven ``assert_release`` inputs from the DB:

  1. batch status (``pending_release``)
  2. the batch's latest ``SampleConclusion`` result
  3. the batch's open incidents
  4. isolated SNs (module_inspections rows with intake isolated)
  5. the batch's required evidence set
  6. CAPA verification state (per open incident, the ``capa_verified`` flag)
  7. **reinspection scope rows** (one ``ReinspectionScopeIn`` per open
     incident: the incident's full affected-SN set from
     ``incident_sn_links``, plus the four ISO values resolved from
     ``module_inspections`` + the latest sample) and the preset ISO table.

  Then it calls the pure ``assert_release``. All-pass: write a release
  (human-confirmation) evidence row, transition the batch
  ``pending_release -> released`` (preconditions assertions_passed +
  human_approved), and record a trace — all in one transaction. The
  auto-archive is NOT done here (D6a: archiving happens at the report
  call, which is what makes the batch terminal). Any assertion fail:
  **no state transition, no ``release_decision`` write, no DB mutation
  beyond the read-only assertion** — respond 200 with the full
  ``AssertionResult`` (per-item + reasons naming the offending incident /
  SN / evidence id) and a "assertion failed, retriable" trace marker.

- batch not ``pending_release`` -> 409; ``archived`` -> 409 (D8).

Purity: this endpoint is the *only* place that resolves the ISO values
and the incident required-SN sets from the DB and feeds them to the
domain function, keeping ``app/domain`` read-free (P0 contract).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api._archival import auto_archive_if_terminal
from app.api.guards import require_not_archived
from app.domain.release import assert_release
from app.domain.state_machine import transition
from app.domain.types import (
    BatchView,
    CapaView,
    EvidenceView,
    IncidentView,
    IsoRow,
    ReinspectionScopeIn,
    SampleView,
)
from app.infra.models import (
    DeliveryBatch,
    Evidence,
    IncidentSnLink,
    IsoSamplingRow,
    ModuleInspection,
    QualityIncident,
    Reinspection,
    SampleConclusion,
)
from app.plugins.trace_store import TraceStore
from app.skills.report_skill import ReportSkill

router = APIRouter(prefix="/api/batches", tags=["release"])


class ReleaseRequest(BaseModel):
    approved_by: str


class ReleaseResponse(BaseModel):
    passed: bool
    batch_id: uuid.UUID
    status: str
    release_decision: str
    checks: list[dict]


class RejectRequest(BaseModel):
    reason: str
    rejected_by: str


class RejectResponse(BaseModel):
    batch_id: uuid.UUID
    status: str
    auto_archived: bool
    reason: str
    report_id: uuid.UUID | None


def get_session() -> Any:
    raise RuntimeError("get_session not wired — use the app factory (Task 9)")


SessionDep = Annotated[Any, Depends(get_session)]


# --- input assembly helpers (DB -> domain views) ---------------------------


def _latest_sample(session: Any, batch_id: uuid.UUID) -> SampleView:
    row = (
        session.query(SampleConclusion)
        .filter(SampleConclusion.batch_id == batch_id)
        .order_by(SampleConclusion.sample_round.desc())
        .first()
    )
    if row is None:
        # No sampling on the batch: the assertion treats "pass" as the
        # default (the judge's per-module results are the gate).
        return SampleView(result="pass", inspection_type="module")
    return SampleView(
        result=row.result,
        inspection_type=row.inspection_type,
        sample_round=row.sample_round,
    )


def _incident_views(session: Any, batch_id: uuid.UUID) -> list[IncidentView]:
    rows = session.query(QualityIncident).filter(QualityIncident.batch_id == batch_id).all()
    return [
        IncidentView(
            incident_id=str(r.incident_id),
            batch_id=str(r.batch_id),
            trigger_type=r.trigger_type,
            severity=r.severity,
            status=r.status,
            owner=r.owner or "",
            confirmed_root_cause=r.confirmed_root_cause or "",
            corrective_action=r.corrective_action or "",
        )
        for r in rows
    ]


def _isolated_sns(session: Any, batch_id: uuid.UUID) -> list[str]:
    rows = (
        session.query(ModuleInspection.sn)
        .filter(
            ModuleInspection.batch_id == batch_id,
            ModuleInspection.intake_status == "isolated",
        )
        .all()
    )
    return [r[0] for r in rows]


def _required_evidence(
    session: Any, batch_id: uuid.UUID, incident_ids: list[str]
) -> list[EvidenceView]:
    """The release gate's evidence set: every piece of evidence the batch's
    incidents + sample conclusions reference."""
    evidence_ids: set[str] = set()
    for iid in incident_ids:
        inc = session.get(QualityIncident, uuid.UUID(iid))
        if inc:
            evidence_ids.update(str(e) for e in (inc.evidence_ids or []))
    concls = session.query(SampleConclusion).filter(SampleConclusion.batch_id == batch_id).all()
    for c in concls:
        if c.evidence_id:
            evidence_ids.add(str(c.evidence_id))
    # Resolve to EvidenceView rows (only those that actually exist).
    views: list[EvidenceView] = []
    for eid in sorted(evidence_ids):
        try:
            ev = session.get(Evidence, uuid.UUID(eid))
        except (ValueError, TypeError):
            ev = None
        if ev is not None:
            views.append(
                EvidenceView(
                    evidence_id=str(ev.evidence_id),
                    evidence_type=ev.evidence_type,
                    object_ref=ev.object_ref or {},
                )
            )
    return views


def _capa_views(session: Any, incident_views: list[IncidentView]) -> list[CapaView]:
    out: list[CapaView] = []
    for iv in incident_views:
        if iv.status in ("closed", "rejected", "scrapped"):
            continue  # closed incidents don't need a CAPA check
        out.append(
            CapaView(
                incident_id=iv.incident_id,
                containment_action=iv.confirmed_root_cause,
                confirmed_root_cause=iv.confirmed_root_cause,
                corrective_action=iv.corrective_action,
                verified=_incident_capa_verified(session, iv.incident_id),
            )
        )
    return out


def _incident_capa_verified(session: Any, incident_id: str) -> bool:
    inc = session.get(QualityIncident, uuid.UUID(incident_id))
    return bool(inc.capa_verified) if inc else False


def _incident_required_sns(session: Any, incident_id: str) -> tuple[str, ...]:
    """The incident's full affected-SN set (the minimal reinspection scope)."""
    links = (
        session.query(IncidentSnLink.inspection_id)
        .filter(IncidentSnLink.incident_id == uuid.UUID(incident_id))
        .all()
    )
    inspection_ids = [r[0] for r in links]
    if not inspection_ids:
        return ()
    rows = (
        session.query(ModuleInspection.sn)
        .filter(ModuleInspection.inspection_id.in_(inspection_ids))
        .all()
    )
    return tuple(r[0] for r in rows)


def _reinspection_scope_rows(
    session: Any,
    batch: DeliveryBatch,
    incident_views: list[IncidentView],
    sample: SampleView,
) -> tuple[list[ReinspectionScopeIn], dict[str, tuple[str, ...]]]:
    """One ReinspectionScopeIn per open incident: the incident's *reinspection
    coverage* (scope_sns from the Reinspection row, or the full affected set
    when no row exists) plus the four ISO values resolved here (Task 8 is
    the resolver that keeps the domain layer read-free).

    The required-SN set for item 7 is what the incident *should* cover
    (its full incident_sn_links set); the scope_sns field is what a
    reinspection *actually* covered. A gap between the two is exactly the
    Review Focus #2 signal item 7 is meant to catch — so the row must
    carry the actual coverage, not re-declare the required set.
    """
    open_ids = [
        iv.incident_id
        for iv in incident_views
        if iv.status not in ("closed", "rejected", "scrapped")
    ]
    inspection_type = sample.inspection_type or "module"
    inspection_level = "full"
    iso_rows = _iso_table(session)
    aql = iso_rows[0].aql if iso_rows else 0.65
    out: list[ReinspectionScopeIn] = []
    required_sns: dict[str, tuple[str, ...]] = {}
    for iid in open_ids:
        actual_sns = _incident_reinspection_sns(session, iid)
        required_sns[iid] = _incident_required_sns(session, iid)
        out.append(
            ReinspectionScopeIn(
                incident_id=iid,
                scope_sns=actual_sns,
                lot_size=len(actual_sns) or 1,
                inspection_type=inspection_type,
                inspection_level=inspection_level,
                aql=aql,
            )
        )
    return out, required_sns


def _incident_reinspection_sns(session: Any, incident_id: str) -> tuple[str, ...]:
    """The SNs a reinspection actually covered for this incident.

    Read from the Reinspection row's ``scope_sns`` column (the D5 explicit
    scope). When no reinspection row exists, the coverage is empty — which
    makes item 7's "no scope row / missing SNs" branch fire (a required
    set with zero coverage is a gap).
    """
    rows = (
        session.query(Reinspection.scope_sns)
        .filter(Reinspection.incident_id == uuid.UUID(incident_id))
        .all()
    )
    covered: set[str] = set()
    for row in rows:
        covered.update(row[0] or [])
    return tuple(sorted(covered))


def _iso_table(session: Any) -> list[IsoRow]:
    rows = session.query(IsoSamplingRow).all()
    return [
        IsoRow(
            inspection_type=r.inspection_type,
            inspection_level=r.inspection_level,
            aql=float(r.aql),
            lot_size_min=r.lot_size_min,
            lot_size_max=r.lot_size_max,
            n=r.n,
            ac=r.ac,
            re=r.re,
        )
        for r in rows
    ]


# --- endpoint ---------------------------------------------------------------


@router.post("/{batch_id}/release", response_model=ReleaseResponse)
def release_batch(
    batch_id: uuid.UUID, payload: ReleaseRequest, session: SessionDep
) -> ReleaseResponse:
    batch = session.get(DeliveryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")

    # D8: archived is terminal — the shared guard (guards.require_not_archived)
    # loads the batch and 409s with the stable ARCHIVED_ERROR payload.
    require_not_archived(batch_id, session)
    # Must be in pending_release to release.
    if batch.status != "pending_release":
        raise HTTPException(
            status_code=409,
            detail={"error": "batch_not_pending_release", "status": batch.status},
        )

    # Assemble the seven assertion inputs (DB -> domain views).
    incident_views = _incident_views(session, batch_id)
    open_ids = [
        iv.incident_id
        for iv in incident_views
        if iv.status not in ("closed", "rejected", "scrapped")
    ]
    sample = _latest_sample(session, batch_id)
    iso_table = _iso_table(session)
    reinspection_scopes, required_sns = _reinspection_scope_rows(
        session, batch, incident_views, sample
    )

    result = assert_release(
        batch=BatchView(batch_id=str(batch.batch_no), status=batch.status),
        sample=sample,
        incidents=incident_views,
        isolated_sns=_isolated_sns(session, batch_id),
        evidence=_required_evidence(session, batch_id, open_ids),
        capa=_capa_views(session, incident_views),
        reinspection_scopes=reinspection_scopes,
        iso_table=iso_table,
        required_sns=required_sns,
    )

    if result.passed:
        # All-pass: write release evidence + transition + trace in one tx.
        # C8: content_hash is a real sha256 over the canonical JSON of the
        # evidence content (sort_keys=True for stability) — not a random uuid.
        _release_content = {"approved_by": payload.approved_by, "assertions": result.to_dict()}
        _release_hash = hashlib.sha256(
            json.dumps(_release_content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        release_ev = Evidence(
            evidence_id=uuid.uuid4(),
            evidence_type="human_confirmation",
            object_ref={"kind": "batch", "id": batch.batch_no, "approved_by": payload.approved_by},
            content_hash=f"sha256:{_release_hash}",
            content=_release_content,
            generated_at=datetime.now(UTC),
            generated_by=payload.approved_by,
        )
        session.add(release_ev)
        batch.release_evidence_id = release_ev.evidence_id
        batch.status = "released"
        batch.release_decision = "released"
        TraceStore().record(
            session,
            task_id=str(uuid.uuid4()),
            batch_id=batch_id,
            skill_name="release",
            input_ref={"kind": "batch", "id": batch.batch_no},
            plugins_invoked=["assert_release"],
            params_summary={"approved_by": payload.approved_by, "assertions_passed": True},
            model_version=None,
            structured_output={"status": "released"},
            validation_result="pass",
            failure_reason=None,
            human_action=None,
            started_at=datetime.now(UTC),
            finished_at=None,
        )
        session.commit()
        return ReleaseResponse(
            passed=True,
            batch_id=batch_id,
            status="released",
            release_decision="released",
            checks=[
                {"name": c.name, "passed": c.passed, "reason": c.reason} for c in result.checks
            ],
        )

    # Any fail: no state transition, no release_decision write. Record the
    # retriable assertion-failure trace, commit only that read-only marker.
    TraceStore().record(
        session,
        task_id=str(uuid.uuid4()),
        batch_id=batch_id,
        skill_name="release",
        input_ref={"kind": "batch", "id": batch.batch_no},
        plugins_invoked=["assert_release"],
        params_summary={"approved_by": payload.approved_by, "assertions_passed": False},
        model_version=None,
        structured_output={
            "status": "pending_release",
            "failed": [c.name for c in result.checks if not c.passed],
        },
        validation_result="fail",
        failure_reason="assertion failed, retriable",
        human_action=None,
        started_at=datetime.now(UTC),
        finished_at=None,
    )
    session.commit()
    return ReleaseResponse(
        passed=False,
        batch_id=batch_id,
        status=batch.status,
        release_decision=batch.release_decision,
        checks=[{"name": c.name, "passed": c.passed, "reason": c.reason} for c in result.checks],
    )


@router.post("/{batch_id}/reject", response_model=RejectResponse)
def reject_batch(
    batch_id: uuid.UUID, payload: RejectRequest, session: SessionDep
) -> RejectResponse:
    """Manual reject (D7 + D6a): record the rejection, generate the incident
    disposal report and auto-archive the batch — all in one transaction.

    D8: an already-archived batch is 409. Any other non-archived batch can be
    rejected; the state-machine ``rejected`` edge then (D6a, empty
    precondition) auto-transitions it to ``archived`` in the same tx.
    """
    batch = session.get(DeliveryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")

    # D8: archived is terminal.
    require_not_archived(batch_id, session)

    # D7: a rejected batch only allows the incident (disposal) report type —
    # the reject action itself is that incident report, so D7 is satisfied by
    # construction.
    res = transition(batch.status, "rejected", {"human_rejected": True}, kind="batch")
    if not res.accepted:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "batch_reject_not_allowed",
                "from_status": batch.status,
                "reason": res.reason,
            },
        )
    batch.status = "rejected"

    # D7: generate the incident disposal report in the SAME transaction.
    skill = ReportSkill()
    report = skill.generate(
        session,
        batch_id,
        "incident",
        generated_at=datetime.now(UTC),
    )

    # D6a: auto-archive the now-rejected batch in the SAME transaction.
    auto_archived = auto_archive_if_terminal(session, batch)

    # D1: the three-layer trace (trace row + the report's evidence + report).
    TraceStore().record(
        session,
        task_id=str(uuid.uuid4()),
        batch_id=batch_id,
        skill_name="reject",
        input_ref={"kind": "batch", "id": batch.batch_no},
        plugins_invoked=["report_skill", "auto_archive"],
        params_summary={
            "reason": payload.reason,
            "rejected_by": payload.rejected_by,
            "auto_archived": auto_archived,
        },
        model_version=None,
        structured_output={"status": batch.status, "report_id": str(report.report_id)},
        validation_result="pass",
        failure_reason=None,
        human_action="rejected",
        started_at=datetime.now(UTC),
        finished_at=None,
    )
    session.commit()
    session.refresh(report)
    return RejectResponse(
        batch_id=batch_id,
        status=batch.status,
        auto_archived=auto_archived,
        reason=payload.reason,
        report_id=report.report_id,
    )
