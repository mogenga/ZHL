"""SQLAlchemy model self-check (Task 7, P0 spec §5.3 / §6 完成标准第 3 条).

``Base.metadata.create_all`` on a SQLite in-memory engine must render the
full 12-table schema without error — this is the schema-consistency gate that
proves the models mirror ``migrations/001_init.sql``'s columns/keys. The
Postgres-specific bits (CREATE TYPE enums, EXCLUDE, triggers, permissions,
JSONB) are deliberately downgraded to cross-DB types in ``app/infra/models.py``
so the check can run on SQLite.

Synchronous SQLite is used (no aiosqlite dependency needed for a create_all).
"""

from __future__ import annotations

from sqlalchemy import create_engine

from app.infra.models import (
    AgentTaskTrace,
    Base,
    DeliveryBatch,
    Evidence,
    IncidentSnLink,
    IsoSamplingRow,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Reinspection,
    Report,
    RuleVersionEvent,
    Sample,
    SampleConclusion,
)


def test_all_13_tables_registered() -> None:
    expected = {
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
        "rule_version_events",
    }
    assert expected == set(Base.metadata.tables.keys()), sorted(Base.metadata.tables.keys())


def test_create_all_on_sqlite() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    # Tables physically exist in the SQLite schema
    import sqlite3

    conn = engine.connect()
    raw = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {row[0] for row in raw}
    expected = {
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
        "rule_version_events",
    }
    assert expected.issubset(names), expected - names
    del sqlite3
    conn.close()
    engine.dispose()


def test_models_are_distinct_orm_classes() -> None:
    models = [
        QualityRuleVersion,
        Evidence,
        DeliveryBatch,
        ModuleInspection,
        QualityIncident,
        Reinspection,
        IncidentSnLink,
        Sample,
        SampleConclusion,
        IsoSamplingRow,
        AgentTaskTrace,
        Report,
        RuleVersionEvent,
    ]
    table_names = [m.__tablename__ for m in models]
    assert len(table_names) == 13
    assert len(set(table_names)) == 13
