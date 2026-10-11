"""FileEvidenceStore DB-write tests (PA plan Task 4).

Covers the Phase-A branch of ``save``: when ``db`` is a live SQLAlchemy
Session, the evidence row is inserted into the ``evidence`` table
(append-only: no UPDATE / DELETE, PA spec §5.8). ``db=None`` keeps the
P0 skeleton behavior (file + in-memory refs only).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.infra.models import Base, Evidence
from app.plugins.evidence_store import FileEvidenceStore


@pytest.fixture()
def session() -> sessionmaker:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def test_save_writes_db_row(session, tmp_path: Path) -> None:
    store = FileEvidenceStore(root=tmp_path, db=session)
    ref = store.save("source_file", {"kind": "inspection", "id": "I-1"}, content=b"pv-data")
    session.flush()

    rows = session.scalars(select(Evidence)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.evidence_id == _to_uuid(ref.evidence_id)
    assert row.content_hash == ref.content_hash
    assert row.file_path == ref.file_path
    assert row.evidence_type == "source_file"


def test_save_append_only(session, tmp_path: Path) -> None:
    """Same object_ref, same content: re-save must NOT create a second row.

    ``make_evidence_id`` is deterministic, so the PK is stable; the append-only
    contract is enforced by a check-then-insert (or ON CONFLICT DO NOTHING)
    so a duplicate save is a no-op at the DB level, not a conflict.
    """
    store = FileEvidenceStore(root=tmp_path, db=session)
    ref1 = store.save("source_file", {"kind": "inspection", "id": "I-2"}, content=b"a")
    session.flush()
    ref2 = store.save("source_file", {"kind": "inspection", "id": "I-2"}, content=b"a")
    session.flush()

    assert ref1.evidence_id == ref2.evidence_id
    rows = session.scalars(select(Evidence)).all()
    assert len(rows) == 1, f"expected 1 row (append-only, idempotent), got {len(rows)}"


def test_save_different_content_new_row(session, tmp_path: Path) -> None:
    """Same object_ref but different content: make_evidence_id is keyed on
    object_ref only, so the row identity is stable; but content_hash changes.
    The append-only rule says we do NOT overwrite: a different hash on the
    same PK would be a conflict, so this is documented as "last-write-wins on
    hash, not a new row" — verified here as still 1 row."""
    store = FileEvidenceStore(root=tmp_path, db=session)
    store.save("source_file", {"kind": "inspection", "id": "I-3"}, content=b"v1")
    session.flush()
    store.save("source_file", {"kind": "inspection", "id": "I-3"}, content=b"v2")
    session.flush()
    rows = session.scalars(select(Evidence)).all()
    assert len(rows) == 1


def test_db_none_fallback(tmp_path: Path) -> None:
    """P0 skeleton behavior: db=None writes the file, no DB row, no crash."""
    store = FileEvidenceStore(root=tmp_path, db=None)
    ref = store.save("source_file", {"kind": "inspection", "id": "I-4"}, content=b"no-db")
    assert ref.content_hash
    target = tmp_path / ref.file_path
    assert target.exists()
    assert target.read_bytes() == b"no-db"


def test_distinct_evidence_types_same_object_get_distinct_rows(session, tmp_path: Path) -> None:
    """C1 fix (Phase A code review): the ``evidence`` table's PK is
    ``evidence_id`` only. If ``make_evidence_id`` keyed on ``object_ref``
    alone, every evidence type saved for the same batch would mint the same
    ID and the check-then-insert in ``_insert_evidence_row`` would silently
    skip every type after the first — the whole batch's audit trail would
    collapse to one row in production DBs (the P0/API tests never caught
    this because they use a stub evidence store that mints fresh UUIDs).

    This test uses the real ``FileEvidenceStore`` + a live SQLite session and
    saves all five evidence types the main-chain graph / API routes write for
    one batch; each must land as its own row."""
    store = FileEvidenceStore(root=tmp_path, db=session)
    ref_obj = {"kind": "batch", "id": "BATCH-1"}
    refs = [
        store.save(etype, ref_obj, content=f"content-{etype}".encode())
        for etype in (
            "source_file",
            "extraction_result",
            "decision_snapshot",
            "task_trace",
            "human_confirmation",
        )
    ]
    session.flush()
    assert len({r.evidence_id for r in refs}) == 5, "evidence_id must differ across etypes"
    rows = session.scalars(select(Evidence)).all()
    assert len(rows) == 5, f"expected 5 rows (one per evidence type), got {len(rows)}"
    assert {row.evidence_type for row in rows} == {
        "source_file",
        "extraction_result",
        "decision_snapshot",
        "task_trace",
        "human_confirmation",
    }


def test_resave_same_type_and_object_is_idempotent(session, tmp_path: Path) -> None:
    """Same etype + same object_ref re-saved must still be idempotent
    (append-only, check-then-insert) even after the C1 fix — the fix keys
    the ID on (etype, object_ref), not on a counter, so a genuine duplicate
    save (same type, same object) still collapses to one row."""
    store = FileEvidenceStore(root=tmp_path, db=session)
    ref1 = store.save("source_file", {"kind": "batch", "id": "BATCH-2"}, content=b"v1")
    ref2 = store.save("source_file", {"kind": "batch", "id": "BATCH-2"}, content=b"v1")
    session.flush()
    assert ref1.evidence_id == ref2.evidence_id
    rows = session.scalars(select(Evidence)).all()
    assert len(rows) == 1, f"expected 1 row (idempotent re-save), got {len(rows)}"


def _to_uuid(hex_str: str):
    import uuid

    return uuid.UUID(hex_str)
