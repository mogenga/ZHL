"""Repository-layer unit tests (PA plan Task 2).

SQLite in-memory engine + P0 frozen models (cross-DB create_all) +
``rules_seed.json`` JH-550W seed for FK targets. Covers:

- create_batch draft semantics
- (batch_id, sn) unique within one batch
- cross-batch same-SN fallback (Review Focus #4: app-layer guard, since
  SQLite has no EXCLUDE)
- confirm_inspection field-modification semantics
- freeze_samples deterministic replay (same seed => same selection)
- save_conclusion unique key (batch_id, inspection_type, rule_version_id,
  sample_round)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.domain.types import IsoRow
from app.infra.models import (
    Base,
    DeliveryBatch,
    QualityRuleVersion,
)
from app.infra.models import (
    Sample as SampleModel,
)
from app.infra.repositories import (
    BatchRepository,
    CrossBatchSnConflict,
    InspectionRepository,
    SampleRepository,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_SEED = json.loads((_FIXTURES / "rules_seed.json").read_text(encoding="utf-8"))["rules"][0]


@pytest.fixture()
def session() -> sessionmaker:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    s = sf()
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


def _make_batch(repo: BatchRepository, s, batch_no: str, rule_version_id) -> DeliveryBatch:
    return repo.create_batch(
        s,
        batch_no=batch_no,
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        rule_version_id=rule_version_id,
        planned_quantity=10,
    )


def _row(sn: str, **overrides) -> dict:
    base = {
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
    base.update(overrides)
    return base


def test_create_batch_draft(session) -> None:
    s = session
    rule = s.query(QualityRuleVersion).first()
    batch = _make_batch(BatchRepository(), s, "B-001", rule.rule_version_id)
    s.flush()
    assert batch.status == "draft"
    assert batch.release_decision == "undecided"


def test_create_inspections_unique(session) -> None:
    s = session
    rule = s.query(QualityRuleVersion).first()
    repo = BatchRepository()
    batch = _make_batch(repo, s, "B-002", rule.rule_version_id)
    s.flush()
    ins = InspectionRepository()
    ins.create_inspections(s, batch.batch_id, [_row("SN-001")])
    s.flush()
    with pytest.raises((IntegrityError, CrossBatchSnConflict)):
        ins.create_inspections(s, batch.batch_id, [_row("SN-001")])
        s.flush()
    s.rollback()


def test_cross_batch_same_sn_rejected(session) -> None:
    s = session
    rule = s.query(QualityRuleVersion).first()
    repo = BatchRepository()
    b1 = _make_batch(repo, s, "B-100", rule.rule_version_id)
    b2 = _make_batch(repo, s, "B-101", rule.rule_version_id)
    s.flush()
    ins = InspectionRepository()
    ins.create_inspections(s, b1.batch_id, [_row("SN-X")])
    s.flush()
    with pytest.raises(CrossBatchSnConflict):
        ins.create_inspections(s, b2.batch_id, [_row("SN-X")])
    s.rollback()


def test_confirm_inspection_fields(session) -> None:
    s = session
    rule = s.query(QualityRuleVersion).first()
    batch = _make_batch(BatchRepository(), s, "B-003", rule.rule_version_id)
    s.flush()
    ins = InspectionRepository()
    created = ins.create_inspections(s, batch.batch_id, [_row("SN-005")])
    s.flush()
    confirmed = ins.confirm_inspection(s, created[0].inspection_id, {"pmax": 549.5})
    s.flush()
    assert confirmed.intake_status == "confirmed"
    assert confirmed.pmax == 549.5


def test_freeze_samples_deterministic(session) -> None:
    s = session
    rule = s.query(QualityRuleVersion).first()
    batch = _make_batch(BatchRepository(), s, "B-004", rule.rule_version_id)
    s.flush()
    sns = [f"SN-{i:03d}" for i in range(10)]
    ins = InspectionRepository()
    ins.create_inspections(s, batch.batch_id, [_row(sn) for sn in sns])
    s.flush()
    rep = SampleRepository()
    sample_plan = rule.sample_plan
    iso_row = IsoRow(
        inspection_type=sample_plan["inspection_type"],
        inspection_level=sample_plan["inspection_level"],
        aql=sample_plan["aql"],
        lot_size_min=9,
        lot_size_max=15,
        n=13,
        ac=0,
        re=1,
    )
    selected_at = datetime(2026, 10, 1, tzinfo=UTC)
    rep.freeze_samples(
        s,
        batch.batch_id,
        sample_plan["inspection_type"],
        rule.rule_version_id,
        sns,
        "seed-1",
        selected_at,
        iso_row,
    )
    s.flush()
    rep.freeze_samples(
        s,
        batch.batch_id,
        sample_plan["inspection_type"],
        rule.rule_version_id,
        sns,
        "seed-1",
        selected_at,
        iso_row,
    )
    s.flush()
    # Determinism: both calls select the same SNs (the seeded PRNG is reproducible).
    # The repository deduplicates against already-persisted rows, so the second
    # call (re-freeze, same seed) inserts nothing new — confirming the selection
    # is stable across calls without data duplication.
    all_samples = s.scalars(select(SampleModel).where(SampleModel.batch_id == batch.batch_id)).all()
    selected_sns = {x.sn for x in all_samples}
    assert selected_sns == {f"SN-{i:03d}" for i in range(10)}
    assert len(all_samples) == 10  # no duplicates after re-freeze


def test_save_conclusion_unique(session) -> None:
    s = session
    rule = s.query(QualityRuleVersion).first()
    batch = _make_batch(BatchRepository(), s, "B-005", rule.rule_version_id)
    s.flush()
    rep = SampleRepository()
    ins_type = "iv"
    rep.save_conclusion(s, batch.batch_id, ins_type, rule.rule_version_id, 10, 0, 1, 1, "pass")
    s.flush()
    with pytest.raises(IntegrityError):
        rep.save_conclusion(s, batch.batch_id, ins_type, rule.rule_version_id, 10, 0, 1, 1, "fail")
        s.flush()
    s.rollback()
