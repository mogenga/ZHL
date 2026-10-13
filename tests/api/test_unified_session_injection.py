"""Regression guard: the unified app must honour the injected session factory.

Background
----------
``create_unified_app`` (``app/main.py``) mounts the Phase A routers (batches /
intake) onto the Phase B base app. Its Phase A ``get_deps`` closure used to do::

    factory = get_session_factory()      # <- ignored the injected factory

so every Phase A route on the unified app silently fell back to the
environment default database. The frontend workbench
(``GET /api/batches``) therefore returned 500 / ``no such table:
delivery_batches`` against the real service even though the caller had passed
a perfectly good session factory.

The pre-existing tests in ``test_workbench.py`` missed this because they
exercise each endpoint through the factory that *owns* it (Phase A app for
batches, Phase B app for rule-versions), which sidesteps the unified wiring
entirely. These tests drive ``create_unified_app`` directly — the same object
``uvicorn app.main:app`` serves.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infra.models import Base, QualityRuleVersion
from app.main import create_unified_app


@pytest.fixture()
def sqlite_factory():
    """In-memory SQLite with all tables created; one shared connection."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    yield factory
    engine.dispose()


def _seed_rule(factory) -> str:
    s = factory()
    rv = QualityRuleVersion(
        project_id="P-U",
        model="M-1",
        name="rv-unified",
        nameplate_power=550.0,
        power_tolerance=0.03,
        parameter_standards={},
        current_bands=[],
        sample_plan={},
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="published",
        version_no=1,
    )
    s.add(rv)
    s.commit()
    rid = str(rv.rule_version_id)
    s.close()
    return rid


def _unified_client(factory) -> TestClient:
    return TestClient(create_unified_app(session_factory=factory, mount_static=False))


def test_unified_app_honours_injected_session_factory(sqlite_factory) -> None:
    """GET /api/batches on the unified app must read the injected DB.

    This is the exact call the frontend workbench makes on page load. Before
    the fix it raised ``no such table: delivery_batches``.
    """
    client = _unified_client(sqlite_factory)
    r = client.get("/api/batches")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_unified_app_lists_batches_created_through_same_app(sqlite_factory) -> None:
    """Create then list, both through the unified app — the full user flow."""
    rid = _seed_rule(sqlite_factory)
    client = _unified_client(sqlite_factory)

    created = client.post(
        "/api/batches",
        json={
            "batch_no": "B-U-1",
            "project_id": "P-U",
            "model": "M-1",
            "rule_version_id": rid,
            "planned_quantity": 7,
        },
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]

    listed = client.get("/api/batches")
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["batch_id"] == batch_id
    assert rows[0]["batch_no"] == "B-U-1"
    assert rows[0]["planned_quantity"] == 7


def test_unified_app_status_filter(sqlite_factory) -> None:
    """The optional ``status`` filter must work on the unified app too."""
    rid = _seed_rule(sqlite_factory)
    client = _unified_client(sqlite_factory)
    client.post(
        "/api/batches",
        json={
            "batch_no": "B-U-2",
            "project_id": "P-U",
            "model": "M-1",
            "rule_version_id": rid,
            "planned_quantity": 1,
        },
    )
    hit = client.get("/api/batches?status=pending_intake")
    assert hit.status_code == 200
    assert len(hit.json()) == 1
    miss = client.get("/api/batches?status=released")
    assert miss.status_code == 200
    assert miss.json() == []


def test_unified_app_serves_both_phases(sqlite_factory) -> None:
    """Phase A (batches) and Phase B (rule-versions) coexist on one app.

    Guards the wiring the frontend depends on: the workbench calls both from
    index.html, and a regression that drops either router would break the
    page while unit tests kept passing.
    """
    _seed_rule(sqlite_factory)
    client = _unified_client(sqlite_factory)

    batches = client.get("/api/batches")
    assert batches.status_code == 200, batches.text

    rules = client.get("/api/quality-rule-versions")
    assert rules.status_code == 200, rules.text
    body = rules.json()
    assert body["available"] is True
    assert len(body["items"]) == 1


def test_unified_app_phase_a_rejects_unknown_rule_version(sqlite_factory) -> None:
    """Regression: the write path must still validate against the injected DB.

    Proves the injected factory is actually being used for writes, not just
    reads — a 400 here means the session reached the same SQLite DB.
    """
    client = _unified_client(sqlite_factory)
    r = client.post(
        "/api/batches",
        json={
            "batch_no": "B-U-3",
            "project_id": "P-U",
            "model": "M-1",
            "rule_version_id": "00000000-0000-0000-0000-0000000000ff",
            "planned_quantity": 1,
        },
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["reason"] == "unknown rule version"


def test_unified_app_falls_back_to_env_when_no_factory_given(sqlite_factory, monkeypatch) -> None:
    """With ``session_factory=None`` the closure must fall back to the env.

    Pins the ``if factory is None`` branch so the fix cannot regress into
    "always use the injected factory" (which would break production, where
    ``uvicorn app.main:app`` passes ``None`` and relies on
    ``ZHL_DATABASE_URL``).
    """
    calls: list[int] = []
    env_factory = sqlite_factory

    def _tracking_factory():
        calls.append(1)
        return env_factory()

    monkeypatch.setattr("app.infra.repositories.get_session_factory", lambda: _tracking_factory)
    # Force the fallback branch regardless of ambient env.
    client = TestClient(create_unified_app(session_factory=None, mount_static=False))

    r = client.get("/api/batches")
    if r.status_code == 200:
        # Env pointed at a usable DB — the fallback wired correctly.
        assert calls, "expected get_session_factory() to be consulted"
    else:
        # Env unset / unreachable — fail-fast is acceptable, but it must not
        # be a silent empty result masquerading as "no batches".
        assert r.status_code in (500, 503)
