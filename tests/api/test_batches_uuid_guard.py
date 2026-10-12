"""B3 UUID-guard tests for the batches routes (Phase A code review, Important #1).

A malformed UUID path/body param (e.g. ``batch_id="abc"``,
``rule_version_id="not-a-uuid"``) must not surface as an unhandled
``ValueError`` -> 500. The intake routes already guard this via
``_get_batch_by_id`` (M3, intake.py:32-41); the batches routes must
align to the same B3 contract (404 detail, not 500).
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from tests.api.test_batches import (
    _ISO_TABLE,
    _make_client,
    _shared_engine,
)


def _env_client(confidence: float = 0.9):
    engine = _shared_engine()
    from app.infra.models import Base

    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    sns = [f"SN-G-{i:02d}" for i in range(3)]
    client = _make_client(confidence, "SN-I-CONF", sns, s, s, _ISO_TABLE)
    return client, sns


def test_get_batch_malformed_uuid_returns_404() -> None:
    client, _ = _env_client()
    r = client.get("/api/batches/not-a-uuid")
    assert r.status_code == 404, r.text
    assert "not-a-uuid" in r.json()["detail"].get("batch_id", "")


def test_judge_malformed_uuid_returns_404() -> None:
    client, _ = _env_client()
    r = client.post("/api/batches/not-a-uuid/judge")
    assert r.status_code == 404, r.text
    assert "not-a-uuid" in r.json()["detail"].get("batch_id", "")


def test_create_batch_malformed_rule_version_id_returns_400() -> None:
    client, _ = _env_client()
    r = client.post(
        "/api/batches",
        json={
            "batch_no": "B-BAD-UUID",
            "project_id": "P",
            "model": "M",
            "rule_version_id": "not-a-uuid",
            "planned_quantity": 1,
        },
    )
    assert r.status_code == 400, r.text


def test_judge_unknown_uuid_returns_404() -> None:
    client, _ = _env_client()
    import uuid

    r = client.post(f"/api/batches/{uuid.uuid4()}/judge")
    assert r.status_code == 404, r.text
