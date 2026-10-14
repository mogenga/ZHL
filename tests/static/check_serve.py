#!/usr/bin/env -S uv run python
"""Verify the workbench page is served correctly by the static mount.

Machine-checked half of T1's browser acceptance: proves the FastAPI
``/static`` mount serves ``workbench.html`` + the three sub-assets, and that
the page's structural invariants hold (container ids, four view tabs,
script load order, no native dialogs). In-browser DOM behavior (view
switching, URL restore) is exercised by the dev at final acceptance.

Run: uv run python tests/static/check_serve.py
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app as create_phase_b_app
from app.infra.models import Base


def _client() -> TestClient:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    return TestClient(create_phase_b_app(session_factory=lambda: factory()))


def check_page() -> None:
    client = _client()
    r = client.get("/static/workbench.html")
    assert r.status_code == 200, f"workbench.html {r.status_code}"
    html = r.text

    for marker in [
        'id="leftPane"',
        'id="mainPane"',
        'id="rightPane"',
        'data-view="batch"',
        'data-view="rules"',
        'data-view="incidents"',
        'data-view="reports"',
        'src="/static/wb/ui.js"',
        'src="/static/wb/main.js"',
    ]:
        assert marker in html, f"missing {marker}"

    assert html.index('src="/static/wb/ui.js"') < html.index(
        'src="/static/wb/main.js"'
    ), "ui.js must load before main.js"

    for sub in ["/static/wb.css", "/static/wb/ui.js", "/static/wb/main.js"]:
        assert client.get(sub).status_code == 200, f"{sub} not served"

    for sub in ["/static/workbench.html", "/static/wb/ui.js", "/static/wb/main.js"]:
        body = client.get(sub).text
        for banned in ("alert(", "prompt(", "confirm("):
            assert banned not in body, f"{sub} contains {banned}"

    print("OK: workbench.html served + structural invariants hold")


if __name__ == "__main__":
    check_page()
    print("T1 serve-check passed")
