"""§8.1 异常事件自动创建（P0 缺口 #2，设计文档 §9.8）。

修复前的行为：判定 Fail、规则缺失、跨批次 SN 冲突都**不会**创建
``QualityIncident``——§8.1 的触发要求全靠人工补建。本文件钉住修复后的语义：

- 判定 Fail（规则_fail）→ 批次 isolated 时同事务自动创建 ``rule_fail``
  异常（status=new，sn_links 指向失败组件，证据引用判定快照）；
- 规则缺失 → ``rule_missing`` 异常（affected = 批次全部 SN）；
- 跨批次 SN 冲突 → 400 照常返回，同时在被占用批次自动创建
  ``data_consistency`` 异常（severity=critical，不产生孤儿异常）；
- 幂等：同批次同触发原因存在未关闭异常时不重复创建。

级别说明：前两个用例跑在 workflow 层（batch_transition 节点所在层，
复用 test_main_chain 的 SQLite + stub 环境）；冲突用例走 API 层（路由
的冲突翻译分支是被测对象）。sample_fail 触发的映射在节点内与 rule_fail
同构，由 ensure_incident 单元断言覆盖（见 Ruling：抽样 Fail 与判定 Fail
在当前确定性抽样下不可能独立出现——抽样判的就是同一批实测值）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from app.plugins.auto_incident import ensure_incident
from tests.workflow.test_main_chain import (
    _deps,
    _initial_inputs,
    _make_env,
    _run_to_completion,
)

_ISO_TS = "2026-10-01T00:00:00+00:00"


def _run_judge_fail() -> dict:
    """一支判定 Fail 的完整主链（一个 SN 功率越下限）。"""
    sns = [f"SN-G-{i:02d}" for i in range(10)]
    env = _make_env(confidence=0.9, sns=sns, pmax_by_sn={sns[0]: 500.0})
    from langgraph.checkpoint.memory import InMemorySaver

    from app.workflow.main_chain import build_main_chain_graph

    graph = build_main_chain_graph(_deps(env), checkpointer=InMemorySaver())
    state = _run_to_completion(graph, _initial_inputs(env), thread_id=env["batch_id"])
    env["state"] = state
    return env


def test_judge_fail_auto_creates_rule_fail_incident() -> None:
    env = _run_judge_fail()
    session = env["session"]
    from app.infra.models import IncidentSnLink, ModuleInspection, QualityIncident

    assert env["state"]["batch_status"] == "isolated"
    batch_id = UUID(env["batch_id"])
    assert (
        session.get(
            __import__("app.infra.models", fromlist=["DeliveryBatch"]).DeliveryBatch, batch_id
        ).status
        == "isolated"
    )

    incidents = session.query(QualityIncident).filter(QualityIncident.batch_id == batch_id).all()
    assert len(incidents) == 1, f"应自动创建一只异常：{incidents}"
    inc = incidents[0]
    assert inc.trigger_type == "rule_fail"
    assert inc.status == "new"
    assert inc.evidence_ids, "自动异常应引用判定证据"

    # sn_links 指向失败组件
    failing_sn = env["state"]["decision"]["fail_sns"][0]
    linked = (
        session.query(ModuleInspection.sn)
        .join(IncidentSnLink, IncidentSnLink.inspection_id == ModuleInspection.inspection_id)
        .filter(IncidentSnLink.incident_id == inc.incident_id)
        .all()
    )
    assert [r.sn for r in linked] == [failing_sn]

    # §8.2 组件级隔离：失败 SN 标记 isolated，合格 SN 不受影响（§9.8 #3）
    from app.infra.models import ModuleInspection as _MI

    failing_row = session.query(_MI).filter(_MI.batch_id == batch_id, _MI.sn == failing_sn).one()
    assert failing_row.intake_status == "isolated"
    ok_row = session.query(_MI).filter(_MI.batch_id == batch_id, _MI.sn != failing_sn).first()
    assert ok_row is not None and ok_row.intake_status == "confirmed"

    # 轨迹留痕
    from app.plugins.trace_store import TraceStore

    assert "incident_auto" in {t.skill_name for t in TraceStore().list_for_batch(session, batch_id)}


def test_rule_missing_creates_rule_missing_incident() -> None:
    env = _make_env(confidence=0.9, sns=[f"SN-G-{i:02d}" for i in range(10)], rule_missing=True)
    from langgraph.checkpoint.memory import InMemorySaver

    from app.workflow.main_chain import build_main_chain_graph

    graph = build_main_chain_graph(_deps(env), checkpointer=InMemorySaver())
    state = _run_to_completion(graph, _initial_inputs(env), thread_id=env["batch_id"])
    session = env["session"]
    from app.infra.models import DeliveryBatch, IncidentSnLink, QualityIncident

    assert state["batch_status"] == "isolated"
    batch_id = UUID(env["batch_id"])
    assert session.get(DeliveryBatch, batch_id).status == "isolated"
    inc = session.query(QualityIncident).filter(QualityIncident.batch_id == batch_id).one()
    assert inc.trigger_type == "rule_missing"
    # 规则缺失影响全批：sn_links 覆盖全部 10 个组件
    linked = (
        session.query(IncidentSnLink).filter(IncidentSnLink.incident_id == inc.incident_id).count()
    )
    assert linked == 10


def test_ensure_incident_idempotent() -> None:
    """同批次同触发原因存在未关闭异常时不重复创建（Review Focus #3）。"""
    env = _make_env(confidence=0.9, sns=["SN-G-00"])
    session = env["session"]
    first = ensure_incident(
        session,
        batch_id=env["batch_id"],
        trigger_type="rule_fail",
        severity="major",
        affected_sns=["SN-G-00"],
    )
    second = ensure_incident(
        session,
        batch_id=env["batch_id"],
        trigger_type="rule_fail",
        severity="major",
        affected_sns=["SN-G-00"],
    )
    session.flush()
    assert first is not None and second is not None
    assert first.incident_id == second.incident_id


