"""Sampling Plugin tests (PA plan Task 6).

Covers ``run_sampling``: the ``iso_lookup``-hit path freezes samples +
writes the conclusion; the missing-combo path (Review Focus #5 Plugin
side) returns ``rule_missing`` and writes NO sample rows — no silent
default release.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.domain.types import IsoRow
from app.infra.models import (
    Base,
    DeliveryBatch,
    ModuleInspection,
    QualityRuleVersion,
    Sample,
    SampleConclusion,
)
from app.infra.repositories import InspectionRepository
from app.plugins.sampling import run_sampling

_ROOT = Path(__file__).resolve().parents[2]
_SEED = json.loads((_ROOT / "tests" / "fixtures" / "rules_seed.json").read_text(encoding="utf-8"))[
    "rules"
][0]
_ISO_TABLE = [
    IsoRow(**row)
    for row in json.loads(
        (_ROOT / "tests" / "fixtures" / "iso2859_1_aql.json").read_text(encoding="utf-8")
    )["rows"]
]


@pytest.fixture()
def session() -> sessionmaker:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    rule = QualityRuleVersion(
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        name="JH-550W default",
        nameplate_power=_SEED["nameplate_power"],
        power_tolerance=_SEED["power_tolerance"],
        parameter_standards=_SEED["parameter_standards"],
        current_bands=_SEED["current_bands"],
        sample_plan=_SEED["sample_plan"],
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="published",
        version_no=_SEED["version_no"],
    )
    s.add(rule)
    s.flush()
    s.commit()
    yield s
    s.close()
    engine.dispose()


def _batch_with_sns(session, batch_no: str, sns: list[str]) -> DeliveryBatch:
    rule = session.scalars(select(QualityRuleVersion)).first()
    batch = DeliveryBatch(
        batch_no=batch_no,
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        planned_quantity=len(sns),
        rule_version_id=rule.rule_version_id,
    )
    session.add(batch)
    session.flush()
    ins_rows = [
        {
            "sn": sn,
            "source_type": "IV_CSV",
            "captured_at": datetime(2026, 10, 1, tzinfo=UTC),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 48.0,
            "vm": 40.0,
            "im": 13.0,
            "ff": 0.80,
            "appearance_result": "pass",
        }
        for sn in sns
    ]
    InspectionRepository().create_inspections(session, batch.batch_id, ins_rows)
    for row in session.scalars(
        select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
    ).all():
        row.intake_status = "confirmed"
    session.flush()
    return batch


def test_sampling_hit_freezes_samples(session) -> None:
    batch = _batch_with_sns(session, "B-S-001", [f"SN-{i:03d}" for i in range(10)])
    result = run_sampling(
        session,
        _ISO_TABLE,
        batch.batch_id,
        inspection_type="iv",
        aql=1.0,
        lot_size=10,
        sns=[f"SN-{i:03d}" for i in range(10)],
        seed=str(batch.batch_id),
    )
    session.flush()
    # AQL 1.0, lot 10 (in [9,15]) -> table row n=13, ac=0, re=1.
    # freeze_sample caps the *selection* to min(n, available)=10, but the
    # plan parameters reported are the table row's n/ac/re.
    assert result["n"] == 13
    assert result["ac"] == 0
    assert result["re"] == 1
    # C6: the seeded inspections never ran the judge, so every sampled SN's
    # decision_status is still pending. The sample-level result is the
    # 'pending' sentinel -- NOT written as pass/fail (no silent release of an
    # undetermined batch), and no conclusion row is persisted yet.
    assert result["result"] == "pending"
    samples = session.scalars(select(Sample)).all()
    assert len(samples) == 10  # capped to available SNs
    assert len(result["selected"]) == 10
    conclusions = session.scalars(select(SampleConclusion)).all()
    assert len(conclusions) == 0  # C6: pending -> conclusion write skipped


def test_sampling_settled_verdicts_write_conclusion(session) -> None:
    """C6: once the judge verdicts are settled, the conclusion IS written.
    All pass -> 'pass'."""
    batch = _batch_with_sns(session, "B-S-003", [f"SN-{i:03d}" for i in range(10)])
    sns = [f"SN-{i:03d}" for i in range(10)]
    rows = session.scalars(
        select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
    ).all()
    for row in rows:
        row.decision_status = "pass"
    session.flush()
    result = run_sampling(
        session,
        _ISO_TABLE,
        batch.batch_id,
        inspection_type="iv",
        aql=1.0,
        lot_size=10,
        sns=sns,
        seed=str(batch.batch_id),
    )
    session.flush()
    assert result["result"] == "pass"
    conclusions = session.scalars(select(SampleConclusion)).all()
    assert len(conclusions) == 1
    assert conclusions[0].result == "pass"


def test_sampling_single_fail_verdict_writes_fail_conclusion(session) -> None:
    """C6: one fail verdict -> sample conclusion 'fail' (terminal, persisted)."""
    batch = _batch_with_sns(session, "B-S-004", [f"SN-{i:03d}" for i in range(10)])
    sns = [f"SN-{i:03d}" for i in range(10)]
    rows = session.scalars(
        select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
    ).all()
    for i, row in enumerate(rows):
        row.decision_status = "fail" if i == 0 else "pass"
    session.flush()
    result = run_sampling(
        session,
        _ISO_TABLE,
        batch.batch_id,
        inspection_type="iv",
        aql=1.0,
        lot_size=10,
        sns=sns,
        seed=str(batch.batch_id),
    )
    session.flush()
    assert result["result"] == "fail"
    conclusions = session.scalars(select(SampleConclusion)).all()
    assert len(conclusions) == 1
    assert conclusions[0].result == "fail"


def test_sampling_missing_combo_rule_missing(session) -> None:
    """Review Focus #5 (Plugin side): combo absent from the ISO table.

    aql 9.9 is not a preset row -> iso_lookup returns None -> the plugin
    must gate on rule_missing and NOT write any sample row.
    """
    batch = _batch_with_sns(session, "B-S-002", [f"SN-{i:03d}" for i in range(5)])
    sns = [f"SN-{i:03d}" for i in range(5)]
    result = run_sampling(
        session,
        _ISO_TABLE,
        batch.batch_id,
        inspection_type="iv",
        aql=9.9,  # not in the preset table
        lot_size=5,
        sns=sns,
        seed=str(batch.batch_id),
    )
    session.flush()
    assert result["result"] == "rule_missing"
    samples = session.scalars(select(Sample)).all()
    assert len(samples) == 0
    conclusions = session.scalars(select(SampleConclusion)).all()
    assert len(conclusions) == 0


def test_sampling_deterministic_same_seed(session) -> None:
    sns = [f"SN-{i:03d}" for i in range(10)]
    batch1 = _batch_with_sns(session, "B-S-003", sns)
    run_sampling(
        session, _ISO_TABLE, batch1.batch_id, "iv", 1.0, 10, sns, seed=str(batch1.batch_id)
    )
    session.flush()
    sns2 = [f"SN-1{idx:02d}" for idx in range(10)]
    batch2 = _batch_with_sns(session, "B-S-004", sns2)
    seed = "fixed-seed"
    r2 = run_sampling(session, _ISO_TABLE, batch2.batch_id, "iv", 1.0, 10, sns2, seed=seed)
    session.flush()
    r2b = run_sampling(session, _ISO_TABLE, batch2.batch_id, "iv", 1.0, 10, sns2, seed=seed)
    session.flush()
    # Re-freezing with the same seed + same sns must not create duplicate rows
    # (the UNIQUE constraint on uq_sample_unique is the backstop).
    b2_samples = session.scalars(select(Sample).where(Sample.batch_id == batch2.batch_id)).all()
    assert len(b2_samples) == 10
    assert r2["selected"] == r2b["selected"]


def test_sampling_second_round_writes_its_own_conclusion(session) -> None:
    """复检后的第 2 轮抽样必须落自己的结论行（轮次参数）。

    缺陷：``run_sampling`` 把 ``sample_round`` 写死为 1，且 ``_idempotent_refreeze``
    一旦查到**任何**结论就直接返回旧结果 —— 于是第 1 轮 fail 之后，无论重跑多少
    次都只会拿到那条 fail、且不写任何新行，批次永久卡在放行断言②。
    """
    sns = [f"SN-{i:03d}" for i in range(10)]
    batch = _batch_with_sns(session, "B-S-R2", sns)
    rows = session.scalars(
        select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
    ).all()
    for i, row in enumerate(rows):
        row.decision_status = "fail" if i == 0 else "pass"
    session.flush()
    first = run_sampling(
        session, _ISO_TABLE, batch.batch_id, "iv", 1.0, 10, sns, seed=str(batch.batch_id)
    )
    session.flush()
    assert first["result"] == "fail"

    # 复检整改后重跑同一规则版本：这一轮全体 pass。
    for row in rows:
        row.decision_status = "pass"
    session.flush()
    second = run_sampling(
        session,
        _ISO_TABLE,
        batch.batch_id,
        "iv",
        1.0,
        10,
        sns,
        seed=str(batch.batch_id),
        sample_round=2,
    )
    session.flush()
    assert second["result"] == "pass"
    conclusions = session.scalars(
        select(SampleConclusion).order_by(SampleConclusion.sample_round)
    ).all()
    assert [(c.sample_round, c.result) for c in conclusions] == [(1, "fail"), (2, "pass")]
    # 第 2 轮有自己的冻结样本行（按轮次去重，不再被第 1 轮的 (batch, sn) 挡住）。
    rounds = session.scalars(select(Sample.sample_round)).all()
    assert sorted(rounds) == [1] * 10 + [2] * 10


def test_same_round_rerun_still_short_circuits(session) -> None:
    """同一轮重放仍是幂等的：返回该轮已存结论，不写重复行。"""
    sns = [f"SN-{i:03d}" for i in range(10)]
    batch = _batch_with_sns(session, "B-S-R3", sns)
    for row in session.scalars(
        select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
    ).all():
        row.decision_status = "pass"
    session.flush()
    first = run_sampling(
        session, _ISO_TABLE, batch.batch_id, "iv", 1.0, 10, sns, seed=str(batch.batch_id)
    )
    session.flush()
    replay = run_sampling(
        session, _ISO_TABLE, batch.batch_id, "iv", 1.0, 10, sns, seed=str(batch.batch_id)
    )
    session.flush()
    assert first["result"] == replay["result"] == "pass"
    conclusions = session.scalars(select(SampleConclusion)).all()
    assert len(conclusions) == 1
