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
from app.domain.types import IsoResult, IsoRow
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
    # C6: pending is not a terminal state. When any sampled SN's judge
    # verdict is still pending, the sample conclusion must NOT be written
    # as pass (it would silently release an undetermined batch). The DDL
    # ENUM (pass/fail/rule_missing) cannot store 'pending', so
    # _sample_result returns the sentinel 'pending' and the caller skips
    # the conclusion insert; a later re-run persists it once settled.
    # 'fail' / 'rule_missing' remain terminal failures.
    statuses = [row.decision_status for row in rows]
    if any(s in ("fail", "rule_missing") for s in statuses):
        return "fail"
    if any(s == "pending" for s in statuses):
        return "pending"  # sentinel: caller skips the conclusion write
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
    sample_round: int = 1,
) -> dict:
    """Run one sampling round for ``batch_id`` (A6 / Review Focus #5).

    Missing preset combo -> ``{"result": "rule_missing"}`` with NO sample
    rows written (the anomaly is surfaced, never defaulted away).
    Hit -> deterministic freeze (domain ``freeze_sample``), rows persisted,
    conclusion recorded, and the plan parameters returned.

    ``sample_round`` names which round this is (the schema's
    ``uq_conclusion_unique`` / ``uq_sample_unique`` both carry it, and the
    release assertion reads the *highest* round). Round 1 is the intake
    sampling; a later round is a re-sample after remediation. The freeze +
    idempotency checks below are scoped to the round, so a genuine second
    round is not silently answered with the first round's verdict.
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

    # C5: reconstruct the matching IsoRow with the same predicate
    # iso_lookup uses (type + level + aql + lot interval) so the stored
    # sample row stays consistent with the domain pure function. A hit
    # from iso_lookup guarantees some row matched; if the exact lot
    # interval row is missing, fall back to the same type/level/aql row.
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
    if matching_row is None:
        matching_row = next(
            (
                r
                for r in iso_table
                if r.inspection_type == inspection_type
                and r.inspection_level == inspection_level
                and abs(r.aql - aql) < 1e-9
            ),
            None,
        )
    # C4: iso_lookup returning a hit guarantees a matching table row, so
    # matching_row cannot be None here. Assert defensively instead of
    # the original unreachable raise.
    assert matching_row is not None, "iso_lookup matched but no table row carries the combo"

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
        sample_round=sample_round,
    )
    selected_sns = [s.sn for s in samples]
    # C4: idempotent re-freeze (same seed + same combo) -> all selected SNs
    # were already persisted for this round, so no new Sample rows came
    # back. Delegate to _idempotent_refreeze: when a conclusion already
    # exists **for this round** it returns the stored result as a
    # short-circuit dict, so we skip re-running the sample-level verdict;
    # otherwise it returns None and we fall through to a fresh verdict below.
    replayed = _idempotent_refreeze(
        session, batch_id, inspection_type, iso_result, sns, seed, sample_round
    )
    if replayed is not None:
        return replayed
    if not samples:
        # No new rows and no prior conclusion: recompute the deterministic
        # selection so the reported SNs stay stable across replays.
        entries = freeze_sample(str(batch_id), len(sns), iso_result, sns, seed)
        selected_sns = [e.sn for e in entries]
    result = _sample_result(session, batch_id, selected_sns)
    # C6: 'pending' sentinel -> no conclusion row is written yet (the
    # DDL ENUM has no 'pending'); a later re-run persists it once the
    # judge verdict has settled. All other results persist as before.
    if result != "pending":
        SampleRepository().save_conclusion(
            session,
            batch_id,
            inspection_type,
            batch_rule_version_id,
            sample_round=sample_round,
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


def _idempotent_refreeze(
    session: Session,
    batch_id: uuid.UUID,
    inspection_type: str,
    iso_result: IsoResult,
    sns: list[str],
    seed: str,
    sample_round: int,
) -> dict | None:
    """C4 helper for the idempotent re-freeze branch of run_sampling.

    When a prior round already froze every selected SN (freeze_samples
    returns no new rows) and a conclusion exists for this
    batch/type/rule/round, return that stored conclusion as a short-circuit
    result dict so the caller does not re-run the sample-level verdict.
    Returns None when the caller must still compute a fresh verdict.

    The lookup is pinned to ``sample_round``: "some round has a conclusion"
    is not the question — answering it that way hands round 2 the round-1
    verdict (the stale ``fail`` that used to block release forever).
    """
    existing = SampleRepository().get_latest_conclusion(
        session, batch_id, inspection_type, sample_round=sample_round
    )
    if existing is None:
        return None
    entries = freeze_sample(str(batch_id), len(sns), iso_result, sns, seed)
    return {
        "result": existing.result,
        "n": iso_result.n,
        "ac": iso_result.ac,
        "re": iso_result.re,
        "selected": [e.sn for e in entries],
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