def test_cross_batch_conflict_creates_consistency_incident() -> None:
    """跨批次 SN 冲突：400 照常返回 + 被占用批次自动创建一致性异常。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.infra.models import (
        Base,
        DeliveryBatch,
        IncidentSnLink,
        ModuleInspection,
        QualityIncident,
        QualityRuleVersion,
    )
    from app.main import create_unified_app
    from tests.fixtures.loader import load_acceptance

    sample = load_acceptance("incident")
    raw = sample["raw"]["rules"]
    ts = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    rule = QualityRuleVersion(
        rule_version_id=UUID("ab000000-3333-2222-3333-444444444401"),
        project_id=raw["project_id"],
        model=raw["model"],
        name="rv-1",
        nameplate_power=raw["nameplate_power"],
        power_tolerance=raw["power_tolerance"],
        parameter_standards=raw["parameter_standards"],
        current_bands=raw["current_bands"],
        sample_plan=raw["sample_plan"],
        effective_at=ts,
        status="published",
        version_no=raw["version_no"],
    )
    batch_a = DeliveryBatch(
        batch_id=UUID("ab000000-3333-2222-3333-444444444402"),
        batch_no="B-OCCUPIED",
        project_id=raw["project_id"],
        model=raw["model"],
        planned_quantity=1,
        actual_quantity=1,
        rule_version_id=rule.rule_version_id,
        status="pending_release",
    )
    insp_a = ModuleInspection(
        inspection_id=UUID("ab000000-3333-2222-3333-444444444403"),
        batch_id=batch_a.batch_id,
        sn="SN-SHARED-1",
        source_type="IV_CSV",
        captured_at=ts,
        pmax=550.0,
        temp=25.0,
        isc=10.5,
        voc=48.0,
        vm=40.0,
        im=13.0,
        ff=0.80,
        appearance_result="pass",
        intake_status="confirmed",
        decision_status="pass",
    )
    session.add_all([rule, batch_a, insp_a])
    session.commit()

    app = create_unified_app(session_factory=lambda: session)
    client = TestClient(app)
    try:
        # 在被占用批次 B-OCCUPIED 上再创建一个新批次，提交同一 SN
        r = client.post(
            "/api/batches",
            json={
                "batch_no": "B-CONFLICT",
                "project_id": raw["project_id"],
                "model": raw["model"],
                "rule_version_id": str(rule.rule_version_id),
                "planned_quantity": 1,
            },
        )
        assert r.status_code == 201, r.text
        batch_b = UUID(r.json()["batch_id"])

        r = client.post(
            "/api/intake",
            json={
                "batch_id": str(batch_b),
                "source_type": "IV_CSV",
                "inspections": [
                    {
                        "sn": "SN-SHARED-1",
                        "source_type": "IV_CSV",
                        "captured_at": _ISO_TS,
                        "pmax": 545.0,
                        "temp": 25.0,
                        "isc": 10.5,
                        "voc": 48.0,
                        "vm": 40.0,
                        "im": 13.0,
                        "ff": 0.80,
                        "appearance_result": "pass",
                    }
                ],
            },
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["reason"] == "cross_batch_sn_conflict"

        # 被占用批次 A 上自动创建 data_consistency 异常（severity=critical）
        incidents = (
            session.query(QualityIncident)
            .filter(QualityIncident.batch_id == batch_a.batch_id)
            .all()
        )
        assert len(incidents) == 1, f"应在被占用批次上自动创建一致性异常：{incidents}"
        assert incidents[0].trigger_type == "data_consistency"
        assert incidents[0].severity == "critical"
        assert incidents[0].status == "new"
        linked = (
            session.query(IncidentSnLink)
            .filter(IncidentSnLink.incident_id == incidents[0].incident_id)
            .count()
        )
        assert linked == 1
        # 新批次 B 不产生孤儿异常
        assert (
            session.query(QualityIncident).filter(QualityIncident.batch_id == batch_b).count() == 0
        )
    finally:
        session.close()
        engine.dispose()
