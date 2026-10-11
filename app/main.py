"""Production entry point: ``uv run uvicorn app.main:app --reload``.

AGENT.md §5 demo command; the app factory lives in ``app.api.main`` and
this module is the one-line alias uvicorn expects.
"""

from dotenv import load_dotenv

from app.api.main import create_app_with_trace

# Load .env before the app factory so ZHL_DATABASE_URL resolves to the
# project's Postgres URL. Without this the factory's fail-fast check
# (app/api/main.py _default_postgres_session) raises on a missing env var
# instead of silently creating an empty SQLite DB.
load_dotenv()

# The trace read endpoint (GET /api/batches/{id}/traces) is needed by
# app/static/trace.html; use the trace variant as the production app.
app = create_app_with_trace()

__all__ = ["app"]
