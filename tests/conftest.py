"""Repo-root on sys.path for all test packages + SQLite foreign keys ON.

Mirrors ``tests/e2e/conftest.py`` so that ``tests/api`` (and the other
top-level test packages) can import ``app.*`` / ``tests.fixtures.*``
regardless of which pytest rootdir the run starts from. This keeps the
``.venv`` Python from relying on the implicit pytest rootdir trick.

**Foreign keys are enforced on every in-memory SQLite engine** (autouse
fixture below). SQLite ignores ``FOREIGN KEY`` clauses unless
``PRAGMA foreign_keys=ON``, and 84 test engines were created without it —
so a whole class of defect was invisible to the suite. The one that got
through: ``create_reinspection`` wrote ``quality_incidents.verification_id``
before the referenced ``reinspections`` row existed (circular FK), which
Postgres rejects and SQLite silently accepted. Enforcing the pragma here
means the test suite now fails on that bug instead of shipping it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

# tests/conftest.py lives one level below the repo root; the root is .parent.parent.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@event.listens_for(Engine, "connect")
def _sqlite_enforce_foreign_keys(dbapi_connection, connection_record) -> None:
    """Turn FK enforcement on for SQLite connections (no-op for Postgres,
    where the constraints are already enforced)."""
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@pytest.fixture(autouse=True)
def _assert_sqlite_fk_pragma_enabled() -> None:
    """Guard: the listener above must stay wired.

    Without it the suite silently loses its Postgres-parity FK checks. This
    opens a real SQLite connection and asserts the pragma actually took
    effect, so "someone deleted the listener" fails loudly here instead of
    turning back into invisible foreign-key violations.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool

    probe = create_engine("sqlite://", poolclass=StaticPool)
    try:
        with probe.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1, (
                "SQLite foreign keys are NOT enforced: the connect listener in "
                "tests/conftest.py is missing or broken, and the suite would "
                "again miss Postgres-only FK violations."
            )
    finally:
        probe.dispose()
