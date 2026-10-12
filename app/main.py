"""ZHL PV QC platform — unified app factory (Phase A + Phase B union).

Two app-factory variants ship side by side after the 2026-10-12 merge:

``create_app`` (Phase A, main): wires the batch / intake routers onto the
Task 7 main-chain graph with a full dependency-injection surface. Layering
(spec §3.2): FastAPI routes → graph orchestration → Skill / Plugin → domain
pure functions → SQLAlchemy. Routes never do business judgment themselves:
they marshal requests, run one request-scoped transaction (AGENT.md §4.3),
and translate skill/plugin exceptions into B3 responses
(``{"detail": ...}``). Every external collaborator is injectable on the
factory so API tests run entirely on in-memory SQLite with a stub LLM /
evidence; production defaults wire the P0 fixture LLM client and the full
ISO 2859-1 table. Route definitions live in ``app.api``: ``app/api/batches.py``
(create / detail / judge) and ``app/api/intake.py`` (intake / confirm).

``create_unified_app`` (Phase B entry): produces the production app that
serves **both** Phase A routers (batches / intake) **and** the Phase B
routers (incidents / reinspections / release / reports), mounts the static
directory (``app/static``), and adds the per-batch trace timeline endpoint
(PB Task 10, D1). The module-level ``app`` below is the unified variant so
``uvicorn app.main:app`` serves the full Phase A + B surface.

Phase B's ``create_app_with_trace`` lives in ``app.api.main`` (non-conflicting
module auto-merged into the worktree) and is re-exported here for any
Phase B consumer that imported it from ``app.main``.
"""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.api.batches import build_router as build_batch_router
from app.api.intake import build_router as build_intake_router
from app.api.main import create_app_with_trace as _phase_b_create_app_with_trace
from app.domain.types import IsoRow
from app.plugins.evidence_store import EvidenceStore, FileEvidenceStore
from app.plugins.llm_client import LLMClient, MockLLMClient
from app.workflow.checkpointer import get_checkpointer

__all__ = [
    "create_app",
    "create_unified_app",
    "create_app_with_trace",
    "app",
    "AppDeps",
]

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_FIXTURES = _ROOT / "tests" / "fixtures"
_DEFAULT_EVIDENCE_ROOT = _ROOT / "data" / "evidence"
_DEFAULT_ISO_PATH = _DEFAULT_FIXTURES / "iso2859_1_aql.json"

# ---------------------------------------------------------------------------
# Per-request dependency container (1 request == 1 transaction, §4.3)
# ---------------------------------------------------------------------------


class AppDeps:
    """One request's environment: session + evidence + repos + llm + graph deps."""

    def __init__(
        self,
        session: object,
        evidence: EvidenceStore,
        inspect_repo: object,
        llm: LLMClient,
        iso_table: list[IsoRow],
        checkpointer: object,
    ) -> None:
        self.session = session
        self.evidence = evidence
        self.inspect_repo = inspect_repo
        self.llm = llm
        self.iso_table = iso_table
        self.checkpointer = checkpointer


def _default_iso_table() -> list[IsoRow]:
    rows = json.loads(_DEFAULT_ISO_PATH.read_text(encoding="utf-8"))["rows"]
    return [IsoRow(**row) for row in rows]


def create_app(
    checkpointer_mode: str = "memory",
    session_factory: sessionmaker | None = None,
    llm: LLMClient | None = None,
    evidence: EvidenceStore | None = None,
    iso_table: list[IsoRow] | None = None,
    inspect_repo: object | None = None,
) -> FastAPI:
    """Build the Phase A app; deps are injectable (plan Task 8 Step 1–2).

    ``session_factory=None`` defers the real repository factory to request
    time (so importing ``app.main`` never requires a live database URL —
    the module-level ``app`` below is import-safe; production rebinds a
    concrete factory). Checkpointer is built eagerly (memory mode is
    side-effect-free; pg mode requires ``ZHL_DATABASE_URL``).
    """
    app = FastAPI(title="ZHL Phase A")

    checkpointer = get_checkpointer(checkpointer_mode)
    llm_client = llm or MockLLMClient(_DEFAULT_FIXTURES)
    table = iso_table if iso_table is not None else _default_iso_table()
    default_evidence = evidence
    default_repo = inspect_repo

    def get_deps() -> AppDeps:
        from app.infra.repositories import InspectionRepository, get_session_factory

        factory = session_factory
        if factory is None:
            factory = get_session_factory()
        session = factory()
        ev = (
            default_evidence
            if default_evidence is not None
            else FileEvidenceStore(_DEFAULT_EVIDENCE_ROOT, db=session)
        )
        repo = default_repo if default_repo is not None else InspectionRepository()
        return AppDeps(session, ev, repo, llm_client, table, checkpointer)

    app.include_router(build_batch_router(get_deps))
    app.include_router(build_intake_router(get_deps))
    return app


def create_unified_app(
    session_factory: sessionmaker | None = None,
    checkpointer_mode: str = "memory",
    mount_static: bool = True,
) -> FastAPI:
    """Phase A + Phase B unified app.

    Base app is the Phase B trace factory (incidents / reinspections /
    release / reports + static + D8 archived terminal guard + trace
    endpoint); the Phase A routers (batches / intake) are included on top
    so one app serves the full surface. Phase A's AppDeps closure keeps
    its own session/evidence/repo wiring (``build_router(get_deps)``),
    independent of Phase B's per-router session overrides — both are
    per-request session factories, so there is no shared session state.

    ``session_factory`` is threaded into the Phase B factory (Postgres via
    ``ZHL_DATABASE_URL`` when ``None`` — fail-fast, no silent SQLite).
    """
    unified = _phase_b_create_app_with_trace(
        session_factory=session_factory, mount_static=mount_static
    )

    # Phase A: build its router set with its own get_deps closure and mount
    # it onto the unified app. checkpointer_mode only affects Phase A's
    # main-chain skill runs (memory is the import-safe default).
    checkpointer = get_checkpointer(checkpointer_mode)
    llm_client = MockLLMClient(_DEFAULT_FIXTURES)
    table = _default_iso_table()
    default_repo: object | None = None

    def get_deps() -> AppDeps:
        from app.infra.repositories import InspectionRepository, get_session_factory

        factory = get_session_factory()
        session = factory()
        ev = FileEvidenceStore(_DEFAULT_EVIDENCE_ROOT, db=session)
        repo = default_repo if default_repo is not None else InspectionRepository()
        return AppDeps(session, ev, repo, llm_client, table, checkpointer)

    unified.include_router(build_batch_router(get_deps))
    unified.include_router(build_intake_router(get_deps))
    return unified


def create_app_with_trace(
    session_factory: sessionmaker | None = None,
    mount_static: bool = True,
) -> FastAPI:
    """Re-export of Phase B's trace-enabled factory (backward compat)."""
    return _phase_b_create_app_with_trace(
        session_factory=session_factory, mount_static=mount_static
    )


# Load .env before building the module-level app so ZHL_DATABASE_URL
# resolves to the project's Postgres URL. Without this the Phase B
# factory's fail-fast check (app/api/main.py _default_postgres_session)
# raises on a missing env var instead of silently creating an empty
# SQLite DB.
load_dotenv()

#: Module-level default app for ``uvicorn app.main:app`` — serves both
#: Phase A and Phase B routers + static pages + trace endpoint.
app = create_unified_app()
