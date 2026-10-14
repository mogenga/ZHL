"""复检闭环 → 放行：第 2 轮结论的两个入口（关闭端点 / 重算端点）。

复现的缺陷（真实链路）：抽样 fail → 异常 → 复检通过 → CAPA verified → 关闭 →
批次 ``pending_release`` → 放行被断言②永久阻断（``sample result is fail,
expected pass``），因为结论侧永远只有第 1 轮那条 fail。

本文件锁住修复后的端到端行为：
- 关闭最后一个异常时，闭环成立即补写第 2 轮结论 → 放行七项全过；
- 闭环**不**成立时（复检未覆盖 / 复检未通过）不得写结论，放行继续被阻断；
- 已经关闭完的历史批次可由 ``POST /api/batches/{id}/recheck`` 幂等补齐。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.infra.models import Base, DeliveryBatch, QualityIncident, SampleConclusion
from tests.api.test_release_api import _BATCH, _INC, _seed

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)


@pytest.fixture()
def client() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = create_app(session_factory=lambda: session, own_session=False)
    yield TestClient(app), session
    session.close()
    engine.dispose()


def _conclusions(session: Any, round_no: int) -> list[SampleConclusion]:
    return list(
        session.scalars(
            select(SampleConclusion).where(
                SampleConclusion.batch_id == _BATCH,
                SampleConclusion.sample_round == round_no,
            )
        ).all()
    )


def _ready_to_close(session: Any) -> None:
    """把共享 seed 补成"关闭前"的真实状态。

    seed 直接给了 ``pending_release``（放行侧的测试用不到关闭前的态），但关闭
    端点的真实前置是：异常 ``pending_recheck`` 且**批次** ``pending_recheck``
    ——关闭的联动（批次转待放行 + 补写第 2 轮结论）就在那条边上。另外补上
    五条件里的临时措施字段。
    """
    inc = session.get(QualityIncident, _INC)
    inc.containment_action = "halt line, segregate lot"
    session.get(DeliveryBatch, _BATCH).status = "pending_recheck"
    session.commit()


def test_close_writes_round_two_and_unblocks_release(client: Any) -> None:
    c, session = client
    _seed(session, sample_result="fail", incident_status="pending_recheck")
    _ready_to_close(session)

    r = c.post(f"/api/incidents/{_INC}/close")
    assert r.status_code == 200, r.text
    assert r.json()["batch_status"] == "pending_release"

    rows = _conclusions(session, 2)
    assert len(rows) == 1
    assert rows[0].result == "pass"

    r = c.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert r.status_code == 200, r.text
    body = r.json()
    failed = [chk for chk in body["checks"] if not chk["passed"]]
    assert body["passed"] is True, failed
    assert body["status"] == "released"


def test_close_writes_nothing_when_reinspection_did_not_pass(client: Any) -> None:
    """复检 fail 的异常根本关不掉（五条件），结论侧也不得出现第 2 轮。"""
    c, session = client
    _seed(
        session, sample_result="fail", incident_status="pending_recheck", reinspection_present=False
    )
    _ready_to_close(session)

    r = c.post(f"/api/incidents/{_INC}/close")
    assert r.status_code == 409, r.text
    assert _conclusions(session, 2) == []
    # 异常关不掉 ⇒ 批次回不到待放行：放行/重算两个入口都进不去（409），
    # 不存在"绕一次重算就放行"的旁路。
    assert session.get(QualityIncident, _INC).status == "pending_recheck"
    assert (
        c.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"}).status_code == 409
    )
    assert c.post(f"/api/batches/{_BATCH}/recheck").status_code == 409


def test_close_writes_nothing_when_scope_does_not_cover_failed_sn(client: Any) -> None:
    c, session = client
    _seed(
        session,
        sample_result="fail",
        incident_status="pending_recheck",
        reinspection_scope=["SN-A"],  # 判 fail 的 SN-B 未被复检覆盖
    )
    _ready_to_close(session)

    r = c.post(f"/api/incidents/{_INC}/close")
    assert r.status_code == 200, r.text
    assert _conclusions(session, 2) == []

    r = c.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    body = r.json()
    assert body["passed"] is False
    sample_check = next(chk for chk in body["checks"] if chk["name"] == "sample_passed")
    assert "SN-B" in sample_check["reason"] or "fail" in sample_check["reason"]


def test_recheck_endpoint_backfills_for_already_closed_batch(client: Any) -> None:
    """历史批次（异常早已关闭、结论侧没补）由重算端点补齐。"""
    c, session = client
    _seed(session, sample_result="fail", incident_status="closed")

    r = c.post(f"/api/batches/{_BATCH}/recheck")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "pass"
    assert body["written"] is True
    assert len(_conclusions(session, 2)) == 1

    r = c.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert r.json()["passed"] is True


def test_recheck_endpoint_is_idempotent(client: Any) -> None:
    c, session = client
    _seed(session, sample_result="fail", incident_status="closed")

    assert c.post(f"/api/batches/{_BATCH}/recheck").json()["written"] is True
    second = c.post(f"/api/batches/{_BATCH}/recheck").json()
    assert second["written"] is False
    assert second["result"] == "pass"
    assert second["sample_round"] == 2  # 回报既存轮次，不报 0
    assert len(_conclusions(session, 2)) == 1


def test_recheck_without_any_conclusion_says_so(client: Any) -> None:
    """从未抽样过的批次：如实说明没有可复检的结论，不写行、不套别的话术。"""
    c, session = client
    _seed(session, incident_status="closed", with_conclusion=False)

    body = c.post(f"/api/batches/{_BATCH}/recheck").json()
    assert body["written"] is False
    assert body["skipped"] == "no_sample_conclusion"
    assert body["reason"]  # 有可读原因，界面不必自己编
    assert _conclusions(session, 2) == []


def test_recheck_reports_failure_and_does_not_release_incomplete_remediation(client: Any) -> None:
    """闭环没走完：重算如实报 fail，绝不写通过的结论。"""
    c, session = client
    _seed(session, sample_result="fail", incident_status="closed", reinspection_scope=["SN-A"])

    r = c.post(f"/api/batches/{_BATCH}/recheck")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "fail"
    assert body["written"] is False
    assert "SN-B" in body["reason"]
    assert _conclusions(session, 2) == []

    assert (
        c.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"}).json()["passed"]
        is False
    )


def test_recheck_rejects_batch_not_pending_release(client: Any) -> None:
    c, session = client
    seeded = _seed(session, sample_result="fail", incident_status="closed")
    seeded["batch"].status = "pending_recheck"
    session.commit()

    r = c.post(f"/api/batches/{_BATCH}/recheck")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "batch_not_pending_release"


def test_recheck_unknown_batch_is_404(client: Any) -> None:
    c, _ = client
    r = c.post(f"/api/batches/{UUID(int=7)}/recheck")
    assert r.status_code == 404
