"""Production entry point: ``uv run uvicorn app.main:app --reload``.

AGENT.md §5 demo command; the app factory lives in ``app.api.main`` and
this module is the one-line alias uvicorn expects.
"""

from app.api.main import create_app

app = create_app()

__all__ = ["app"]
