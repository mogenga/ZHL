"""Loader enum-validation tests (Task 7 prep — fixture status alignment).

Positive: every shipped fixture loads cleanly and its status values fall
inside the DDL-derived enum sets.
Negative: a fixture whose batch.status / incident.status is not a legal
enum value must raise ValueError on load, never pass through silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.enums import BATCH_STATUSES, INCIDENT_STATUSES, validate_batch_status
from tests.fixtures.loader import load_acceptance

_FIXTURES_DIR = Path(__file__).resolve().parent


def test_shipped_fixtures_load_clean() -> None:
    """All four acceptance fixtures validate against the DDL enums."""
    for name in ("normal", "boundary", "incident", "consistency"):
        ns = load_acceptance(name)
        assert ns["batch"]["status"] in BATCH_STATUSES, name
        for inc in ns["incidents"]:
            if inc.get("status"):
                assert inc["status"] in INCIDENT_STATUSES, (name, inc["status"])


def test_incident_fixture_sn_linked_flag() -> None:
    """acceptance_incident carries sn_linked=True (D4 incident_sn_links)."""
    ns = load_acceptance("incident")
    assert ns["incidents"][0]["sn_linked"] is True


def test_invalid_batch_status_raises(tmp_path: Path, monkeypatch) -> None:
    """An illegal batch.status must raise ValueError, not pass through."""
    src = _FIXTURES_DIR / "acceptance_normal.json"
    raw = json.loads(src.read_text(encoding="utf-8-sig"))
    raw["batch"]["status"] = "totally_bogus"
    out = tmp_path / "acceptance_bogus.json"
    out.write_text(json.dumps(raw), encoding="utf-8")

    loader = pytest.importorskip("tests.fixtures.loader")

    # Point _load_json at tmp_path so the bogus fixture wins the candidate scan.
    monkeypatch.setattr(loader, "_FIXTURES_DIR", tmp_path)
    with pytest.raises(ValueError, match="illegal batch.status"):
        loader.load_acceptance("bogus")


def test_invalid_incident_status_raises(tmp_path: Path, monkeypatch) -> None:
    """An illegal incident.status must raise ValueError, not pass through."""
    src = _FIXTURES_DIR / "acceptance_incident.json"
    raw = json.loads(src.read_text(encoding="utf-8-sig"))
    raw["incidents"][0]["status"] = "not_a_state"
    out = tmp_path / "acceptance_bogus_inc.json"
    out.write_text(json.dumps(raw), encoding="utf-8")

    loader = pytest.importorskip("tests.fixtures.loader")
    monkeypatch.setattr(loader, "_FIXTURES_DIR", tmp_path)
    with pytest.raises(ValueError, match="illegal incident.status"):
        loader.load_acceptance("bogus_inc")


def test_validate_batch_status_positive() -> None:
    for v in sorted(BATCH_STATUSES):
        validate_batch_status(v)  # no raise


def test_validate_batch_status_negative() -> None:
    with pytest.raises(ValueError):
        validate_batch_status("draft-ish")
