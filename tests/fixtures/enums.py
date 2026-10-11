"""Fixture enum validation (001_init.sql enums, single source of truth).

The canonical enum values live in ``migrations/001_init.sql``.  This module
parses the two fixture-relevant enums (``batch_status``, ``incident_status``)
from that DDL at import time and exposes them to the loader so that
``load_acceptance`` can reject illegal status values instead of passing them
through silently.

Purity: no ``sqlalchemy`` / ``langgraph`` / ``app.infra`` / ``datetime`` /
``random`` imports.  A frozen ``frozenset`` per enum keeps the contract
immutable for the duration of a test run.
"""

from __future__ import annotations

import re
from pathlib import Path

# migrations/001_init.sql lives two levels up from tests/fixtures/
_DDL_PATH = Path(__file__).resolve().parent.parent.parent / "migrations" / "001_init.sql"

# Only the two enums the fixture loader needs to validate.  Extend this tuple
# here (not in the loader) when a new enum must be enforced.
_VALIDATED_ENUM_NAMES = ("batch_status", "incident_status")

_ENUM_RE = re.compile(
    r"CREATE\s+TYPE\s+(?P<type>\w+)\s+AS\s+ENUM\s*\((?P<values>[^)]*)\)",
    re.IGNORECASE,
)


def _parse_ddl_enums() -> dict[str, frozenset]:
    """Parse enum definitions out of 001_init.sql."""
    src = _DDL_PATH.read_text(encoding="utf-8")
    out: dict[str, frozenset] = {}
    for m in _ENUM_RE.finditer(src):
        type_name = m.group("type").lower()
        values = frozenset(v.strip().strip("'") for v in m.group("values").split(",") if v.strip())
        out[type_name] = values
    return out


_ALL_ENUMS = _parse_ddl_enums()

BATCH_STATUSES: frozenset = _ALL_ENUMS["batch_status"]
INCIDENT_STATUSES: frozenset = _ALL_ENUMS["incident_status"]

#: Public map used by the loader to pick the right enum for a given field.
#: Key: (json namespace, json field) -> frozenset of allowed values.
_ENUM_VALIDATION: dict[tuple[str, str], frozenset] = {
    ("batch", "status"): BATCH_STATUSES,
    ("incidents", "status"): INCIDENT_STATUSES,
}


def get_enum(namespace: str, field: str) -> frozenset | None:
    """Return the allowed value set for (namespace, field), or None."""
    return _ENUM_VALIDATION.get((namespace, field))


def validate_batch_status(status: str) -> None:
    """Raise ValueError if status is not a legal batch_status value."""
    if status not in BATCH_STATUSES:
        allowed = ", ".join(sorted(BATCH_STATUSES))
        raise ValueError(f"illegal batch.status {status!r}; expected one of: {allowed}")


def validate_incident_status(status: str) -> None:
    """Raise ValueError if status is not a legal incident_status value."""
    if status not in INCIDENT_STATUSES:
        allowed = ", ".join(sorted(INCIDENT_STATUSES))
        raise ValueError(f"illegal incident.status {status!r}; expected one of: {allowed}")


__all__ = [
    "BATCH_STATUSES",
    "INCIDENT_STATUSES",
    "get_enum",
    "validate_batch_status",
    "validate_incident_status",
]
