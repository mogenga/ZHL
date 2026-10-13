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
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from app.api import incidents, reinspections, release, reports, rule_versions
from app.api.guards import ARCHIVED_ERROR, require_not_archived
from app.infra.models import Base

#: Static dir (frontend pages). parents[2] = repo root, app/static relative.
_STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "static"


def get_session() -> Any:
    """The app-level session dependency.

    Routers define their own ``get_session`` stub (which raises), and the
    tests override *that* stub per-router. The app-level ``get_session`` is
    the real factory used when no override is present (production). It is
    exposed here so ``create_app`` can wire it and so the smoke test imports
    it as the single source of truth.
    """
    raise RuntimeError("get_session not wired — create_app() overrides this")


def create_app(
    session_factory: Callable[[], Any] | None = None,
    mount_static: bool = True,
    own_session: bool = True,
) -> FastAPI:
    """Build the Phase B FastAPI app.

    - ``session_factory``: a zero-arg callable returning a fresh request
      session. When ``None`` a Postgres engine from ``ZHL_DATABASE_URL`` is
      used (production); tests pass a SQLite one.
    - ``mount_static``: when True and ``app/static`` exists, mount the static
      dir at ``/static`` + a ``GET /`` redirect to ``/static/index.html``.
      Tests can pass ``mount_static=False``; an absent dir is also skipped.
    - ``own_session``: True (production default) closes the request-scoped
      session after each request (C1). False (tests inject a *shared*
      long-lived session via ``session_factory=lambda: session``) skips the
      close; only rollback on error.
    """
    if session_factory is None:
        session_factory = _default_postgres_session()

    app = FastAPI(title="ZHL Solar Module QC", version="0.2.0")

    # Wire the real session into every router's get_session stub.
    # Each router module defines its own get_session (a stub that raises);
    # the app overrides it with a request-scoped *generator* dependency
    # built from the session factory. FastAPI consumes the generator per
    # request and runs the teardown after the route returns or raises:
    # rollback on error (never leave a dirty transaction) + close when
    # ``own_session`` (factory mints a fresh session per request, i.e.
    # the production ``sessionmaker`` path — C1: no leaked connections).
    #
    # Tests / E2E inject a *shared* long-lived session via
    # ``session_factory=lambda: session`` and pass ``own_session=False``
    # so the generator must NOT close it.
    def _open_session():
        s = session_factory()
        try:
            yield s
        except Exception:
            try:
                s.rollback()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
            raise
        finally:
            if own_session:
                s.close()

    # The trace router (create_app_with_trace) depends on the app-level
    # get_session, so the live generator must be wired onto that stub too.
    # (Wiring it onto a router stub alone left it a no-op for the trace route.)
    for module in (incidents, reinspections, release, reports, rule_versions):
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
    # 规则版本发现（只读，供前端工作台下拉选择，避免人肉粘贴 UUID）。
    app.include_router(rule_versions.router)

    # 挂载静态目录（前端页面，Task 10）。只在该开关打开且目录存在时生效，
    # 测试可通过 mount_static=False 跳过。
    if mount_static and _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def _root_redirect() -> RedirectResponse:
            """``/`` → 前端首页（index.html）。"""
            return RedirectResponse(url="/static/index.html", status_code=302)

    return app


def _default_postgres_session() -> Callable[[], Any]:
    # Fail fast: no silent SQLite fallback (an empty ./zhl.db created by
    # Base.metadata.create_all masks a missing/unread .env and makes every
    # API call return 404 against an empty DB). The caller must set
    # ZHL_DATABASE_URL (a Postgres URL from .env / the environment); the
    # test path never reaches here — it injects a SQLite session via
    # dependency_overrides.
    url = os.environ.get("ZHL_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "ZHL_DATABASE_URL is not set; configure .env (loaded by app.main) "
            "or the environment. Refusing to fall back to a silent SQLite DB."
        )
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    return factory


def create_app_with_trace(
    session_factory: Callable[[], Any] | None = None,
    mount_static: bool = True,
    own_session: bool = True,
) -> FastAPI:
    """``create_app`` + a read-only per-batch trace timeline (PB Task 10, D1).

    The 4 static pages fetch a batch's ``agent_task_traces``; this is the
    minimal read endpoint that exposes ``TraceStore.list_for_batch`` over
    HTTP so ``trace.html`` renders the trajectory layer without a DB client.
    """
    from app.plugins.trace_store import TraceStore

    app = create_app(
        session_factory=session_factory,
        mount_static=mount_static,
        own_session=own_session,
    )

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
