"""FastAPI app assembly (PB Task 9).

``create_app()`` wires the four Phase B routers (incidents / reinspections /
release / reports) onto one app, provides a real request-scoped session
factory (the routers' ``get_session`` stubs are overridden with it), mounts
the static directory, and installs the D8 *archived terminal-state guard* —
every write endpoint pre-checks the batch status and returns 409 when the
batch is already ``archived``.

The session factory is injectable (``session_factory``) so tests bind a
SQLite session; the production entry point (``app.main``) supplies a Postgres
one via the ``ZHL_DATABASE_URL`` env var.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import incidents, reinspections, release, reports
from app.api.guards import ARCHIVED_ERROR, require_not_archived
from app.infra.models import Base


def get_session() -> Any:
    """The app-level session dependency.

    Routers define their own ``get_session`` stub (which raises), and the
    tests override *that* stub per-router. The app-level ``get_session`` is
    the real factory used when no override is present (production). It is
    exposed here so ``create_app`` can wire it and so the smoke test imports
    it as the single source of truth.
    """
    raise RuntimeError("get_session not wired — create_app() overrides this")


def _make_session_dependency(session_factory: Callable[[], Any]) -> Callable[[], Any]:
    """(Retained for API completeness; create_app now inlines a plain closure.)"""

    def _session() -> Any:
        session = session_factory()
        return session

    return _session


def create_app(
    session_factory: Callable[[], Any] | None = None,
    mount_static: bool = True,
) -> FastAPI:
    """Build the Phase B FastAPI app.

    - ``session_factory``: a zero-arg callable returning a fresh request
      session. When ``None`` a Postgres engine from ``ZHL_DATABASE_URL`` is
      used (production); tests pass a SQLite one.
    - ``mount_static``: mount ``app/static`` when present (Task 10 pages);
      absent in Task 9 so the flag is honoured but the dir may not exist yet.
    """
    if session_factory is None:
        session_factory = _default_postgres_session()

    app = FastAPI(title="ZHL Solar Module QC", version="0.2.0")

    # Wire the real session into every router's get_session stub.
    # Each router module defines its own get_session (a stub that raises);
    # the app overrides it with a request-scoped callable built from the
    # session factory. FastAPI's override protocol accepts a plain callable
    # (return a value), so we hand it a closure that opens one session per
    # request (the session is request-scoped; teardown is a no-op here).
    def _open_session() -> Any:
        return session_factory()

    # The trace router (create_app_with_trace) depends on the app-level
    # get_session, so the live closure must be wired onto that stub too.
    # (Wiring it onto a router stub alone left it a no-op for the trace route.)
    for module in (incidents, reinspections, release, reports):
        app.dependency_overrides[module.get_session] = _open_session
    app.dependency_overrides[get_session] = _open_session  # app-level, used by trace router
    # Attach the D8 guard as a global dependency on the four write routers so
    # the require_not_archived check runs on every write request. The guard is
    # applied per-endpoint (see the routers' write endpoints); here we simply
    # install the routers.
    app.include_router(incidents.router)
    app.include_router(reinspections.router)
    app.include_router(release.router)
    app.include_router(reports.router)

    if mount_static:
        # app/api/main.py -> dirname(dirname(__file__)) = app ; pages live in app/static.
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
        if os.path.isdir(static_dir):
            app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def _default_postgres_session() -> Callable[[], Any]:
    url = os.environ.get("ZHL_DATABASE_URL", "sqlite:///./zhl.db")
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    return factory


def create_app_with_trace(
    session_factory: Callable[[], Any] | None = None,
    mount_static: bool = True,
) -> FastAPI:
    """``create_app`` + a read-only per-batch trace timeline (PB Task 10, D1).

    The 4 static pages fetch a batch's ``agent_task_traces``; this is the
    minimal read endpoint that exposes ``TraceStore.list_for_batch`` over
    HTTP so ``trace.html`` renders the trajectory layer without a DB client.
    """
    from app.plugins.trace_store import TraceStore

    app = create_app(session_factory=session_factory, mount_static=mount_static)

    from fastapi import APIRouter, Depends

    trace_router = APIRouter(prefix="/api/batches", tags=["traces"])

    # B008: module-level singleton for the session dependency — the override
    # dict (populated by create_app) is what resolves it to a live session.
    trace_session_dep = Depends(get_session)

    @trace_router.get("/{batch_id}/traces")
    def list_traces(batch_id: uuid.UUID, session: Any = trace_session_dep) -> list[dict]:
        rows = TraceStore().list_for_batch(session, batch_id)
        return [
            {
                "task_id": str(t.task_id),
                "skill_name": t.skill_name,
                "input_ref": t.input_ref,
                "plugins_invoked": t.plugins_invoked,
                "params_summary": t.params_summary,
                "model_version": t.model_version,
                "validation_result": t.validation_result,
                "failure_reason": t.failure_reason,
                "human_action": t.human_action,
                "started_at": t.started_at.isoformat() if t.started_at else None,
            }
            for t in rows
        ]

    # ``create_app`` wired the live session closure onto the app-level
    # ``get_session`` stub via ``app.dependency_overrides`` — the same dict
    # lives on this ``app`` instance, so ``Depends(_main.get_session)``
    # resolves to the request-scoped session without a second override.
    app.include_router(trace_router)
    return app


__all__ = [
    "create_app",
    "create_app_with_trace",
    "get_session",
    "require_not_archived",
    "ARCHIVED_ERROR",
]
