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

from app.domain.types import (
    IsoResult,
    IsoRow,
    RemediationConclusion,
    SampleEntry,
    SnRemediationView,
)


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


def derive_remediation_conclusion(
    sample: list[SnRemediationView],
) -> RemediationConclusion:
    """第一轮样本判 fail 的 SN 是否都已由复检闭环解除 → 第 2 轮结论。

    判定一个 SN 被解除（discharged）的条件，三条缺一不可：

    1. 该 SN 至少关联一个异常（判 fail 却没有任何异常 = 闭环流程根本没走）；
    2. 其关联异常**全部**处于终态 ``closed`` —— ``rejected``（复检失败）
       与 ``scrapped`` 都不算解除，它们恰恰是"没修好"的记录；
    3. 存在 ``result='pass'`` 的复检记录覆盖该 SN。

    样本为空是 Fail 而不是空通过：没有样本就没有可判通过的结论，与放行
    断言②「无结论 ⇒ 不通过」同一条 D9 原则（无默默默认放行）。
    """
    if not sample:
        return RemediationConclusion(result="fail", reason="no sampled SN to conclude on")

    undischarged: list[str] = []
    for row in sample:
        if row.judge_result == "pass":
            continue
        reasons: list[str] = []
        if not row.incidents:
            reasons.append("judged fail with no incident linked")
        else:
            not_closed = [i for i in row.incidents if i.status != "closed"]
            if not_closed:
                named = ", ".join(f"{i.incident_id}({i.status})" for i in not_closed)
                reasons.append(f"incident not closed: {named}")
        if not row.reinspection_covered:
            reasons.append("no passing reinspection covering this SN")
        if reasons:
            undischarged.append(f"{row.sn}: {'; '.join(reasons)}")

    if not undischarged:
        return RemediationConclusion(result="pass", reason="")
    return RemediationConclusion(
        result="fail",
        reason="sample round 2 not discharged — " + "; ".join(undischarged),
    )
