"""Repository layer: batch / inspection / sample / evidence CRUD.

Phase A data-layer twin of the P0-frozen models. All business rules live in
``app/domain`` (frozen); these repositories only translate between request
parameters and the ORM, and enforce the app-layer fallbacks that the DDL
cannot (or the test DB does not) guarantee on their own — most notably the
cross-batch SN uniqueness guard that mirrors the Postgres EXCLUDE constraint
(D5/D17) on databases where it is not active (e.g. SQLite in tests).

Session lifetime: repositories never commit; the caller owns the
transaction (AGENT.md §4.3 — one call == one transaction boundary).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.sampling import freeze_sample
from app.domain.types import IsoRow
from app.infra.models import (
    DeliveryBatch,
    ModuleInspection,
    Sample,
    SampleConclusion,
)


class CrossBatchSnConflict(Exception):
    """App-layer guard for the DDL EXCLUDE constraint (P0 spec D5/D17).

    Carries the conflicting ``sn`` as a structured attribute（§8.1 自动创建
    data_consistency 异常依赖它，避免调用方解析消息文本——审查 M-1）。

    The Postgres schema rejects the same SN across two batches at the DDL
    layer; on SQLite (and any engine without the EXCLUDE constraint) the
    repository performs the equivalent query before inserting.
    """

    def __init__(self, message: str, sn: str | None = None) -> None:
        super().__init__(message)
        self.sn = sn


class BatchRepository:
    """CRUD for ``delivery_batches``."""

    def create_batch(
        self,
        session: Session,
        batch_no: str,
        project_id: str,
        model: str,
        rule_version_id: uuid.UUID,
        planned_quantity: int,
    ) -> DeliveryBatch:
        """Insert a new draft batch; status transitions are the caller's job."""
        batch = DeliveryBatch(
            batch_no=batch_no,
            project_id=project_id,
            model=model,
            planned_quantity=planned_quantity,
            rule_version_id=rule_version_id,
        )
        session.add(batch)
        session.flush()
        return batch

    def get_batch(self, session: Session, batch_id: uuid.UUID) -> DeliveryBatch | None:
        return session.get(DeliveryBatch, batch_id)


class InspectionRepository:
    """CRUD for ``module_inspections`` with the cross-batch SN fallback."""

    def create_inspections(
        self,
        session: Session,
        batch_id: uuid.UUID,
        rows: list[dict],
    ) -> list[ModuleInspection]:
        """Insert inspection rows for ``batch_id``.

        ``rows`` carry the electrical parameters + SN + source metadata.
        Before flushing, every SN is checked against other batches
        (the DDL EXCLUDE fallback, D5/D17): if any SN already appears in a
        different batch the whole call is rejected.
        """
        if not rows:
            return []
        sns = [row["sn"] for row in rows]
        self._assert_no_cross_batch_conflict(session, batch_id, sns)
        created: list[ModuleInspection] = []
        for row in rows:
            inspection = ModuleInspection(
                batch_id=batch_id,
                sn=row["sn"],
                source_type=row["source_type"],
                captured_at=row["captured_at"],
                pmax=row["pmax"],
                temp=row["temp"],
                isc=row["isc"],
                voc=row["voc"],
                vm=row["vm"],
                im=row["im"],
                ff=row["ff"],
                appearance_result=row["appearance_result"],
            )
            session.add(inspection)
            created.append(inspection)
        session.flush()
        return created

    def get_batch_sns(self, session: Session, batch_id: uuid.UUID) -> list[str]:
        stmt = select(ModuleInspection.sn).where(ModuleInspection.batch_id == batch_id)
        return list(session.scalars(stmt).all())

    def confirm_inspection(
        self,
        session: Session,
        inspection_id: uuid.UUID,
        fields: dict | None,
    ) -> ModuleInspection:
        """Confirm an inspection. D7: original measured columns are locked.

        The Postgres DDL revokes UPDATE on the measured columns for the app
        role (D7 / AGENT.md §8: original inspection values stay immutable so
        the evidence chain remains auditable). We keep that semantics uniform
        across engines: confirming only flips ``intake_status`` to
        ``confirmed``; any ``fields`` passed in are NEVER written back to the
        measured columns (pmax / temp / isc / voc / vm / im / ff /
        appearance_result).

        ``fields`` is retained in the signature (the API layer may still pass
        a human-proposed override); it is used for whitelist validation /
        audit only, not applied. Unknown keys are rejected by the API-layer
        ``_CONFIRMABLE`` guard before reaching here.

        ``fields`` is intentionally not applied in the body (it is the audit
        surface; writing it back would violate D7). It stays in the signature
        so callers / API routes keep their current call shape.
        """
        _ = fields  # audit-only; measured columns stay immutable (D7).
        inspection = session.get(ModuleInspection, inspection_id)
        if inspection is None:
            raise LookupError(f"unknown inspection_id: {inspection_id}")
        # D7: do NOT setattr the measured columns — original values are
        # immutable. The fields dict is accepted but deliberately not applied
        # (audit surface only; the API layer already whitelists via
        # ``_CONFIRMABLE``).
        inspection.intake_status = "confirmed"
        session.flush()
        return inspection

    def reject_inspection(self, session: Session, inspection_id: uuid.UUID) -> ModuleInspection:
        inspection = session.get(ModuleInspection, inspection_id)
        if inspection is None:
            raise LookupError(f"unknown inspection_id: {inspection_id}")
        inspection.intake_status = "rejected"
        session.flush()
        return inspection

    @staticmethod
    def _assert_no_cross_batch_conflict(
        session: Session, batch_id: uuid.UUID, sns: list[str]
    ) -> None:
        """The EXCLUDE fallback: reject if any SN exists in another batch."""
        stmt = (
            select(ModuleInspection.sn)
            .where(ModuleInspection.sn.in_(sns))
            .where(ModuleInspection.batch_id != batch_id)
            .limit(1)
        )
        conflict = session.scalars(stmt).first()
        if conflict is not None:
            raise CrossBatchSnConflict(
                f"SN {conflict!r} already recorded in a different batch", sn=conflict
            )


