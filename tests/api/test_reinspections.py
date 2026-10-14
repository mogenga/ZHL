"""Reinspection scope API tests (PB Task 7, D5/D5a — Review Focus #2).

Pins:
- default minimal: scope_sns server-filled = the incident's full SN set,
  scope_type="minimal"
- expanded without scope_sns -> 400; expanded with a scope_sns that does not
  cover the minimal set -> 400 listing the gap
- after create: independent reinspection row + verification_id written back
  + module_inspections original values unchanged (DB assertion)
- fail result -> incident + batch both transition to rejected (state machine)
- response body carries ``sn`` resolved from module_inspections via the
  inspection_id link (not a direct reinspection column)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import reinspections as re_api
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    IncidentSnLink,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Reinspection,
)

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_BATCH = UUID("cccccccc-1111-2222-3333-444444444401")
_RULE = UUID("cccccccc-1111-2222-3333-444444444402")
_INSP1 = UUID("cccccccc-1111-2222-3333-444444444403")
_INSP2 = UUID("cccccccc-1111-2222-3333-444444444404")
_INC = UUID("cccccccc-1111-2222-3333-444444444405")
_EV1 = UUID("cccccccc-1111-2222-3333-444444444406")


def _mk_ins(session: Any, iid: UUID, sn: str, pmax: float) -> ModuleInspection:
    ins = ModuleInspection(
        inspection_id=iid,
        batch_id=_BATCH,
        sn=sn,
        source_type="IV_CSV",
        captured_at=_TS,
        pmax=pmax,
        temp=25.0,
        isc=10.5,
        voc=0.5,
        vm=48.0,
        im=11.0,
        ff=0.75,
        appearance_result="pass",
    )
    session.add(ins)
    return ins


def _seed(
    session: Any,
    *,
    incident_status: str = "pending_recheck",
    batch_status: str = "pending_recheck",
) -> dict[str, Any]:
    rule = QualityRuleVersion(
        rule_version_id=_RULE,
        project_id="P-1",
        model="JH-550W",
        name="rv-1",
        nameplate_power=550.0,
        power_tolerance=0.5,
        parameter_standards={},
        current_bands={},
        sample_plan={},
        effective_at=_TS,
        version_no=1,
    )
    batch = DeliveryBatch(
        batch_id=_BATCH,
        batch_no="B-RE-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=100,
        rule_version_id=_RULE,
        status=batch_status,
    )
    ev = Evidence(
        evidence_id=_EV1,
        evidence_type="decision_snapshot",
        object_ref={"kind": "module", "id": "SN-A"},
        content_hash="sha256:111",
        generated_at=_TS,
        generated_by="test",
    )
    incident = QualityIncident(
        incident_id=_INC,
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[str(_EV1)],
        status=incident_status,
        owner="ZHL-QA",
        due_at=_TS,
    )
    session.add_all([rule, ev])
    session.flush()
    session.add(batch)
    session.flush()
    ins1 = _mk_ins(session, _INSP1, "SN-A", 550.0)
    ins2 = _mk_ins(session, _INSP2, "SN-B", 545.0)
    session.add_all([ins1, ins2, incident])
    session.flush()
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP1))
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP2))
    session.commit()
    return {"incident": incident, "batch": batch}


@pytest.fixture()
def api_client() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(re_api.router)
    app.dependency_overrides[re_api.get_session] = lambda: session
    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_default_minimal_scope_server_filled(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    re_ = session.get(Reinspection, UUID(body["reinspection_id"]))
    assert re_.scope_type == "minimal"
    # server-filled = the incident's full SN set
    assert set(re_.scope_sns) == {"SN-A", "SN-B"}
    # verification_id written back
    inc = session.get(QualityIncident, _INC)
    assert inc.verification_id == re_.reinspection_id
    # response ``sn`` resolved from module_inspections via inspection_id link
    assert body["sn"] == "SN-A"
    assert body["passed"] is True
    assert body["scope_applied"] == "minimal"


def test_expanded_without_scope_sns_400(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
            "scope_type": "expanded",  # no scope_sns
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "scope_sns_required"


def test_expanded_scope_not_covering_minimal_400_lists_gap(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
            "scope_type": "expanded",
            "scope_sns": ["SN-A"],  # missing SN-B from the minimal set
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    # gap is listed
    assert "SN-B" in detail["missing"] or "SN-B" in str(detail)


def test_independent_row_module_inspections_unchanged(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    original_pmax = session.get(ModuleInspection, _INSP1).pmax
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 555.0,  # different from the original module inspection
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
        },
    )
    assert resp.status_code == 201
    re_ = session.get(Reinspection, UUID(resp.json()["reinspection_id"]))
    assert re_.pmax == 555.0  # the reinspection carries its own value
    # module_inspections original row is untouched
    assert session.get(ModuleInspection, _INSP1).pmax == original_pmax


def test_fail_result_transitions_rejected(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, incident_status="pending_recheck", batch_status="pending_recheck")
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "fail",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text
    assert session.get(QualityIncident, _INC).status == "rejected"
    assert session.get(DeliveryBatch, _BATCH).status == "rejected"
    assert resp.json()["passed"] is False


def test_list_reinspections(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
        },
    )
    resp = client.get(f"/api/incidents/{_INC}/reinspections")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_reinspection_rejected_for_closed_incident(api_client: Any) -> None:
    """复检记录只能在异常处于 ``assigned`` / ``pending_recheck`` 时写入。

    原实现不校验异常状态：对已关闭（甚至已报废）的异常仍可追加复检记录，
    并把 ``verification_id`` 覆盖掉 —— 闭环签署过的结论会被事后改写。
    """
    client, session, _ = api_client
    _seed(session, incident_status="closed", batch_status="released")
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": _TS.isoformat(),
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["status"] == "closed"
    assert session.query(Reinspection).count() == 0
    assert session.get(QualityIncident, _INC).verification_id is None


def test_reinspection_rejected_before_the_incident_is_assigned(api_client: Any) -> None:
    """``new`` / ``isolated`` 的异常还没有处置责任人，不能直接复检。"""
    client, session, _ = api_client
    _seed(session, incident_status="new", batch_status="isolated")
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": _TS.isoformat(),
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["status"] == "new"
    assert session.query(Reinspection).count() == 0


def test_reinspection_insert_and_verification_writeback_are_fk_safe(api_client: Any) -> None:
    """回归：循环外键顺序（生产 Postgres 500 的根因）。

    ``reinspections.incident_id -> quality_incidents`` 与
    ``quality_incidents.verification_id -> reinspections`` 互为外键，SQLAlchemy
    的工作单元无法给这个环排序，默认先 UPDATE quality_incidents 再 INSERT
    reinspections —— Postgres 直接报 fk_incident_verification 违约。端点必须先
    flush 复检行再回填 ``verification_id``。

    本用例之所以此前漏网：SQLite 默认不强制外键。``tests/conftest.py`` 现已
    全局 ``PRAGMA foreign_keys=ON``，所以这条路径在测试里也会按 Postgres 语义
    失败——删掉端点里的 ``session.flush()``，本用例即红。
    """
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text
    re_id = UUID(resp.json()["reinspection_id"])
    # Both directions of the cycle are satisfied: the reinspection row exists
    # (so the FK the incident points at resolves) and the incident points at it.
    assert session.get(Reinspection, re_id) is not None
    assert session.get(QualityIncident, _INC).verification_id == re_id


def test_pass_reinspection_auto_marks_measures_verified(api_client: Any) -> None:
    """复检通过即自动置「整改措施已验证」（2026-10-15 反馈）。

    L3 门禁本来就规定 ``verified=true`` 必须先有通过的复检记录
    （``app/api/incidents.py``），所以这个标记从来不是一支独立判断，只是对
    「复检已通过」的重述。此前它需要操作员从复检节点**倒回**异常处置节点
    手动补勾，一步不产生新信息的手续；改为在复检通过的时刻落位。
    """
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text
    assert session.get(QualityIncident, _INC).capa_verified is True


def test_fail_reinspection_does_not_mark_measures_verified(api_client: Any) -> None:
    """复检不通过绝不标记已验证（否则 close 五条件里的 capa_verified 形同虚设）。"""
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_INC}/reinspections",
        json={
            "inspection_id": str(_INSP1),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 0.5,
            "vm": 48.0,
            "im": 11.0,
            "ff": 0.75,
            "result": "fail",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-01T10:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text
    inc = session.get(QualityIncident, _INC)
    assert inc.capa_verified is False
    assert inc.status == "rejected"
