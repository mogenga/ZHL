"""EvidenceStore Protocol + FileEvidenceStore (P0 spec §5.4 / D10 / D11,
PA plan Task 4 DB branch).

Phase 0 delivered the *file + hash* half: ``save`` computes SHA-256, writes
the bytes under ``root`` (evidence-type subdirectory layout, 总 spec §9.5),
and records metadata in memory. Phase A adds the DB half: when constructed
with a live SQLAlchemy ``Session``, ``save`` also INSERTs the matching
row into the ``evidence`` table (append-only — no UPDATE / DELETE, PA spec
§5.8). ``db=None`` keeps the P0 skeleton behavior exactly (file + in-memory
refs, no DB), so P0 tests stay green unmodified.

Purity boundary: this module lives in ``app/plugins`` and MAY reference the
SQLAlchemy ``Session`` / ``Evidence`` model types, but it never opens a DB
itself — the Session is injected by the caller, which owns the transaction
(AGENT.md §4.3: one call == one transaction boundary; the workflow node
commits after the evidence row lands).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infra.models import Evidence as EvidenceModel

#: D10: the default evidence root (overridable via ZHL_EVIDENCE_ROOT at Phase A).
DEFAULT_EVIDENCE_ROOT = "data/evidence"


@dataclass(frozen=True)
class EvidenceRef:
    """A pointer to a stored evidence record (the DB row's identifying fields)."""

    evidence_id: str
    evidence_type: str
    object_ref: dict
    content_hash: str
    file_path: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EvidencePayload:
    """The full evidence content returned by ``get``."""

    evidence_id: str
    evidence_type: str
    object_ref: dict
    content: bytes
    content_hash: str
    file_path: str = ""
    meta: dict = field(default_factory=dict)


@runtime_checkable
class EvidenceStore(Protocol):
    """The evidence-store contract Phase A implements / Phase B consumes."""

    def save(
        self,
        etype: str,
        object_ref: dict,
        *,
        content: bytes | None = None,
        path: str | None = None,
        meta: dict | None = None,
        now: datetime | None = None,
    ) -> EvidenceRef: ...

    def get(self, evidence_id: str) -> EvidencePayload: ...

    def list_by_object(self, object_ref: dict) -> list[EvidenceRef]: ...


def make_evidence_id(object_ref: dict, etype: str | None = None) -> str:
    """Derive a stable, reproducible evidence ID from the object reference.

    A deterministic ID (hash of the serialized ref + type) lets a re-save of
    the same logical object reuse the same identity instead of minting a
    fresh random UUID each time — important for idempotent re-runs.

    ``etype`` is part of the key: two different evidence types for the same
    object (e.g. ``source_file`` vs ``decision_snapshot`` for one batch) must
    get distinct IDs so the DB ``evidence`` table's append-only, one-row-per-
    (etype, object_ref) contract holds. ``FileEvidenceStore.save`` always
    passes the real ``etype``; a bare ``make_evidence_id(ref)`` call (P0
    skeleton tests) omits it and gets the ``"default"`` branch, preserving the
    old behavior for those callers.
    """
    key = f"{etype or 'default'}/{object_ref.get('kind', '')}/{object_ref.get('id', '')}"
    return uuid.UUID(hashlib.sha1(key.encode()).hexdigest()[:32]).hex[:36]


class FileEvidenceStore:
    """Local-directory evidence store with SHA-256 hashing.

    Phase-0 skeleton: ``save`` writes the file and records the in-memory
    reference; DB persistence is a Phase-A concern. The layout under
    ``root`` is ``{evidence_type}/{evidence_id}/{filename}`` — a stable,
    hash-keyed path that survives re-saves (总 spec §9.5).
    """

    def __init__(self, root: Path, db=None) -> None:
        self.root = Path(root)
        # ``db`` is reserved for Phase A's DB writes; Phase 0 leaves it None.
        self._db = db
        self._refs: list[EvidenceRef] = []
        self._by_id: dict[str, EvidencePayload] = {}

    def _sha256(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def save(
        self,
        etype: str,
        object_ref: dict,
        *,
        content: bytes | None = None,
        path: str | None = None,
        meta: dict | None = None,
        now: datetime | None = None,
    ) -> EvidenceRef:
        if content is None and path is None:
            raise ValueError("save requires content or path")
        evidence_id = make_evidence_id(object_ref, etype)
        meta = dict(meta or {})

        # Resolve the bytes to hash: inline content, or read an existing file.
        if content is not None:
            payload_bytes = content
            filename = path or f"{evidence_id}.bin"
        else:  # path only
            src = self.root / (path or "")
            payload_bytes = src.read_bytes()
            filename = Path(path).name if path else evidence_id

        # 总 spec §9.5 layout: evidence_type/{evidence_id}/{filename}
        target = self.root / etype / evidence_id / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload_bytes)

        digest = self._sha256(payload_bytes)
        rel = target.relative_to(self.root).as_posix()
        ref = EvidenceRef(
            evidence_id=evidence_id,
            evidence_type=etype,
            object_ref=dict(object_ref),
            content_hash=digest,
            file_path=rel,
            meta=meta,
        )
        self._refs.append(ref)
        self._by_id[evidence_id] = EvidencePayload(
            evidence_id=evidence_id,
            evidence_type=etype,
            object_ref=dict(object_ref),
            content=payload_bytes,
            content_hash=digest,
            file_path=rel,
            meta=meta,
        )
        # PA Task 4: DB half (append-only) when a live session was injected.
        if self._db is not None:
            self._insert_evidence_row(ref, now=now)
        return ref

    def _insert_evidence_row(self, ref: EvidenceRef, now: datetime | None = None) -> None:
        """INSERT the evidence row (check-then-insert ⇒ idempotent, append-only).

        ``make_evidence_id`` is deterministic on ``(etype, object_ref)``, so a
        re-save of the same logical object reuses the same PK: when the row
        already exists the insert is skipped entirely (ON CONFLICT DO NOTHING
        semantics, DDL-agnostic so it works on SQLite too). The row is never
        UPDATEd or DELETEd here (PA spec §5.8). Distinct evidence types for
        the same object get distinct IDs (``etype`` is part of the key), so
        each type's DB row lands independently rather than being silently
        swallowed by an earlier type's insert.

        The row stores the file pointer (``content_hash`` + ``file_path``)
        plus identity fields; the bytes themselves stay on disk, not in the
        JSONB ``content`` column (which is reserved for structured content).
        """
        db: Session = self._db
        generated_at = now or datetime.now(UTC)
        existing = db.scalars(
            select(EvidenceModel.evidence_id).where(
                EvidenceModel.evidence_id == uuid.UUID(ref.evidence_id)
            )
        ).first()
        if existing is not None:
            return
        db.add(
            EvidenceModel(
                evidence_id=uuid.UUID(ref.evidence_id),
                evidence_type=ref.evidence_type,
                object_ref=ref.object_ref,
                content_hash=ref.content_hash,
                file_path=ref.file_path,
                content=None,
                generated_at=generated_at,
                generated_by="system",
            )
        )
        db.flush()

    def get(self, evidence_id: str) -> EvidencePayload:
        if evidence_id not in self._by_id:
            raise KeyError(f"unknown evidence_id: {evidence_id}")
        return self._by_id[evidence_id]

    def list_by_object(self, object_ref: dict) -> list[EvidenceRef]:
        want = {k: object_ref.get(k) for k in ("kind", "id")}
        return [r for r in self._refs if all(r.object_ref.get(k) == v for k, v in want.items())]
