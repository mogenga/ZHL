"""C1 regression: production session must be closed after every request.

``create_app`` (``app/api/main.py``) wires a request-scoped *generator*
dependency (``_open_session``) onto every router's ``get_session`` stub.
With the production default ``own_session=True`` the generator must
``close()`` the session in its ``finally`` block after each request so a
Postgres long-lived connection pool is not leaked; on error it must also
``rollback()`` so a dirty transaction never survives the request.

Before the fix the generator was a bare closure that never closed the
session ("teardown is a no-op"), so every request leaked a connection and
an in-request exception left a half-rolled-back transaction open.

These tests use a counting fake session factory to assert the open/close
event sequence across two requests, exactly as the review plan requires.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.api.main import create_app


class _FakeSession:
    """Records open/close/rollback events so the test can assert the order."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        log.append("open")

    def close(self) -> None:
        self._log.append("close")

    def rollback(self) -> None:
        self._log.append("rollback")

    def commit(self) -> None:
        self._log.append("commit")

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def query(self, *args: Any, **kwargs: Any) -> Any:
        class _Q:
            def order_by(self, *a, **k):
                return self

            def all(self):
                return []

        return _Q()

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _app_with_tracking() -> tuple[TestClient, list[str]]:
    log: list[str] = []

    def factory() -> _FakeSession:
        return _FakeSession(log)

    client = TestClient(create_app(session_factory=factory, mount_static=False))
    return client, log


def test_session_closed_after_every_request() -> None:
    """C1: two sequential requests -> open/close per request, no leak."""
    client, log = _app_with_tracking()

    # GET /api/quality-rule-versions is read-only and needs no rows; the
    # fake session returns None for everything but the route still runs.
    r1 = client.get("/api/quality-rule-versions")
    r2 = client.get("/api/quality-rule-versions")
    # Either 200 (route wired) or a 500 from the fake session returning
    # None where a real row is expected — both prove the request ran and
    # the generator's finally-block teardown fired. We assert on the
    # session lifecycle, not the HTTP status.
    assert r1.status_code in (200, 500, 503)
    assert r2.status_code in (200, 500, 503)
    # Each request must open exactly one session and close it again:
    # the event sequence is open/close per request.
    assert log == ["open", "close", "open", "close"], f"unexpected sequence: {log}"


def test_shared_session_not_closed_when_own_session_false() -> None:
    """C1 complement: tests / E2E inject one shared long-lived session via
    ``session_factory=lambda: session`` and ``own_session=False``; the
    generator must NOT close it (closing would break subsequent
    ``session.get()`` reads in the same test)."""
    events: list[str] = []

    class _Shared:
        def close(self) -> None:
            events.append("close")

        def rollback(self) -> None:
            events.append("rollback")

        def get(self, *a: Any, **k: Any) -> Any:
            return None

        def query(self, *a: Any, **k: Any) -> Any:
            class _Q:
                def order_by(self, *aa, **kk):
                    return self

                def all(self):
                    return []

            return _Q()

    shared = _Shared()

    def factory() -> Any:
        return shared

    client = TestClient(create_app(session_factory=factory, mount_static=False, own_session=False))
    r = client.get("/api/quality-rule-versions")
    assert r.status_code in (200, 500, 503)
    # The shared session must NOT have been closed by the request teardown.
    assert events == [], f"own_session=False must not close the shared session: {events}"
