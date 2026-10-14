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


def test_intake_partial_inspection_row_returns_400() -> None:
    """B3: a legacy partial row ({sn, source_type} only) must surface as a
    structured 400 naming the missing fields, not a KeyError -> 500 inside the
    graph (the original intake.js bug)."""
    from tests.api.test_batches import _r_for, _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-CONF", ["SN-N-001"], s, s, _ISO_TABLE)
    from tests.api.test_batches import _post_batch

    batch = _post_batch(client, planned_quantity=1)

    r = client.post(
        "/api/intake",
        json={
            "batch_id": batch["batch_id"],
            "inspections": [
                {"sn": "SN-N-001", "source_type": "IV_CSV"},  # legacy partial row
            ],
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["reason"] == "incomplete_inspection"
    # The full 9 columns the repo requires are named.
    for k in ("captured_at", "pmax", "temp", "isc", "voc", "vm", "im", "ff", "appearance_result"):
        assert k in detail["missing_fields"], k

    # A complete row (the fixed intake.js shape) succeeds and auto-confirms.
    ok = client.post(
        "/api/intake",
        json={
            "batch_id": batch["batch_id"],
            "inspections": [_r_for("SN-N-001")],
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "confirmed"
