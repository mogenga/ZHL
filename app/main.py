"""Production entry point: ``uv run uvicorn app.main:app --reload``.

AGENT.md §5 demo command; the app factory lives in ``app.api.main`` and
this module is the one-line alias uvicorn expects.
"""

from app.api.main import create_app_with_trace

# The trace read endpoint (GET /api/batches/{id}/traces) is needed by
# app/static/trace.html; use the trace variant as the production app.
app = create_app_with_trace()

__all__ = ["app"]
