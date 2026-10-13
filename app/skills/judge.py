"""Judge Skill: single-transaction batch decision (PA plan Task 6 / A4).

``judge_batch`` reads every CONFIRMED inspection of the batch plus its rule
snapshot, runs the P0-frozen pure ``judge()`` per SN, persists the per-SN
verdict (``decision_status`` + ``decision_evidence`` + ``rule_snapshot``
columns), writes ONE ``decision_snapshot`` evidence record, and returns the
batch-level rollup (B5: per-SN view is a dict, not a flat pass/fail bool).

A single SQLAlchemy ``Session`` is the transaction boundary (AGENT.md §4.3):
all DB writes happen on the caller's session inside one transaction; the
evidence record is written through the injected ``EvidenceStore`` so a
``db=None`` store (tests / demos) stays file-only.

The Skill performs no state-machine work: batch status transitions are the
``batch_transition`` graph node's job (Task 7).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING

from app.domain.judge import judge
from app.domain.types import (
    CurrentBand,
    ModuleInspectionIn,
    ParamStandard,
    RuleSnapshotIn,
)
from app.infra.models import ModuleInspection, QualityRuleVersion
from app.plugins.evidence_store import EvidenceStore

if TYPE_CHECKING:
    from sqlalchemy import Session


class UnconfirmedSnsError(Exception):
    """A4: judging requires every inspection in the batch to be confirmed.

    Raised when at least one SN still has ``intake_status`` other than
    ``confirmed``; carries the offending SN list so the API layer (Task 8)
    can surface it as a 400 with the checklist (Review Focus #3).
    """

    def __init__(self, unconfirmed: list[str]) -> None:
        self.unconfirmed = list(unconfirmed)
        super().__init__(f"{len(self.unconfirmed)} unconfirmed SNs: {self.unconfirmed}")


class RuleNotEffective(Exception):
    """R1: the chosen rule version was not yet effective when the
    inspections happened.

    ``judge_batch`` raises this when ``rule.effective_at`` is later than the
    earliest ``captured_at`` among the batch's confirmed inspections - a rule
    that only takes effect in the future must not grade historical data
    (spec §3.1). Dedicated type so the API layer maps it to a stable 400
    reason distinct from ``unknown rule version``.
    """

    def __init__(self, effective_at, earliest_captured_at) -> None:
        self.effective_at = effective_at
        self.earliest_captured_at = earliest_captured_at
        super().__init__(
            f"rule effective_at {effective_at.isoformat()} is later than "
            f"earliest inspection {earliest_captured_at.isoformat()}"
        )


def _load_rule_snapshot(session: Session, rule_version_id: uuid.UUID) -> RuleSnapshotIn:
    row = session.get(QualityRuleVersion, rule_version_id)
    if row is None:
        raise LookupError(f"unknown rule_version_id: {rule_version_id}")
    parameter_standards = {
        name: ParamStandard(
            std_value=spec["std_value"],
            tolerance=spec["tolerance"],
            enabled=spec.get("enabled", True),
        )
        for name, spec in row.parameter_standards.items()
    }
    current_bands = tuple(
        CurrentBand(
            lower=band["lower"],
            upper=band["upper"],
            grade=band.get("grade", ""),
        )
        for band in row.current_bands
    )
    return RuleSnapshotIn(
        nameplate_power=float(row.nameplate_power),
        power_tolerance=float(row.power_tolerance),
        parameter_standards=parameter_standards,
        current_bands=current_bands,
        sample_plan=dict(row.sample_plan),
    )


def _snapshot_to_storable(snapshot: RuleSnapshotIn) -> dict:
    """JSONB-safe dict view of the rule snapshot (dataclass-free)."""
    return {
        "nameplate_power": snapshot.nameplate_power,
        "power_tolerance": snapshot.power_tolerance,
        "parameter_standards": {
            name: asdict(spec) for name, spec in snapshot.parameter_standards.items()
        },
        "current_bands": [asdict(band) for band in snapshot.current_bands],
        "sample_plan": dict(snapshot.sample_plan),
    }


def _inspection_in(row: ModuleInspection, project: str) -> ModuleInspectionIn:
    captured = row.captured_at
    return ModuleInspectionIn(
        sn=row.sn,
        project=project,
        grade="A",
        captured_date=captured.date().isoformat() if captured else "",
        pmax=float(row.pmax),
        temp=float(row.temp),
        isc=float(row.isc),
        voc=float(row.voc),
        vm=float(row.vm),
        im=float(row.im),
        ff=float(row.ff),
        appearance_result=row.appearance_result,
    )


def judge_batch(
    session: Session,
    evidence: EvidenceStore,
    batch_id: uuid.UUID,
    rule_version_id: uuid.UUID,
) -> dict:
    """Judge every confirmed inspection of ``batch_id``; one transaction.

    Returns the B5 shape::

        {
          "result": "Pass" | "Fail",   # whole batch (any Fail => Fail)
          "counts": {"Pass": n, "Fail": m},
          "fail_sns": [...],
          "per_sn": {sn: {"result": ..., "checks": [...]}},
        }

    Raises ``UnconfirmedSnsError`` when any SN is not confirmed (A4).
    """
    from sqlalchemy import select

    rule = session.get(QualityRuleVersion, rule_version_id)
    if rule is None:
        raise LookupError(f"unknown rule_version_id: {rule_version_id}")
    project = rule.project_id

    all_rows = session.scalars(
        select(ModuleInspection).where(ModuleInspection.batch_id == batch_id)
    ).all()
    confirmed = [r for r in all_rows if r.intake_status == "confirmed"]
    unconfirmed = [r.sn for r in all_rows if r.intake_status != "confirmed"]
    if unconfirmed:
        raise UnconfirmedSnsError(unconfirmed)

    # R1: the rule must have been effective before the earliest inspection.
    # A rule that only takes effect later must not grade historical data -
    # that would be a silent "judgment reproducible" violation (spec §3.1).
    earliest_captured = min((r.captured_at for r in confirmed if r.captured_at), default=None)
    if (
        earliest_captured is not None
        and rule.effective_at is not None
        and rule.effective_at > earliest_captured
    ):
        raise RuleNotEffective(rule.effective_at, earliest_captured)

    rule_snapshot = _load_rule_snapshot(session, rule_version_id)
    per_sn: dict[str, dict] = {}
    fail_sns: list[str] = []
    counts = {"Pass": 0, "Fail": 0}
    for row in confirmed:
        inspection_in = _inspection_in(row, project)
        decision = judge(inspection_in, rule_snapshot, batch_id=str(batch_id))
        per_sn[row.sn] = {
            "result": decision.result,
            "checks": [asdict(c) for c in decision.checks],
        }
        counts[decision.result] = counts.get(decision.result, 0) + 1
        if decision.result == "Fail":
            fail_sns.append(row.sn)

        # Persist the per-SN verdict on the inspection row (same tx).
        row.decision_status = "pass" if decision.result == "Pass" else "fail"
        row.decision_evidence = {c.rule: c.detail for c in decision.checks}
        row.rule_snapshot = _snapshot_to_storable(rule_snapshot)

    result = "Fail" if fail_sns else "Pass"
    payload = {
        "batch_id": str(batch_id),
        "result": result,
        "counts": counts,
        "fail_sns": fail_sns,
        "per_sn": per_sn,
        "rule_version_id": str(rule_version_id),
        "rule_snapshot": _snapshot_to_storable(rule_snapshot),
    }
    evidence.save(
        "decision_snapshot",
        {"kind": "batch", "id": str(batch_id)},
        content=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        meta={"rule_version_id": str(rule_version_id)},
    )
    return {
        "result": result,
        "counts": counts,
        "fail_sns": fail_sns,
        "per_sn": per_sn,
    }
