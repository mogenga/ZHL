"""Tests for the two new read endpoints (frontend workbench):

- GET /api/batches            (list, desc by created_at, optional status filter)
- GET /api/quality-rule-versions (discovery, {items, available, reason})

Both follow the project's injectable-session convention: tests build the app
with a SQLite ``session_factory``, so no live Postgres or ``ZHL_DATABASE_URL``
is required.

Routing note: ``GET /api/batches`` lives in the Phase A batches router
(``app.main.create_app``), while ``GET /api/quality-rule-versions`` and the
static mount live in the Phase B factory (``app.api.main.create_app``).
``create_unified_app`` wires both, but its Phase A ``get_deps`` resolves the
repository factory from the environment, so we test each endpoint with the
factory that owns it to keep the session fully injectable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app as create_phase_b_app
from app.infra.models import Base, DeliveryBatch, QualityRuleVersion
from app.main import create_app as create_phase_a_app


@pytest.fixture()
def sqlite_env():
    """Shared in-memory SQLite: one connection for every session."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    yield factory, engine
    engine.dispose()


def _seed_rule(factory, status: str = "published", version_no: int = 1) -> str:
    s = factory()
    rv = QualityRuleVersion(
        project_id="P-X",
        model="M-1",
        name=f"rv-{version_no}",
        nameplate_power=550.0,
        power_tolerance=0.03,
        parameter_standards={},
        current_bands=[],
        sample_plan={},
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=status,
        version_no=version_no,
    )
    s.add(rv)
    s.commit()
    rid = str(rv.rule_version_id)
    s.close()
    return rid


def _batches_client(factory) -> TestClient:
    """Phase A app: owns GET /api/batches (list + create + detail + judge)."""
    return TestClient(create_phase_a_app(session_factory=factory))


def _rule_versions_client(factory, mount_static: bool = False) -> TestClient:
    """Phase B app: owns GET /api/quality-rule-versions + static mount."""
    return TestClient(create_phase_b_app(session_factory=factory, mount_static=mount_static))


# --- GET /api/batches -------------------------------------------------------


def test_list_batches_empty(sqlite_env) -> None:
    factory, _ = sqlite_env
    client = _batches_client(factory)
    r = client.get("/api/batches")
    assert r.status_code == 200
    assert r.json() == []


def test_list_batches_desc_and_fields(sqlite_env) -> None:
    factory, _ = sqlite_env
    rid = _seed_rule(factory)
    client = _batches_client(factory)

    ids = []
    for i in range(3):
        resp = client.post(
            "/api/batches",
            json={
                "batch_no": f"B-{i}",
                "project_id": "P-X",
                "model": "M-1",
                "rule_version_id": rid,
                "planned_quantity": 5,
            },
        )
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["batch_id"])

    r = client.get("/api/batches")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    # 字段齐全
    assert set(rows[0].keys()) == {
        "batch_id",
        "batch_no",
        "project_id",
        "model",
        "status",
        "planned_quantity",
        "created_at",
    }
    # 倒序：最后创建（B-2）在最前
    assert rows[0]["batch_id"] == ids[2]
    assert rows[2]["batch_id"] == ids[0]
    # 默认 pending_intake
    assert all(row["status"] == "pending_intake" for row in rows)


def test_list_batches_status_filter(sqlite_env) -> None:
    factory, _ = sqlite_env
    rid = _seed_rule(factory)
    client = _batches_client(factory)

    resp = client.post(
        "/api/batches",
        json={
            "batch_no": "B-F",
            "project_id": "P-X",
            "model": "M-1",
            "rule_version_id": rid,
            "planned_quantity": 5,
        },
    )
    bid = resp.json()["batch_id"]
    s = factory()
    b = s.get(DeliveryBatch, uuid.UUID(bid))
    b.status = "released"
    s.commit()
    s.close()

    rel = client.get("/api/batches?status=released").json()
    assert len(rel) == 1 and rel[0]["batch_id"] == bid
    # pending_intake 应为空（那条已 released）
    assert client.get("/api/batches?status=pending_intake").json() == []
    # 未知状态 → 空
    assert client.get("/api/batches?status=bogus").json() == []


# --- GET /api/quality-rule-versions -----------------------------------------


def test_rule_versions_empty_available(sqlite_env) -> None:
    factory, _ = sqlite_env
    client = _rule_versions_client(factory)
    r = client.get("/api/quality-rule-versions")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["items"] == []
    assert body["reason"] is None


def test_rule_versions_with_items(sqlite_env) -> None:
    factory, _ = sqlite_env
    _seed_rule(factory, status="published", version_no=1)
    _seed_rule(factory, status="draft", version_no=2)
    client = _rule_versions_client(factory)
    r = client.get("/api/quality-rule-versions")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert len(body["items"]) == 2
    # 每条字段齐全
    for it in body["items"]:
        expected = {
            "rule_version_id",
            "project_id",
            "model",
            "name",
            "status",
            "version_no",
            "effective_at",
        }
        assert set(it.keys()) == expected
    # 含 published 与 draft
    statuses = {it["status"] for it in body["items"]}
    assert statuses == {"published", "draft"}


def test_rule_versions_table_missing() -> None:
    """未建表时返回 available=False 而非 500（前端区分『无数据』与『表不可用』）。"""
    engine = create_engine("sqlite://")  # 不 create_all，模拟未跑 DDL
    factory = sessionmaker(bind=engine, future=True)
    client = _rule_versions_client(factory)
    r = client.get("/api/quality-rule-versions")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "table_missing"
    assert body["items"] == []
    engine.dispose()


def test_static_mount_serves_workbench(sqlite_env) -> None:
    """mount_static=True 时 /static/workbench.html 200；False 时不挂静态（404）。"""
    factory, _ = sqlite_env
    on = _rule_versions_client(factory, mount_static=True)
    assert on.get("/static/workbench.html").status_code == 200
    off = _rule_versions_client(factory, mount_static=False)
    assert off.get("/static/workbench.html").status_code == 404
