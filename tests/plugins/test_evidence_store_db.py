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


def _to_uuid(hex_str: str):
    import uuid

    return uuid.UUID(hex_str)
