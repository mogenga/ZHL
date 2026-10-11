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
