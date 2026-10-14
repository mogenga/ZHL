"""第 2 轮抽样结论的装配 + 落库（复检闭环的结论侧回写）。

复现的缺陷：抽样 fail 后，复检通过并关闭异常，批次转 ``pending_release``，
但 ``sample_conclusions`` 里永远只有第 1 轮那条 fail —— 放行断言②读"最新
一轮结论"，于是永久阻断放行。本模块负责在闭环成立时补上第 2 轮结论。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infra.models import (
    Base,
    DeliveryBatch,
    IncidentSnLink,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Reinspection,
    Sample,
    SampleConclusion,
)
from app.skills.remediation_conclusion import materialize_remediation_conclusion

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_BATCH = UUID("dddddddd-1111-2222-3333-444444444401")
_RULE = UUID("dddddddd-1111-2222-3333-444444444402")
_INSP_OK = UUID("dddddddd-1111-2222-3333-444444444403")
_INSP_FAIL = UUID("dddddddd-1111-2222-3333-444444444404")
_INC = UUID("dddddddd-1111-2222-3333-444444444405")
_RE = UUID("dddddddd-1111-2222-3333-444444444406")


@pytest.fixture()
def session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _seed(
    session: Any,
    *,
    round_one_result: str = "fail",
    incident_status: str = "closed",
    reinspection_result: str = "pass",
    reinspection_scope: list[str] | None = None,
    fail_sn_linked: bool = True,
    with_round_one: bool = True,
    sample_rows: bool = True,
    batch_status: str = "pending_release",
) -> DeliveryBatch:
    session.add(
        QualityRuleVersion(
            rule_version_id=_RULE,
            project_id="P-1",
            model="JH-550W",
            name="rv-1",
            nameplate_power=550.0,
            power_tolerance=0.03,
            parameter_standards={},
            current_bands={},
            sample_plan={"inspection_type": "iv", "inspection_level": "normal", "aql": 1.0},
            effective_at=_TS,
            version_no=1,
        )
    )
    # 显式落库规则版本：只有两个待插入 mapper 时 SQLAlchemy 的依赖排序会退化，
    # batch 可能先于规则插入而撞 FK（生产路径逐条 flush，不存在该问题）。
    session.flush()
    batch = DeliveryBatch(
        batch_id=_BATCH,
        batch_no="B-REM-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=3,
        rule_version_id=_RULE,
        status=batch_status,
    )
    session.add(batch)
    for iid, sn, verdict in ((_INSP_OK, "SN-A", "pass"), (_INSP_FAIL, "SN-B", "fail")):
        session.add(
            ModuleInspection(
                inspection_id=iid,
                batch_id=_BATCH,
                sn=sn,
                source_type="IV_CSV",
                captured_at=_TS,
                pmax=550.0,
                temp=25.0,
                isc=10.5,
                voc=0.5,
                vm=48.0,
                im=11.0,
                ff=0.75,
                appearance_result="pass",
                intake_status="confirmed",
                decision_status=verdict,
            )
        )
    # 同上：逐层 flush 固定插入顺序，避免 SQLAlchemy 依赖排序退化导致
    # incident_sn_links 先于 quality_incidents / module_inspections 落库。
    session.flush()
    if sample_rows:
        for sn in ("SN-A", "SN-B"):
            session.add(
                Sample(
                    batch_id=_BATCH,
                    inspection_type="iv",
                    rule_version_id=_RULE,
                    sample_round=1,
                    sn=sn,
                    selected_at=_TS,
                    seed=str(_BATCH),
                )
            )
    if with_round_one:
        session.add(
            SampleConclusion(
                batch_id=_BATCH,
                inspection_type="iv",
                rule_version_id=_RULE,
                sample_round=1,
                n=2,
                ac=0,
                re=1,
                result=round_one_result,
            )
        )
    session.add(
        QualityIncident(
            incident_id=_INC,
            batch_id=_BATCH,
            trigger_type="rule_fail",
            severity="major",
            evidence_ids=[],
            status=incident_status,
            owner="ZHL-QA",
            due_at=_TS,
            capa_verified=True,
        )
    )
    session.flush()
    if fail_sn_linked:
        session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP_FAIL))
    session.add(
        Reinspection(
            reinspection_id=_RE,
            incident_id=_INC,
            inspection_id=_INSP_FAIL,
            rule_version_id=_RULE,
            pmax=550.0,
            temp=25.0,
            isc=10.5,
            voc=0.5,
            vm=48.0,
            im=11.0,
            ff=0.75,
            result=reinspection_result,
            executor="ZHL-QA",
            executed_at=_TS,
            scope_type="minimal",
            scope_sns=["SN-A", "SN-B"] if reinspection_scope is None else reinspection_scope,
        )
    )
    session.commit()
    return batch


def _conclusions(session: Any, round_no: int) -> list[SampleConclusion]:
    return list(
        session.scalars(
            select(SampleConclusion).where(
                SampleConclusion.batch_id == _BATCH,
                SampleConclusion.sample_round == round_no,
            )
        ).all()
    )


def test_writes_round_two_pass_after_closed_incident_with_passing_reinspection(
    session: Any,
) -> None:
    batch = _seed(session)
    outcome = materialize_remediation_conclusion(session, batch)
    assert outcome.result == "pass"
    assert outcome.written is True

    row = _conclusions(session, 2)[0]
    assert row.result == "pass"
    assert row.inspection_type == "iv"
    assert row.rule_version_id == _RULE


def test_round_two_carries_round_one_plan_params(session: Any) -> None:
    """FR-4.2：复检重跑**同一规则版本**的同一抽样方案，不得换方案/换版本。"""
    batch = _seed(session)
    materialize_remediation_conclusion(session, batch)
    row = _conclusions(session, 2)[0]
    assert (row.n, row.ac, row.re) == (2, 0, 1)


def test_incomplete_remediation_writes_nothing_and_stays_fail(session: Any) -> None:
    """复检未通过 ⇒ 不得写 pass 结论；第 1 轮 fail 仍是"最新一轮"。"""
    batch = _seed(session, reinspection_result="fail")
    outcome = materialize_remediation_conclusion(session, batch)
    assert outcome.result == "fail"
    assert outcome.written is False
    assert _conclusions(session, 2) == []


def test_reinspection_not_covering_the_failed_sn_does_not_discharge(session: Any) -> None:
    batch = _seed(session, reinspection_scope=["SN-A"])
    outcome = materialize_remediation_conclusion(session, batch)
    assert outcome.result == "fail"
    assert "SN-B" in outcome.reason
    assert _conclusions(session, 2) == []


def test_open_incident_does_not_discharge(session: Any) -> None:
    batch = _seed(session, incident_status="pending_recheck")
    outcome = materialize_remediation_conclusion(session, batch)
    assert outcome.result == "fail"
    assert _conclusions(session, 2) == []


def test_second_call_is_idempotent(session: Any) -> None:
    batch = _seed(session)
    first = materialize_remediation_conclusion(session, batch)
    second = materialize_remediation_conclusion(session, batch)
    assert first.result == second.result == "pass"
    assert second.written is False  # 已存在，不重复写
    assert len(_conclusions(session, 2)) == 1


def test_no_round_one_conclusion_is_a_noop(session: Any) -> None:
    """没有第 1 轮结论 ⇒ 无从复检闭环；交给断言②的"无结论即不通过"。"""
    batch = _seed(session, with_round_one=False)
    outcome = materialize_remediation_conclusion(session, batch)
    assert outcome.written is False
    assert outcome.skipped == "no_sample_conclusion"
    assert _conclusions(session, 2) == []


def test_round_one_pass_is_a_noop(session: Any) -> None:
    batch = _seed(session, round_one_result="pass")
    outcome = materialize_remediation_conclusion(session, batch)
    assert outcome.written is False
    assert outcome.skipped == "already_passed"
    assert _conclusions(session, 2) == []


def test_falls_back_to_batch_sns_when_sample_rows_are_missing(session: Any) -> None:
    """样本行缺失时用批次全量 SN 兜底（超集，更保守），不得因此放过 fail SN。"""
    batch = _seed(session, sample_rows=False)
    outcome = materialize_remediation_conclusion(session, batch)
    assert outcome.result == "pass"
    assert outcome.written is True


def test_unlinked_fail_sn_blocks_even_with_other_incidents_closed(session: Any) -> None:
    """判 fail 的 SN 没进任何异常 ⇒ 闭环没走，不得通过。"""
    batch = _seed(session, fail_sn_linked=False)
    outcome = materialize_remediation_conclusion(session, batch)
    assert outcome.result == "fail"
    assert _conclusions(session, 2) == []
