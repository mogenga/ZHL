"""API tests for the intake routes (PA plan Task 8, §5.6 / Q1–Q3)."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from app.infra.models import Base
from tests.api.test_batches import _ISO_TABLE, _make_client


def test_confirm_unknown_task_returns_404() -> None:
    """B3: confirming an unknown task_id -> 404, not 500."""
    from tests.api.test_batches import _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-CONF", [], s, s, _ISO_TABLE)

    r = client.post(
        "/api/intake/00000000-0000-0000-0000-000000000000/confirm",
        json={"action": "confirm"},
    )
    assert r.status_code == 404


def test_intake_unknown_batch_returns_404() -> None:
    """B3: intake into an unknown batch -> 404."""
    from tests.api.test_batches import _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-CONF", [], s, s, _ISO_TABLE)

    r = client.post(
        "/api/intake",
        json={
            "batch_id": "00000000-0000-0000-0000-000000000000",
            "inspections": [],
        },
    )
    assert r.status_code == 404