# Measured-column whitelist (D7 audit surface). The Postgres DDL revokes UPDATE
# on these columns for the app role; confirming never writes them back
# (``confirm_inspection`` is audit-only over ``fields``). This set documents the
# field names the API layer may surface to a human confirmer and validate; it is
# NOT applied to the row.
_CONFIRMABLE_FIELDS = frozenset(
    {
        "pmax",
        "temp",
        "isc",
        "voc",
        "vm",
        "im",
        "ff",
        "appearance_result",
    }
)


class SampleRepository:
    """CRUD for ``samples`` / ``sample_conclusions`` + ISO table reads."""

    def freeze_samples(
        self,
        session: Session,
        batch_id: uuid.UUID,
        inspection_type: str,
        rule_version_id: uuid.UUID,
        sns: list[str],
        seed: str,
        selected_at: datetime,
        iso_row: IsoRow,
    ) -> list[Sample]:
        """Run the domain ``freeze_sample`` and persist the frozen selection.

        The seed is the batch-id string (P0 spec §9.4): identical inputs
        always reproduce the identical selection, so re-freezing the same
        batch with the same seed is a no-op on the data (the UNIQUE
        constraint on the sample table is the backstop).

        ``iso_row`` is the resolved ISO table row (n/ac/re) already looked
        up by the sampling plugin (Task 6); the repository stores only the
        ``n`` portion of it via the selection count, and trusts the caller
        for ``ac``/``re`` (which belong to the conclusion, not the sample).
        """
        from sqlalchemy import select as _sel

        from app.domain.types import IsoResult

        iso_result = IsoResult(n=iso_row.n, ac=iso_row.ac, re=iso_row.re)
        entries = freeze_sample(str(batch_id), len(sns), iso_result, sns, seed)
        selected = [e.sn for e in entries]
        existing_stmt = _sel(Sample.sn).where(Sample.batch_id == batch_id)
        existing = set(session.scalars(existing_stmt).all())
        created: list[Sample] = []
        for sn in selected:
            if sn in existing:
                continue
            sample = Sample(
                batch_id=batch_id,
                inspection_type=inspection_type,
                rule_version_id=rule_version_id,
                sn=sn,
                seed=seed,
                selected_at=selected_at,
            )
            session.add(sample)
            created.append(sample)
        session.flush()
        return created

    def save_conclusion(
        self,
        session: Session,
        batch_id: uuid.UUID,
        inspection_type: str,
        rule_version_id: uuid.UUID,
        sample_round: int,
        n: int,
        ac: int,
        re: int,
        result: str,
    ) -> SampleConclusion:
        conclusion = SampleConclusion(
            batch_id=batch_id,
            inspection_type=inspection_type,
            rule_version_id=rule_version_id,
            sample_round=sample_round,
            n=n,
            ac=ac,
            re=re,
            result=result,
        )
        session.add(conclusion)
        session.flush()
        return conclusion

    def get_latest_conclusion(
        self,
        session: Session,
        batch_id: uuid.UUID,
        inspection_type: str,
    ) -> SampleConclusion | None:
        """Latest conclusion for ``batch_id`` scoped to one inspection type.

        ``inspection_type`` is required: calling without it (the old
        default-``None`` behavior) would compare across inspection types and
        could return a conclusion from the wrong one — no caller relies on
        that cross-type behavior, so the parameter is not optional."""
        stmt = select(SampleConclusion).where(
            SampleConclusion.batch_id == batch_id,
            SampleConclusion.inspection_type == inspection_type,
        )
        stmt = stmt.order_by(SampleConclusion.sample_round.desc())
        return session.scalars(stmt).first()


# ---------------------------------------------------------------------------
# Engine / session plumbing
# ---------------------------------------------------------------------------


def _resolve_database_url() -> str:
    """``ZHL_DATABASE_URL`` env var, falling back to the project-root .env.

    C11: falls back to python-dotenv (already a project dependency, see
    ``app/main.py``) instead of a hand-rolled .env parser.
    """
    url = os.environ.get("ZHL_DATABASE_URL")
    if not url:
        from dotenv import load_dotenv

        load_dotenv()
        url = os.environ.get("ZHL_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "No database URL. Set ZHL_DATABASE_URL in .env "
            "(see .env.example) before using the app-layer repositories."
        )
    return url.strip()


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    """Lazily-built shared engine (one per process, P0 spec §7.2)."""
    global _engine
    if _engine is None:
        _engine = create_engine(_resolve_database_url())
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """One call == one transaction: commit on success, rollback on error."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
