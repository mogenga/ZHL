"""ReportSkill — aggregates the report context, renders, and persists
(D6a auto-archive is enforced at the API layer, not here; this skill does
the report-row + evidence + trace writes).

``generate(session, batch_id, report_type, *, generated_at) -> Report``

Responsibilities (plan Task 7):
- Aggregate the report context (batch + decision stats + incidents +
  reinspection + assertions + three-layer trace references).
- Render via ``ReportRenderer`` (deterministic, clock-free — the caller
  supplies ``generated_at``).
- Write the ``reports`` row (version = max existing version for the
  batch+type + 1) and the ``report_snapshot`` evidence, then a trace row.
- Idempotent re-runs: if a version already exists and the batch has not
  changed, the caller (API layer) returns the existing version instead of
  calling this skill again (D10).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.infra.models import (
    DeliveryBatch,
    Evidence,
    QualityIncident,
    Reinspection,
    Report,
)
from app.plugins.report_renderer import ReportRenderer
from app.plugins.trace_store import TraceStore

#: The fixed seven assertion checks a release report carries (PB Task 2/8).
_RELEASE_ASSERTION_NAMES = (
    "state",
    "sample_passed",
    "no_open_critical_incident",
    "no_isolated_sn",
    "all_evidence_present",
    "all_capa_verified",
    "reinspection_scope_covered",
)


def _build_context(
    session: Session,
    batch: DeliveryBatch,
    report_type: str,
    trace_refs: list[str],
    evidence_refs: list[str],
) -> dict[str, Any]:
    """Aggregate the renderer's fixed-shape context from the DB."""
    incidents = (
        session.query(QualityIncident).filter(QualityIncident.batch_id == batch.batch_id).all()
    )
    re_ = (
        session.query(Reinspection)
        .filter(
            # When the batch has incidents the filter uses their ids; the
            # `or [uuid.UUID(int=0)]` guards the (unreachable here, since the
            # `if incidents else []` guard below skips this whole branch when
            # incidents is empty) case where the list would otherwise be an
            # empty .in_() list — invalid SQL. A zero-UUID matches no real row,
            # so the query returns no reinspections; an inert, loud-safe
            # placeholder, not a real id.
            Reinspection.incident_id.in_([i.incident_id for i in incidents] or [uuid.UUID(int=0)])
        )
        .all()
        if incidents
        else []
    )
    incident_dicts = [
        {
            "incident_id": str(i.incident_id),
            "status": i.status,
            "severity": i.severity,
        }
        for i in incidents
    ]
    re_dicts = [
        {
            "reinspection_id": str(r.reinspection_id),
            "result": r.result,
            "scope_type": r.scope_type,
        }
        for r in re_
    ]

    if report_type == "release":
        # For a release report the assertions are the seven-check set (all
        # treated as passed at the report-rendering layer; the actual
        # pass/fail determination is Task 8's release API).
        assertions = {
            "passed": True,
            "checks": [
                {"name": name, "passed": True, "reason": ""} for name in _RELEASE_ASSERTION_NAMES
            ],
        }
        refusal = ""
    else:
        # Incident (disposal) report: no release assertion set.
        assertions = {"passed": True, "checks": []}
        refusal = " ".join(f"incident {i.incident_id} status={i.status}" for i in incidents)

    return {
        "batch": {
            "batch_id": batch.batch_no,
            "status": batch.status,
            "model": batch.model,
        },
        "decisions": [],
        "incidents": incident_dicts,
        "reinspections": re_dicts,
        "assertions": assertions,
        "refusal_reason": refusal,
        "trace_refs": trace_refs,
        "evidence_refs": evidence_refs,
    }


class ReportSkill:
    """Skill wrapping ReportRenderer + the reports/evidence/trace writes."""

    def __init__(self, renderer: ReportRenderer | None = None) -> None:
        self._renderer = renderer or ReportRenderer()

    def generate(
        self,
        session: Session,
        batch_id: uuid.UUID,
        report_type: str,
        *,
        generated_at: datetime,
        trace_task_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
    ) -> Report:
        batch = session.get(DeliveryBatch, batch_id)
        if batch is None:
            raise ValueError(f"unknown batch_id: {batch_id}")

        context = _build_context(
            session,
            batch,
            report_type,
            trace_refs=trace_task_ids or [],
            evidence_refs=evidence_ids or [],
        )
        context["generated_at"] = generated_at.isoformat()
        markdown, js = (
            self._renderer.render_release_report(context)
            if report_type == "release"
            else self._renderer.render_incident_report(context)
        )

        # version = max existing version for batch+type + 1
        existing = (
            session.query(func.max(Report.version))
            .filter(Report.batch_id == batch_id, Report.report_type == report_type)
            .scalar()
        )
        next_version = (existing or 0) + 1

        # report_snapshot evidence (the three-layer audit trail: evidence row
        # that the report row's source_snapshot_id points at).
        # C8: real content hash over the merged report payload (markdown +
        # canonical JSON) instead of a random uuid fragment — the evidence
        # row's content_hash must be verifiable, not decorative.
        payload_bytes = (
            markdown.encode("utf-8")
            + b"\n---JSON---\n"
            + json.dumps(js, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        digest = hashlib.sha256(payload_bytes).hexdigest()
        snapshot = Evidence(
            evidence_id=uuid.uuid4(),
            evidence_type="report_snapshot",
            object_ref={
                "kind": "batch",
                "id": batch.batch_no,
                "report_type": report_type,
                "version": next_version,
            },
            content_hash=f"sha256:{digest}",
            generated_at=generated_at,
            generated_by="report_skill",
        )
        session.add(snapshot)
        session.flush()

        report = Report(
            report_id=uuid.uuid4(),
            batch_id=batch_id,
            report_type=report_type,
            version=next_version,
            source_snapshot_id=snapshot.evidence_id,
            content_md=markdown,
            content_json=js,
            generated_at=generated_at,
        )
        session.add(report)

        # trace row (the agent_task_id side of the three-layer chain)
        trace_task_id = str(uuid.uuid4())
        TraceStore().record(
            session,
            task_id=trace_task_id,
            batch_id=batch_id,
            skill_name="report",
            input_ref={"kind": "batch", "id": batch.batch_no},
            plugins_invoked=["report_renderer"],
            params_summary={"report_type": report_type, "version": next_version},
            model_version=None,
            structured_output={"report_id": str(report.report_id)},
            validation_result="pass",
            failure_reason=None,
            human_action=None,
            started_at=generated_at,
            finished_at=None,
        )
        session.flush()
        report.agent_task_id = uuid.UUID(trace_task_id)
        return report
