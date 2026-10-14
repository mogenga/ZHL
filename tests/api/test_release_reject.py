"""Manual reject endpoint tests (T-3 Task 6).

A batch rejected by a human is a *terminal* disposition: the same
transaction that records the rejection also (D7) generates the incident
disposal report and (D6a) auto-archives the batch.
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

from app.api import release as release_router
from app.infra.models import Base, DeliveryBatch, QualityRuleVersion, Report

_TS = datetime(2026, 6, 1, tzinfo=UTC)
_BATCH = UUID("dddddddd-1111-2222-3333-444444444430")
_RULE = UUID("dddddddd-1111-2222-3333-444444444431")


def _seed_batch(session: Any, *, status: str = "pending_release") -> DeliveryBatch:
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
        batch_no="B-REJ-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=10,
        rule_version_id=_RULE,
        status=status,
    )
    session.add_all([rule, batch])
    session.commit()
    return batch


@pytest.fixture()
def api_client() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(release_router.router)
    app.dependency_overrides[release_router.get_session] = lambda: session
    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_reject_from_pending_release(api_client: Any) -> None:
    """pending_release + reject -> 200, batch archived (D6a), D7 incident
    report row exists at version 1, trace row written (D1)."""
    client, session, _ = api_client
    _seed_batch(session, status="pending_release")
    resp = client.post(
        f"/api/batches/{_BATCH}/reject",
        json={"reason": "复检全批不合格，拒绝交付", "rejected_by": "ZHL-QA"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "archived"
    assert body["auto_archived"] is True
    assert body["reason"] == "复检全批不合格，拒绝交付"
    batch = session.get(DeliveryBatch, _BATCH)
    assert batch.status == "archived"
    # D7: an incident report row was written for the batch.
    report = (
        session.query(Report)
        .filter(Report.batch_id == _BATCH, Report.report_type == "incident")
        .order_by(Report.version.desc())
        .first()
    )
    assert report is not None
    assert report.version == 1
    # 拒因是操作员填的真实 reason，进报告正文（不再是编造的占位文本）
    assert "复检全批不合格，拒绝交付" in (report.content_md or "")
    assert report.content_json["refusal_reason"] == "复检全批不合格，拒绝交付"


def test_reject_d8_archived_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed_batch(session, status="archived")
    resp = client.post(
        f"/api/batches/{_BATCH}/reject",
        json={"reason": "x", "rejected_by": "ZHL-QA"},
    )
    assert resp.status_code == 409
    assert "archived" in str(resp.json().get("detail", ""))


def test_reject_unknown_batch_404(api_client: Any) -> None:
    client, session, _ = api_client
    _seed_batch(session, status="pending_release")  # ensure the table exists
    unknown = UUID("eeeeeeee-0000-0000-0000-000000000044")
    resp = client.post(
        f"/api/batches/{unknown}/reject",
        json={"reason": "x", "rejected_by": "ZHL-QA"},
    )
    assert resp.status_code == 404


def test_reject_writes_trace(api_client: Any) -> None:
    client, session, _ = api_client
    _seed_batch(session, status="pending_release")
    client.post(
        f"/api/batches/{_BATCH}/reject",
        json={"reason": "拒绝交付", "rejected_by": "ZHL-QA"},
    )
    from app.plugins.trace_store import TraceStore

    traces = TraceStore().list_for_batch(session, _BATCH)
    assert any(t.skill_name == "reject" for t in traces)
