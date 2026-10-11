"""Sampling Plugin: ISO lookup + sample freezing + conclusion (PA plan Task 6).

``run_sampling`` is the orchestration twin of the P0-frozen pure functions
(``iso_lookup`` / ``freeze_sample``): resolve the preset ISO 2859-1 combo,
gate the rule-missing anomaly (D9: no silent default release), freeze the
deterministic selection on the caller's session, and record the sample
conclusion. The plugin does not run any per-SN rule judgment — the
conclusion's ``result`` reflects the sample-level plan outcome
(``pass`` when every sampled SN passed the judge's batch verdict already
written by Task 6's judge Skill, else ``fail``).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.domain.sampling import freeze_sample, iso_lookup
from app.domain.types import IsoRow
from app.infra.repositories import SampleRepository

if TYPE_CHECKING:
    from sqlalchemy import Session


def _sample_result(session: Session, batch_id: uuid.UUID, selected_sns: list[str]) -> str:
    """Aggregate the judge verdicts already persisted on the inspected SNs.

    The judge Skill (Task 6) writes ``decision_status`` per inspection row
    in the same transaction family; a sampled SN still ``pending``/``fail``
    keeps the sample conclusion from being a silent ``pass``. No sampled SN
    failed or is pending -> ``pass``.
    """
    from sqlalchemy import select

    from app.infra.models import ModuleInspection

    rows = session.scalars(
        select(ModuleInspection).where(
            ModuleInspection.batch_id == batch_id,
            ModuleInspection.sn.in_(selected_sns),
        )
    ).all()
    for row in rows:
        if row.decision_status in ("fail", "pending", "rule_missing"):
            return "fail"
    return "pass"


def run_sampling(
    session: Session,
    iso_table: list[IsoRow],
    batch_id: uuid.UUID,
    inspection_type: str,
    aql: float,
    lot_size: int,
    sns: list[str],
    seed: str,
    inspection_level: str = "normal",
) -> dict:
    """Run one sampling round for ``batch_id`` (A6 / Review Focus #5).

    Missing preset combo -> ``{"result": "rule_missing"}`` with NO sample
    rows written (the anomaly is surfaced, never defaulted away).
    Hit -> deterministic freeze (domain ``freeze_sample``), rows persisted,
    conclusion recorded, and the plan parameters returned.
    """
    iso_result = iso_lookup(
        inspection_type,
        inspection_level,
        aql,
        lot_size,
        iso_table,
    )
    if iso_result is None:
        return {"result": "rule_missing", "n": 0, "ac": 0, "re": 0, "selected": []}

    # The repository's freeze_samples needs an IsoRow; reconstruct the
    # matching table entry to carry lot bounds (used for the stored row).
    matching_row = next(
        (
            r
            for r in iso_table
            if r.inspection_type == inspection_type
            and r.inspection_level == inspection_level
            and abs(r.aql - aql) < 1e-9
            and r.lot_size_min <= lot_size <= r.lot_size_max
        ),
        None,
    )
    if matching_row is None:  # pragma: no cover - iso_lookup already matched
        raise LookupError("iso_lookup matched but no table row carries the combo")

    batch_rule_version_id = _batch_rule_version_id(session, batch_id)
    selected_at = _utcnow()
    samples = SampleRepository().freeze_samples(
        session,
        batch_id,
        inspection_type,
        batch_rule_version_id,
        sns,
        seed,
        selected_at,
        matching_row,
    )
    selected_sns = [s.sn for s in samples]
    # Idempotent re-freeze (same seed + same combo): all selected SNs already
    # persisted in a previous round -> no new Sample rows came back. Recompute
    # the deterministic selection from the domain function (same input, same
    # output) so the reported selection is stable across re-freezes, and skip
    # the conclusion insert when one already exists (uq_conclusion_unique:
    # batch/type/rule/round) instead of re-running the sample-level verdict.
    if not samples:
        entries = freeze_sample(str(batch_id), len(sns), iso_result, sns, seed)
        selected_sns = [e.sn for e in entries]
        existing = SampleRepository().get_latest_conclusion(session, batch_id, inspection_type)
        if existing is not None:
            return {
                "result": existing.result,
                "n": iso_result.n,
                "ac": iso_result.ac,
                "re": iso_result.re,
                "selected": selected_sns,
            }
    result = _sample_result(session, batch_id, selected_sns)
    SampleRepository().save_conclusion(
        session,
        batch_id,
        inspection_type,
        batch_rule_version_id,
        sample_round=1,
        n=iso_result.n,
        ac=iso_result.ac,
        re=iso_result.re,
        result=result,
    )
    return {
        "result": result,
        "n": iso_result.n,
        "ac": iso_result.ac,
        "re": iso_result.re,
        "selected": selected_sns,
    }


def _batch_rule_version_id(session: Session, batch_id: uuid.UUID) -> uuid.UUID:
    from sqlalchemy import select

    from app.infra.models import DeliveryBatch

    row = session.scalars(
        select(DeliveryBatch.rule_version_id).where(DeliveryBatch.batch_id == batch_id)
    ).first()
    if row is None:
        raise LookupError(f"unknown batch_id: {batch_id}")
    return row


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)
