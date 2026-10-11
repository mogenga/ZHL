"""Checkpointer factory for the main-chain graph (PA spec §5.5 / R2, B4).

``get_checkpointer(mode)`` returns the LangGraph Checkpointer used to make
the graph resumable:

- ``"memory"`` -> ``InMemorySaver()`` (graph-replay tests, demos, and
  any integration that must NOT depend on a live Postgres — R2).
- ``"pg"``     -> ``PostgresSaver.from_conn_string(ZHL_DATABASE_URL)``.
  The factory returns the *context manager* (``from_conn_string`` is a
  context manager); the caller owns it (B4: the caller holds the CM, not
  this factory — we just surface it).

The connection string is read from ``ZHL_DATABASE_URL``; missing value
raises ``LookupError`` (no silent fallback to a default DSN — AGENT.md
§4.0: configuration lives in env vars, never in code).
"""

from __future__ import annotations

import os

from langgraph.checkpoint.memory import InMemorySaver

#: R2/B4: the pg checkpointer is only used in production; tests and demos
#: inject InMemorySaver via ``build_main_chain_graph(..., checkpointer=...)``.
DEFAULT_MODE = "memory"


def get_checkpointer(mode: str = "memory"):
    """Build the checkpointer for ``mode``.

    Returns the checkpointer object, or — for ``"pg"`` — the context
    manager that yields it (callers own it, B4:
    ``with get_checkpointer("pg") as saver: ...``).
    """
    if mode == "memory":
        return InMemorySaver()
    if mode == "pg":
        conn_string = os.environ.get("ZHL_DATABASE_URL")
        if not conn_string:
            raise LookupError(
                "ZHL_DATABASE_URL is not set; the pg checkpointer requires it "
                "(AGENT.md §4.0 — no default DSN fallback)"
            )
        from langgraph.checkpoint.postgres import PostgresSaver

        return PostgresSaver.from_conn_string(conn_string)
    raise ValueError(f"unknown checkpointer mode: {mode!r} (expected 'memory' or 'pg')")


__all__ = ["get_checkpointer", "DEFAULT_MODE"]
