"""Deprecation guard: ``datetime.utcnow()`` must not appear in the ORM defaults.

Phase A close-out ruling (user-confirmed): ``app/infra/models.py`` is a P0
frozen file whose ``_ts`` helper used ``datetime.utcnow()`` — a naive-UTC
default flagged by 500+ ``DeprecationWarning``s per full test run. The fix
is a one-line change to ``datetime.now(timezone.utc)``; this test RED-first
pins that no model default calls ``utcnow`` (a recurrence fails this
suite even with the warning filters off).
"""

from __future__ import annotations

import inspect
from datetime import UTC

from app.infra import models


def test_no_model_default_calls_utcnow() -> None:
    """No ``default`` callable on any mapped column may reference utcnow."""
    offenders = []
    src = inspect.getsource(models)
    if "utcnow" in src:
        offenders.append("app/infra/models.py source still references utcnow")
    assert offenders == [], f"utcnow still present: {offenders}"


def test_ts_column_defaults_produce_aware_datetimes() -> None:
    """The ``_ts`` helper's default must yield timezone-aware datetimes."""

    from app.infra.models import _ts

    col = _ts(col_default_server=True)
    default = col.column.default.arg  # the callable stored by mapped_column
    value = default(None)  # invoked with the execution context, as SQLAlchemy does
    assert value.tzinfo is not None, f"_ts default must be tz-aware, got {value}"
    assert value.tzinfo == UTC
