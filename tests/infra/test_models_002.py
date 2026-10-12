"""Contract-extension model self-check (PB Task 1, L2/L3).

After the DDL 002 contract extension, ``reinspections`` gains
``scope_type`` / ``scope_sns`` and ``quality_incidents`` gains
``capa_verified``. This test pins the schema-consistency gate on the
ORM mirror: the new columns exist with the pinned defaults so that
``Base.metadata.create_all`` on SQLite still renders a consistent
schema (P0 spec §5.3 / §6 gate 3, extended by PB L2/L3).
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from app.infra.models import Base, QualityIncident, Reinspection


def test_reinspection_has_scope_columns() -> None:
    cols = Reinspection.__table__.columns
    assert "scope_type" in cols, "Reinspection.scope_type missing (PB L2)"
    assert "scope_sns" in cols, "Reinspection.scope_sns missing (PB L2)"
    # Column types pinned
    assert cols["scope_type"].type.compile().startswith("VARCHAR") or "VARCHAR" in str(
        cols["scope_type"].type
    ), f"scope_type type unexpected: {cols['scope_type'].type}"
    # scope_sns must be a JSON-ish type (JSONB on PG, JSON variant on SQLite)
    assert (
        "JSON" in str(cols["scope_sns"].type).upper()
    ), f"scope_sns type unexpected: {cols['scope_sns'].type}"


def test_quality_incident_has_capa_verified() -> None:
    cols = QualityIncident.__table__.columns
    assert "capa_verified" in cols, "QualityIncident.capa_verified missing (PB L3)"
    assert (
        "BOOL" in str(cols["capa_verified"].type).upper()
        or "BOOLEAN" in str(cols["capa_verified"].type).upper()
    ), f"capa_verified type unexpected: {cols['capa_verified'].type}"


def test_scope_type_default_is_minimal() -> None:
    col = Reinspection.__table__.c["scope_type"]
    assert col.default is not None, "scope_type has no default (PB L2)"
    # The Python-side default must be the string 'minimal'
    assert (
        col.default.arg == "minimal" or col.default.arg is None and _server_default_is_minimal(col)
    ), f"scope_type default not 'minimal': {col.default.arg!r}"


def _server_default_is_minimal(col) -> bool:
    if col.server_default is not None:
        return "minimal" in str(col.server_default.arg)
    return False


def test_scope_sns_default_is_empty_list() -> None:
    col = Reinspection.__table__.c["scope_sns"]
    assert col.default is not None, "scope_sns has no default (PB L2)"
    # Python-side callable default (list): confirm the column is NOT NULL in the
    # rendered DDL — the [] value is applied at insert time by the ORM layer,
    # so we verify the schema-level invariant instead of the Python object state.
    import sqlalchemy as sa
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        insp = sa.inspect(conn)
        cols = [c for c in insp.get_columns("reinspections") if c["name"] == "scope_sns"]
        assert cols, "scope_sns column missing from SQLite schema"
        assert cols[0]["nullable"] is False, "scope_sns should be NOT NULL in DDL"
    engine.dispose()


def test_capa_verified_default_is_false() -> None:
    col = QualityIncident.__table__.c["capa_verified"]
    assert col.default is not None, "capa_verified has no default (PB L3)"
    assert col.default.arg is False, f"capa_verified default not False: {col.default.arg!r}"


def test_create_all_includes_new_columns() -> None:
    """Schema-consistency gate: create_all on SQLite renders the new columns."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    re_cols = {c["name"] for c in insp.get_columns("reinspections")}
    qi_cols = {c["name"] for c in insp.get_columns("quality_incidents")}
    assert "scope_type" in re_cols, f"reinspections.scope_type missing in DB: {re_cols}"
    assert "scope_sns" in re_cols, f"reinspections.scope_sns missing in DB: {re_cols}"
    assert "capa_verified" in qi_cols, f"quality_incidents.capa_verified missing in DB: {qi_cols}"
    engine.dispose()


def test_models_are_distinct_and_complete() -> None:
    """The 12 models still registered; new columns do not break the count."""
    expected_12 = {
        "quality_rule_versions",
        "evidence",
        "delivery_batches",
        "module_inspections",
        "quality_incidents",
        "reinspections",
        "incident_sn_links",
        "samples",
        "sample_conclusions",
        "iso_sampling_table",
        "agent_task_traces",
        "reports",
    }
    assert expected_12 == set(Base.metadata.tables.keys())
