"""RuleVersionEvent model + migration contract."""

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.infra.models import RULE_VERSION_ACTION, Base, QualityRuleVersion, RuleVersionEvent


def test_rule_version_action_values() -> None:
    assert RULE_VERSION_ACTION == ("created", "published", "disabled")


def test_rule_version_event_roundtrip_sqlite() -> None:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    sm = sessionmaker(bind=eng, future=True)
    s = sm()
    rule = QualityRuleVersion(
        project_id="P1",
        model="JH-550W",
        name="v1",
        nameplate_power=550.0,
        power_tolerance=0.03,
        parameter_standards={},
        current_bands={},
        sample_plan={},
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="draft",
        version_no=1,
    )
    s.add(rule)
    s.flush()
    ev = RuleVersionEvent(
        rule_version_id=rule.rule_version_id,
        action="created",
        operator="qa",
        detail={"version_no": 1},
    )
    s.add(ev)
    s.commit()
    got = s.query(RuleVersionEvent).one()
    assert got.action == "created"
    assert got.operator == "qa"
    assert got.detail == {"version_no": 1}
