"""Unit tests for the checkpointer factory (PA Task 7, R2/B4)."""

from __future__ import annotations

import os
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from app.workflow.checkpointer import get_checkpointer


def test_memory_mode_returns_in_memory_saver() -> None:
    assert isinstance(get_checkpointer("memory"), InMemorySaver)
    assert isinstance(get_checkpointer(), InMemorySaver)  # default mode


def test_pg_mode_without_env_var_raises_lookup_error() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ZHL_DATABASE_URL", None)
        try:
            get_checkpointer("pg")
            raise AssertionError("expected LookupError")
        except LookupError:
            pass


def test_unknown_mode_raises_value_error() -> None:
    try:
        get_checkpointer("sqlite")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_pg_mode_invokes_from_conn_string_with_env_dsn() -> None:
    """B4/§4.0 contract: the pg factory must delegate verbatim to
    ``PostgresSaver.from_conn_string(ZHL_DATABASE_URL)`` and hand the caller
    the resulting context manager — no hard-coded DSN, no connection opened
    here (the CM is entered later by the caller)."""
    from contextlib import contextmanager

    from langgraph.checkpoint.postgres import PostgresSaver

    with patch.dict(os.environ, {"ZHL_DATABASE_URL": "postgresql://u:p@localhost:15432/zhl"}):
        calls: list[str] = []

        def _fake_from_conn_string(cls, conn_string, *, pipeline=False):
            calls.append(conn_string)

            # Mimic the real @contextmanager yield without opening a socket.
            @contextmanager
            def _cm():
                yield "fake-saver"

            return _cm()

        with patch.object(
            PostgresSaver,
            "from_conn_string",
            classmethod(lambda cls, cs, **kw: _fake_from_conn_string(cls, cs, **kw)),
        ):
            cm = get_checkpointer("pg")
            assert calls == [
                "postgresql://u:p@localhost:15432/zhl"
            ], "DSN must pass through verbatim"
            # CM is usable by the caller (B4: the caller holds it).
            with cm as saver:
                assert saver == "fake-saver"
