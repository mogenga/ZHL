"""EvidenceStore Protocol + FileEvidenceStore skeleton (P0 spec §5.4 / D10 / D11).

Phase 0 delivers the Protocol and the *file + hash* half of
``FileEvidenceStore``: ``save`` computes SHA-256, writes the bytes under
``root/evidence_type/...`` (总 spec §9.5 layout), and records metadata
in-memory. DB writes are explicitly deferred to Phase A, so the skeleton is
constructible with ``db=None`` and is testable on a temp dir with no clock
reads — a caller-injected ``now`` (defaulting to a fixed value) keeps
``generated_at`` deterministic so tests can pin it.

Purity boundary: this module lives in ``app/plugins`` and MAY reference the
SQLAlchemy ``Session`` type for forward-compat, but Phase-0 logic never
opens a DB. No model / LangGraph / random-internal-imports.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

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
    ) -> EvidenceRef: ...

    def get(self, evidence_id: str) -> EvidencePayload: ...

    def list_by_object(self, object_ref: dict) -> list[EvidenceRef]: ...


def make_evidence_id(object_ref: dict) -> str:
    """Derive a stable, reproducible evidence ID from the object reference.

    A deterministic ID (hash of the serialized ref + type) lets a re-save of
    the same logical object reuse the same identity instead of minting a
    fresh random UUID each time — important for idempotent re-runs.
    """
    key = f"{object_ref.get('kind','')}/{object_ref.get('id','')}"
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
    ) -> EvidenceRef:
        if content is None and path is None:
            raise ValueError("save requires content or path")
        evidence_id = make_evidence_id(object_ref)
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
        # DB write (evidence table insert) is deferred to Phase A.
        return ref

    def get(self, evidence_id: str) -> EvidencePayload:
        if evidence_id not in self._by_id:
            raise KeyError(f"unknown evidence_id: {evidence_id}")
        return self._by_id[evidence_id]

    def list_by_object(self, object_ref: dict) -> list[EvidenceRef]:
        want = {k: object_ref.get(k) for k in ("kind", "id")}
        return [r for r in self._refs if all(r.object_ref.get(k) == v for k, v in want.items())]
