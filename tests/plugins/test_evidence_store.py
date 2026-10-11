"""Evidence-store skeleton tests (Task 7, P0 spec §5.4 / D10 / D11).

``FileEvidenceStore`` is a Phase-0 skeleton: it computes the SHA-256 hash of
saved content, writes it under ``root`` with the evidence-type subdirectory
layout (总 spec §9.5: ``evidence_type/{yyyy}/{mm}/{evidence_id}/{filename}``),
and records metadata in memory. It does NOT write to the DB (that is Phase A),
so the skeleton must be constructible with ``db=None`` and fully testable in
memory / on a temp dir — no DB, no clock reading beyond the file's own write
time (we pin ``generated_at`` deterministically via an injected value, not
``datetime.now()``).
"""

from __future__ import annotations

from pathlib import Path

from app.plugins.evidence_store import EvidenceRef, FileEvidenceStore, make_evidence_id


def test_make_evidence_id_deterministic_from_ref() -> None:
    """IDs are derived from the object_ref so a re-save is idempotent-ish and
    reproducible, not a fresh random UUID each time."""
    ref = {"kind": "batch", "id": "B-1"}
    a = make_evidence_id(ref)
    b = make_evidence_id(ref)
    assert a == b
    assert isinstance(a, str) and a


def test_save_computes_sha256(tmp_path: Path) -> None:
    store = FileEvidenceStore(root=tmp_path, db=None)
    ev = store.save(
        "source_file",
        {"kind": "inspection", "id": "I-1"},
        content=b"pv-data",
    )
    assert isinstance(ev, EvidenceRef)
    assert ev.content_hash
    # SHA-256 digest: 64 lowercase hex chars, valid as hex
    assert len(ev.content_hash) == 64
    assert all(c in "0123456789abcdef" for c in ev.content_hash)
    assert ev.evidence_type == "source_file"
    assert ev.object_ref == {"kind": "inspection", "id": "I-1"}
    # the file was written under the root with the type subdirectory
    target = tmp_path / ev.file_path
    assert target.exists()


def test_save_returns_stable_id_and_writes_meta(tmp_path: Path) -> None:
    store = FileEvidenceStore(root=tmp_path, db=None)
    ev = store.save(
        "decision_snapshot",
        {"kind": "batch", "id": "B-9"},
        content=b'{"result":"Pass"}',
        meta={"batch_id": "B-9"},
    )
    got = store.list_by_object({"kind": "batch", "id": "B-9"})
    assert len(got) == 1
    assert got[0].evidence_id == ev.evidence_id
    # get() round-trips the stored bytes
    payload = store.get(ev.evidence_id)
    assert payload.content == b'{"result":"Pass"}'


def test_list_by_object_filters_by_ref(tmp_path: Path) -> None:
    store = FileEvidenceStore(root=tmp_path, db=None)
    store.save("source_file", {"kind": "inspection", "id": "I-1"}, content=b"a")
    store.save("source_file", {"kind": "inspection", "id": "I-2"}, content=b"b")
    for obj in ({"kind": "inspection", "id": "I-1"}, {"kind": "inspection", "id": "I-2"}):
        listed = store.list_by_object(obj)
        assert len(listed) == 1
