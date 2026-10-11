"""App factory wiring the batch / intake routers to the Task 7 main-chain graph.

``create_app`` returns a ready-to-serve FastAPI instance. Layering (spec
§3.2): FastAPI routes → graph orchestration → Skill / Plugin → domain pure
functions → SQLAlchemy. Routes never do business judgment themselves: they
marshal requests, run one request-scoped transaction (AGENT.md §4.3), and
translate skill/plugin exceptions into B3 responses (``{"detail": ...}``).

Dependency injection (spec §8): every external collaborator is injectable on
the factory so API tests run entirely on in-memory SQLite with a stub LLM /
evidence; production defaults wire the P0 fixture LLM client and the full
ISO 2859-1 table. Route definitions live in ``app.api`` (plan Task 8
Interfaces): ``app/api/batches.py`` (create / detail / judge) and
``app/api/intake.py`` (intake / confirm); this module keeps only the
factory, the per-request deps, and the module-level ``app``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.api.batches import build_router as build_batch_router
from app.api.intake import build_router as build_intake_router
from app.domain.types import IsoRow
from app.plugins.evidence_store import EvidenceStore, FileEvidenceStore
from app.plugins.llm_client import LLMClient, MockLLMClient
from app.workflow.checkpointer import get_checkpointer

__all__ = ["create_app", "app", "AppDeps"]

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
    """Build the app; deps are injectable (plan Task 8 Step 1–2).

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


#: Module-level default app for ``uvicorn app.main:app`` (memory checkpointer).
app = create_app()
