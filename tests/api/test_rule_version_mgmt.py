"""Rule version management API tests (create/publish/disable/events)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.infra.models import Base, QualityRuleVersion, RuleVersionEvent


def _make_app(session: Any) -> TestClient:
    from app.api import rule_versions

    app = create_app(session_factory=lambda: session, mount_static=False)
    app.dependency_overrides[rule_versions.get_session] = lambda: session
    return TestClient(app)


def _seed_published_rule(session: Any) -> QualityRuleVersion:
    r = QualityRuleVersion(
        project_id="P1",
        model="M1",
        name="n",
        nameplate_power=550.0,
        power_tolerance=0.03,
        parameter_standards={},
        current_bands={},
        sample_plan={},
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="draft",
        version_no=1,
    )
    session.add(r)
    session.commit()
    return r


def test_create_rule_version_new() -> None:
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, future=True)()
    client = _make_app(session)
    body = {
        "project_id": "P2",
        "model": "M2",
        "name": "v1",
        "nameplate_power": 400.0,
        "power_tolerance": 0.05,
        "parameter_standards": {},
        "current_bands": {},
        "sample_plan": {},
        "effective_at": "2026-02-01T00:00:00Z",
        "version_no": 1,
    }
    resp = client.post("/api/quality-rule-versions", json=body)
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert data["duplicate"] is False
    assert data["status"] == "draft"
    assert data["event_id"] is not None
    # event 已落库
    ev = session.query(RuleVersionEvent).filter_by(action="created").one()
    assert ev.rule_version_id == UUID(data["rule_version_id"])
    session.close()


def test_create_rule_version_idempotent_duplicate() -> None:
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, future=True)()
    existing = _seed_published_rule(session)
    client = _make_app(session)
    body = {
        "project_id": "P1",
        "model": "M1",
        "name": "n",
        "nameplate_power": 550.0,
        "power_tolerance": 0.03,
        "parameter_standards": {},
        "current_bands": {},
        "sample_plan": {},
        "effective_at": "2026-01-01T00:00:00Z",
        "version_no": 1,
    }
    resp = client.post("/api/quality-rule-versions", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is True
    assert data["rule_version_id"] == str(existing.rule_version_id)
    assert data["event_id"] is None
    # 幂等重复不写新 event
    assert session.query(RuleVersionEvent).count() == 0
    session.close()
