"""Sampling pure functions (P0 spec §5.2 ``iso_lookup`` / ``freeze_sample``).

``iso_lookup`` resolves a preset ISO 2859-1 combo to its ``(n, ac, re)`` row;
a missing combo returns ``None`` (D9: rule anomaly, no default release).

``freeze_sample`` deterministically selects a sample of size ``n`` from the
batch's SNs using a seeded PRNG (seed = batch_id string), so the same seed +
same SN list always yields the same selection (reproducible, audit-able).

Purity: no DB, no clock, no infra/langgraph imports. The PRNG is seeded from
the caller-supplied ``seed`` (not an internal random source), so outputs are
deterministic — this is the "fixed seed => reproducible" control named by
P0 spec §3.2 / 总 spec §3.2.1.
"""

from __future__ import annotations

import random

from app.domain.types import IsoResult, IsoRow, SampleEntry


def iso_lookup(
    inspection_type: str,
    level: str,
    aql: float,
    lot_size: int,
    table: list[IsoRow],
) -> IsoResult | None:
    """Find the preset row matching ``(type, level, aql)`` whose lot-size
    interval contains ``lot_size``; return ``None`` when no row matches
    (D9: missing combo is a rule anomaly, never a silent default)."""
    for row in table:
        if row.inspection_type != inspection_type:
            continue
        if row.inspection_level != level:
            continue
        if abs(row.aql - aql) > 1e-9:
            continue
        if row.lot_size_min <= lot_size <= row.lot_size_max:
            return IsoResult(n=row.n, ac=row.ac, re=row.re)
    return None


def freeze_sample(
    batch_id: str,
    lot_size: int,
    iso_row: IsoResult,
    sns: list[str],
    seed: str,
) -> list[SampleEntry]:
    """Select a reproducible sample from ``sns`` of size ``iso_row.n``.

    Uses a seeded PRNG (seed = the batch_id string) so identical inputs give
    identical output. When fewer than ``n`` SNs are available, every SN is
    selected (capped at the input size) so the sample never exceeds the lot.
    """
    n = min(iso_row.n, len(sns))
    if n <= 0:
        return []
    rng = random.Random(f"{batch_id}:{seed}")
    sample = sorted(rng.sample(sorted(sns), n))
    return [SampleEntry(sn=sn) for sn in sample]
