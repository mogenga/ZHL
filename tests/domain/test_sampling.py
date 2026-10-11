"""Sampling-domain tests (Task 7, P0 spec §5.2 iso_lookup / freeze_sample).

Pins the two sampling pure functions:
  * iso_lookup — D9: a preset combo resolves to n/ac/re; a missing combo
    returns None (rule anomaly, no default release).
  * freeze_sample — deterministic layered-random selection with a fixed seed
    (seed = batch_id string): same seed + same sns => identical result,
    and the sample size equals iso_row.n.

Pure in-memory; no DB / clock / model. The ISO table fixture is loaded via
the shared loader so the data is the single DDL-aligned source.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.domain.sampling import freeze_sample, iso_lookup
from app.domain.types import IsoRow

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _iso_table() -> list[IsoRow]:
    """Load the preset ISO sampling table into IsoRow objects."""
    data = json.loads((_FIXTURES_DIR / "iso2859_1_aql.json").read_text(encoding="utf-8"))
    rows: list[IsoRow] = []
    for row in data["rows"]:
        rows.append(
            IsoRow(
                inspection_type=row["inspection_type"],
                inspection_level=row["inspection_level"],
                aql=row["aql"],
                lot_size_min=row["lot_size_min"],
                lot_size_max=row["lot_size_max"],
                n=row["n"],
                ac=row["ac"],
                re=row["re"],
            )
        )
    return rows


def test_iso_lookup_hit() -> None:
    table = _iso_table()
    # iv / normal / aql=1.0 / lot 16..25 -> n=20 ac=1 re=2 (fixture row 10)
    res = iso_lookup("iv", "normal", 1.0, 20, table)
    assert res is not None
    assert (res.n, res.ac, res.re) == (20, 1, 2)


def test_iso_lookup_hit_upper_edge() -> None:
    table = _iso_table()
    # lot_size at the inclusive upper bound of the interval still resolves
    res = iso_lookup("iv", "normal", 0.65, 15, table)
    assert res is not None
    assert (res.n, res.ac, res.re) == (13, 0, 1)


def test_iso_lookup_missing_returns_none() -> None:
    table = _iso_table()
    # lot_size below the smallest interval (min 2) has no row -> None
    res = iso_lookup("iv", "normal", 0.65, 1, table)
    assert res is None
    # unknown inspection type -> None
    assert iso_lookup("bogus", "normal", 1.0, 20, table) is None


def test_freeze_sample_deterministic() -> None:
    table = _iso_table()
    iso = iso_lookup("iv", "normal", 1.0, 20, table)
    assert iso is not None
    sns = [f"SN-{i}" for i in range(30)]
    a = freeze_sample("B-1", 20, iso, sns, "B-1")
    b = freeze_sample("B-1", 20, iso, sns, "B-1")
    assert [e.sn for e in a] == [e.sn for e in b]


def test_freeze_sample_respects_n() -> None:
    table = _iso_table()
    iso = iso_lookup("iv", "normal", 1.0, 20, table)
    assert iso is not None
    sns = [f"SN-{i}" for i in range(40)]
    sample = freeze_sample("B-1", 20, iso, sns, "B-1")
    assert len(sample) == iso.n
    assert all(e.selected for e in sample)
    # selected sns must be a subset of the input sns
    chosen = {e.sn for e in sample}
    assert chosen.issubset(set(sns))


def test_freeze_sample_smaller_lot_fewer_than_n() -> None:
    """When lot_size < iso.n, only the sns actually present can be sampled."""
    table = _iso_table()
    iso = iso_lookup("iv", "normal", 1.0, 20, table)  # n = 20
    assert iso is not None
    sns = [f"SN-{i}" for i in range(5)]
    sample = freeze_sample("B-1", 5, iso, sns, "B-1")
    # cannot sample more than the sns available
    assert len(sample) == len(sns)
    assert {e.sn for e in sample} == set(sns)
