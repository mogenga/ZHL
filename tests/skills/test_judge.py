"""Judge Skill tests (PA plan Task 6).

SQLite in-memory + P0 frozen models + ``rules_seed.json`` JH-550W seed.
The fixture ``acceptance_normal.json`` supplies 10 all-pass inspection
records; one deliberate pmax out-of-band (Review Focus #2 input side)
drives the single-fail case.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infra.models import Base, DeliveryBatch, ModuleInspection, QualityRuleVersion
from app.infra.repositories import InspectionRepository
from app.plugins.evidence_store import FileEvidenceStore
from app.skills.judge import UnconfirmedSnsError, judge_batch

_ROOT = Path(__file__).resolve().parents[2]
_SEED = json.loads((_ROOT / "tests" / "fixtures" / "rules_seed.json").read_text(encoding="utf-8"))[
    "rules"
][0]
_NORMAL = json.loads(
    (_ROOT / "tests" / "fixtures" / "acceptance_normal.json").read_text(encoding="utf-8")
)


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


def _insert_inspections(session, batch_id, rows, intake_status: str = "confirmed") -> None:
    """Deep-copies ``rows`` so callers never mutate the shared ``_NORMAL`` fixture."""
    ins_rows = [
        {
            "sn": r["sn"],
            "source_type": "IV_CSV",
            "captured_at": datetime(2026, 10, 1, tzinfo=UTC),
            "pmax": r["pmax"],
            "temp": r["temp"],
            "isc": r["isc"],
            "voc": r["voc"],
            "vm": r["vm"],
            "im": r["im"],
            "ff": r["ff"],
            "appearance_result": r["appearance_result"],
        }
        for r in rows
    ]
    repo = InspectionRepository()
    created = repo.create_inspections(session, batch_id, ins_rows)
    for item in created:
        item.intake_status = intake_status
    session.flush()


def test_judge_batch_all_pass(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
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
    batch = DeliveryBatch(
        batch_no="B-J-001",
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        planned_quantity=10,
        rule_version_id=rule.rule_version_id,
    )
    s.add(batch)
    s.flush()
    _insert_inspections(s, batch.batch_id, _NORMAL["inspections"], intake_status="confirmed")

    evidence = FileEvidenceStore(root=tmp_path, db=None)
    result = judge_batch(s, evidence, batch.batch_id, rule.rule_version_id)
    s.commit()
    s2 = sessionmaker(bind=engine)()
    rows = s2.scalars(
        select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
    ).all()
    assert len(rows) == 10
    assert result["result"] == "Pass"
    assert result["counts"] == {"Pass": 10, "Fail": 0}
    assert result["fail_sns"] == []
    s.close()
    s2.close()
    engine.dispose()


def test_judge_batch_one_fail(session, tmp_path: Path) -> None:
    """Review Focus #2: one SN with out-of-band pmax -> batch result is Fail.

    Uses its own SQLite engine (independent of the shared ``session`` fixture)
    because the cross-batch SN EXCLUDE fallback (Task 2 / Review Focus #4)
    would reject re-inserting the same SNs from ``_NORMAL`` into a second
    batch on the same shared engine — each judge test runs on its own
    in-memory DB to keep the SN sets isolated.
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
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
    batch = DeliveryBatch(
        batch_no="B-J-002",
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        planned_quantity=2,
        rule_version_id=rule.rule_version_id,
    )
    s.add(batch)
    s.flush()
    rows = deepcopy(list(_NORMAL["inspections"][:2]))
    rows[0]["pmax"] = 600.0  # clearly out of band (550 * 0.97=533.5..566.5)
    _insert_inspections(s, batch.batch_id, rows, intake_status="confirmed")

    evidence = FileEvidenceStore(root=tmp_path, db=None)
    result = judge_batch(s, evidence, batch.batch_id, rule.rule_version_id)
    s.flush()
    assert result["result"] == "Fail"
    assert result["fail_sns"] == [rows[0]["sn"]]
    assert result["counts"]["Fail"] == 1
    s.close()
    engine.dispose()


def test_judge_batch_unconfirmed_raises(tmp_path: Path) -> None:
    """Review Focus #3 (Skill side): at least one inspection still pending.

    Own SQLite engine (same cross-batch SN isolation reason as above).
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
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
    batch = DeliveryBatch(
        batch_no="B-J-003",
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        planned_quantity=2,
        rule_version_id=rule.rule_version_id,
    )
    s.add(batch)
    s.flush()
    rows = deepcopy(list(_NORMAL["inspections"][:2]))
    _insert_inspections(s, batch.batch_id, rows, intake_status="confirmed")
    # force one back to pending
    pending_sn = rows[0]["sn"]
    s.query(ModuleInspection).filter(
        ModuleInspection.batch_id == batch.batch_id,
        ModuleInspection.sn == pending_sn,
    ).first().intake_status = "pending"
    s.flush()

    evidence = FileEvidenceStore(root=tmp_path, db=None)
    with pytest.raises(UnconfirmedSnsError) as exc_info:
        judge_batch(s, evidence, batch.batch_id, rule.rule_version_id)
    assert exc_info.value.unconfirmed == [pending_sn]
    s.close()
    engine.dispose()


def test_judge_writes_decision_snapshot_evidence(session, tmp_path: Path) -> None:
    rule = session.get(
        QualityRuleVersion, session.scalars(select(QualityRuleVersion.rule_version_id)).first()
    )
    batch = DeliveryBatch(
        batch_no="B-J-004",
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        planned_quantity=10,
        rule_version_id=rule.rule_version_id,
    )
    session.add(batch)
    session.flush()
    _insert_inspections(session, batch.batch_id, _NORMAL["inspections"], intake_status="confirmed")

    evidence = FileEvidenceStore(root=tmp_path, db=None)
    judge_batch(session, evidence, batch.batch_id, rule.rule_version_id)
    session.flush()
    refs = evidence.list_by_object({"kind": "batch", "id": str(batch.batch_id)})
    assert any(r.evidence_type == "decision_snapshot" for r in refs)


def test_judge_persists_decision_status_and_evidence(tmp_path: Path) -> None:
    """The per-inspection decision_status + decision_evidence / rule_snapshot
    columns are written back to module_inspections within the same tx.

    StaticPool => one shared connection => commit + re-read on a second
    session of the same engine sees the persisted rows (a plain sqlite://
    pool hands out a fresh in-memory DB per connection, masking the writes).
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
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
    batch = DeliveryBatch(
        batch_no="B-J-006",
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        planned_quantity=10,
        rule_version_id=rule.rule_version_id,
    )
    s.add(batch)
    s.flush()
    _insert_inspections(s, batch.batch_id, _NORMAL["inspections"], intake_status="confirmed")

    evidence = FileEvidenceStore(root=tmp_path, db=None)
    result = judge_batch(s, evidence, batch.batch_id, rule.rule_version_id)
    s.commit()

    s2 = sessionmaker(bind=engine)()
    rows = s2.scalars(
        select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
    ).all()
    assert len(rows) == 10
    assert all(r.decision_status == "pass" for r in rows)
    assert all(r.decision_evidence is not None for r in rows)
    assert all(r.rule_snapshot is not None for r in rows)
    assert result["per_sn"]
    s.close()
    s2.close()
    engine.dispose()
    engine.dispose()
