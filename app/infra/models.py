"""SQLAlchemy 2.0 ORM models for the 12 shared-contract tables.

Maps ``migrations/001_init.sql`` one-to-one (P0 spec §5.1 / §5.3). This module
is the *data-layer* twin of the DDL: it is NOT the authority on constraints
(triggers / EXCLUDE / permission revocation stay in the DDL), but it must
reproduce the schema's columns, types, keys, and FKs so that
``Base.metadata.create_all`` can be used as a schema-consistency self-check
and so Phase A/B can build on a single model surface.

Design choices for cross-DB self-check (SQLite in-memory in tests):
  * Postgres enums  -> ``sa.Enum(native_enum=False, create_type=False)``
    (render as VARCHAR + CHECK, so SQLite create_all never emits CREATE TYPE)
  * JSONB           -> ``JSONB().with_variant(sa.JSON(), "sqlite")``
  * UUID            -> ``sa.Uuid`` with a Python-side default (the DDL keeps
    its own ``gen_random_uuid()`` server default; the model default only
    matters for in-Python inserts)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# Reusable column-type builders (cross-DB safe)
# ---------------------------------------------------------------------------

JSONB_T = JSONB().with_variant(JSON(), "sqlite")

# PB L2: reinspection scope-type value domain (minimal / expanded / full_lot).
# Cross-DB safe: VARCHAR + CHECK in DDL (002), native_enum=False in models.
SCOPE_TYPE = ("minimal", "expanded", "full_lot")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _ts(col_default_server=True) -> Mapped[datetime | None]:
    """A timestamptz column; Python default so SQLite inserts work."""
    kw = {"nullable": True}
    if col_default_server:
        kw["default"] = lambda ctx: datetime.now(UTC)
    return mapped_column(DateTime(timezone=True), **kw)  # type: ignore


# ---------------------------------------------------------------------------
# Enum value lists (mirror the DDL CREATE TYPE statements verbatim)
# ---------------------------------------------------------------------------

RULE_STATUS = ("draft", "published", "disabled")
RULE_VERSION_ACTION = ("created", "published", "disabled")
BATCH_STATUS = (
    "draft",
    "pending_intake",
    "pending_judge",
    "pending_release",
    "isolated",
    "disposing",
    "pending_recheck",
    "rejected",
    "released",
    "archived",
)
RELEASE_DECISION = ("released", "rejected", "undecided")
SOURCE_TYPE = ("OCR_FORM", "IV_CSV", "MANUAL", "SIMULATED_DEVICE")
APPEARANCE_RESULT = ("pass", "fail")
INTAKE_STATUS = ("pending", "confirmed", "isolated", "rejected")
DECISION_STATUS = ("pending", "pass", "fail", "rule_missing")
TRIGGER_TYPE = (
    "rule_fail",
    "sample_fail",
    "appearance_manual",
    "data_consistency",
    "rule_missing",
)
SEVERITY = ("critical", "major", "minor")
INCIDENT_STATUS = (
    "new",
    "isolated",
    "assigned",
    "pending_recheck",
    "closed",
    "rejected",
    "scrapped",
)
SAMPLE_RESULT = ("pass", "fail", "rule_missing")
EVIDENCE_TYPE = (
    "source_file",
    "extraction_result",
    "human_confirmation",
    "decision_snapshot",
    "task_trace",
    "report_snapshot",
)
TRACE_VALIDATION = ("pass", "fail", "partial")
REPORT_TYPE = ("release", "incident")


class Base(DeclarativeBase):
    """Declarative base shared by all 13 models."""


def _enum(values: tuple[str, ...], *, nullable: bool = False) -> Mapped[str]:
    """Cross-DB enum: VARCHAR + CHECK, never a Postgres CREATE TYPE."""
    return mapped_column(
        SAEnum(*values, native_enum=False, create_type=False, length=40),
        nullable=nullable,
    )


# ---------------------------------------------------------------------------
# 5.1.1 quality_rule_versions
# ---------------------------------------------------------------------------


class QualityRuleVersion(Base):
    __tablename__ = "quality_rule_versions"

    rule_version_id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    nameplate_power: Mapped[float] = mapped_column(Numeric, nullable=False)
    power_tolerance: Mapped[float] = mapped_column(Numeric, nullable=False)
    parameter_standards: Mapped[dict] = mapped_column(JSONB_T, nullable=False)
    current_bands: Mapped[dict] = mapped_column(JSONB_T, nullable=False)
    sample_plan: Mapped[dict] = mapped_column(JSONB_T, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        SAEnum(*RULE_STATUS, native_enum=False, create_type=False, length=40),
        nullable=False,
        default="draft",
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = _ts()

    __table_args__ = (
        UniqueConstraint("project_id", "model", "version_no", name="uq_rule_idempotent"),
        CheckConstraint("nameplate_power > 0", name="ck_rule_power_positive"),
        CheckConstraint(
            "power_tolerance >= 0 AND power_tolerance <= 1",
            name="ck_rule_tolerance_range",
        ),
    )


# ---------------------------------------------------------------------------
# 5.1.10 evidence
# ---------------------------------------------------------------------------


class Evidence(Base):
    __tablename__ = "evidence"

    evidence_id: Mapped[uuid.UUID] = _uuid_pk()
    evidence_type: Mapped[str] = _enum(EVIDENCE_TYPE)
    object_ref: Mapped[dict] = mapped_column(JSONB_T, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str | None] = mapped_column(String)
    content: Mapped[dict | None] = mapped_column(JSONB_T)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_by: Mapped[str] = mapped_column(String, nullable=False, default="system")


# ---------------------------------------------------------------------------
# 5.1.2 delivery_batches
# ---------------------------------------------------------------------------


class DeliveryBatch(Base):
    __tablename__ = "delivery_batches"

    batch_id: Mapped[uuid.UUID] = _uuid_pk()
    batch_no: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    planned_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_rule_versions.rule_version_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        SAEnum(*BATCH_STATUS, native_enum=False, create_type=False, length=40),
        nullable=False,
        default="draft",
    )
    release_decision: Mapped[str] = mapped_column(
        SAEnum(*RELEASE_DECISION, native_enum=False, create_type=False, length=40),
        nullable=False,
        default="undecided",
    )
    release_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence.evidence_id")
    )
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = _ts()


# ---------------------------------------------------------------------------
# 5.1.3 module_inspections
# ---------------------------------------------------------------------------


class ModuleInspection(Base):
    __tablename__ = "module_inspections"

    inspection_id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("delivery_batches.batch_id"), nullable=False
    )
    sn: Mapped[str] = mapped_column(String, nullable=False)
    # D5 helper columns are filled by the DDL trigger; model mirrors them as
    # nullable so create_all does not require a Python value.
    sn_num: Mapped[int | None] = mapped_column(Integer)
    source_type: Mapped[str] = _enum(SOURCE_TYPE)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence.evidence_id"))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pmax: Mapped[float] = mapped_column(Numeric, nullable=False)
    temp: Mapped[float] = mapped_column(Numeric, nullable=False)
    isc: Mapped[float] = mapped_column(Numeric, nullable=False)
    voc: Mapped[float] = mapped_column(Numeric, nullable=False)
    vm: Mapped[float] = mapped_column(Numeric, nullable=False)
    im: Mapped[float] = mapped_column(Numeric, nullable=False)
    ff: Mapped[float] = mapped_column(Numeric, nullable=False)
    appearance_result: Mapped[str] = _enum(APPEARANCE_RESULT)
    intake_status: Mapped[str] = mapped_column(
        SAEnum(*INTAKE_STATUS, native_enum=False, create_type=False, length=40),
        nullable=False,
        default="pending",
    )
    decision_status: Mapped[str] = mapped_column(
        SAEnum(*DECISION_STATUS, native_enum=False, create_type=False, length=40),
        nullable=False,
        default="pending",
    )
    rule_snapshot: Mapped[dict | None] = mapped_column(JSONB_T)
    decision_evidence: Mapped[dict | None] = mapped_column(JSONB_T)
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = _ts()

    __table_args__ = (UniqueConstraint("batch_id", "sn", name="uq_inspection_batch_sn"),)


# ---------------------------------------------------------------------------
# 5.1.4 quality_incidents
# ---------------------------------------------------------------------------


class QualityIncident(Base):
    __tablename__ = "quality_incidents"

    incident_id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("delivery_batches.batch_id"), nullable=False
    )
    trigger_type: Mapped[str] = _enum(TRIGGER_TYPE)
    severity: Mapped[str] = _enum(SEVERITY)
    evidence_ids: Mapped[dict] = mapped_column(JSONB_T, nullable=False, default=list)
    owner: Mapped[str | None] = mapped_column(String)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    containment_action: Mapped[str | None] = mapped_column(Text)
    root_cause_hypotheses: Mapped[dict | None] = mapped_column(JSONB_T)
    confirmed_root_cause: Mapped[str | None] = mapped_column(Text)
    corrective_action: Mapped[str | None] = mapped_column(Text)
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reinspections.reinspection_id")
    )
    # PB L3: explicit engineer-declared CAPA-verified flag (reinspection pass alone
    # does NOT imply verified — measures-effectiveness is an independent human
    # judgement). `assert_release`'s `check_all_capa_verified` reads this.
    capa_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        SAEnum(*INCIDENT_STATUS, native_enum=False, create_type=False, length=40),
        nullable=False,
        default="new",
    )
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = _ts()


# ---------------------------------------------------------------------------
# 5.1.6 reinspections
# ---------------------------------------------------------------------------


class Reinspection(Base):
    __tablename__ = "reinspections"

    reinspection_id: Mapped[uuid.UUID] = _uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_incidents.incident_id"), nullable=False
    )
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("module_inspections.inspection_id"), nullable=False
    )
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_rule_versions.rule_version_id"), nullable=False
    )
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence.evidence_id"))
    pmax: Mapped[float] = mapped_column(Numeric, nullable=False)
    temp: Mapped[float] = mapped_column(Numeric, nullable=False)
    isc: Mapped[float] = mapped_column(Numeric, nullable=False)
    voc: Mapped[float] = mapped_column(Numeric, nullable=False)
    vm: Mapped[float] = mapped_column(Numeric, nullable=False)
    im: Mapped[float] = mapped_column(Numeric, nullable=False)
    ff: Mapped[float] = mapped_column(Numeric, nullable=False)
    result: Mapped[str] = _enum(APPEARANCE_RESULT)
    executor: Mapped[str] = mapped_column(String, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence.evidence_id"))
    # PB L2: reinspection scope — explicit, assertion-validated (PB spec D5/D5a).
    # scope_type: which scope (minimal / expanded / full_lot); scope_sns: JSONB array
    # of SNs actually covered by this reinspection (default = minimal, the incident's SN set).
    scope_type: Mapped[str] = mapped_column(
        SAEnum(*SCOPE_TYPE, native_enum=False, create_type=False, length=40),
        nullable=False,
        default="minimal",
    )
    scope_sns: Mapped[list] = mapped_column(JSONB_T, nullable=False, default=list)
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = _ts()


# ---------------------------------------------------------------------------
# 5.1.5 incident_sn_links
# ---------------------------------------------------------------------------


class IncidentSnLink(Base):
    __tablename__ = "incident_sn_links"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_incidents.incident_id", ondelete="CASCADE"),
        primary_key=True,
    )
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("module_inspections.inspection_id", ondelete="CASCADE"),
        primary_key=True,
    )


# ---------------------------------------------------------------------------
# 5.1.7 samples
# ---------------------------------------------------------------------------


class Sample(Base):
    __tablename__ = "samples"

    sample_id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("delivery_batches.batch_id"), nullable=False
    )
    inspection_type: Mapped[str] = mapped_column(String, nullable=False)
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_rule_versions.rule_version_id"), nullable=False
    )
    sample_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sn: Mapped[str] = mapped_column(String, nullable=False)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seed: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime | None] = _ts()

    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "inspection_type",
            "rule_version_id",
            "sample_round",
            "sn",
            name="uq_sample_unique",
        ),
    )


# ---------------------------------------------------------------------------
# 5.1.8 sample_conclusions
# ---------------------------------------------------------------------------


class SampleConclusion(Base):
    __tablename__ = "sample_conclusions"

    conclusion_id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("delivery_batches.batch_id"), nullable=False
    )
    inspection_type: Mapped[str] = mapped_column(String, nullable=False)
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_rule_versions.rule_version_id"), nullable=False
    )
    sample_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    ac: Mapped[int] = mapped_column(Integer, nullable=False)
    re: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[str] = _enum(SAMPLE_RESULT)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence.evidence_id"))
    created_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime | None] = _ts()

    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "inspection_type",
            "rule_version_id",
            "sample_round",
            name="uq_conclusion_unique",
        ),
    )


# ---------------------------------------------------------------------------
# 5.1.9 iso_sampling_table (read-only config, D9/D16)
# ---------------------------------------------------------------------------


class IsoSamplingRow(Base):
    __tablename__ = "iso_sampling_table"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inspection_type: Mapped[str] = mapped_column(String, nullable=False)
    inspection_level: Mapped[str] = mapped_column(String, nullable=False)
    aql: Mapped[float] = mapped_column(Numeric, nullable=False)
    lot_size_min: Mapped[int] = mapped_column(Integer, nullable=False)
    lot_size_max: Mapped[int] = mapped_column(Integer, nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    ac: Mapped[int] = mapped_column(Integer, nullable=False)
    re: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("ac < re", name="ck_iso_ac_lt_re"),
        UniqueConstraint(
            "inspection_type",
            "inspection_level",
            "aql",
            "lot_size_min",
            "lot_size_max",
            name="uq_iso_combo",
        ),
    )


# ---------------------------------------------------------------------------
# 5.1.11 agent_task_traces
# ---------------------------------------------------------------------------


class AgentTaskTrace(Base):
    __tablename__ = "agent_task_traces"

    task_id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("delivery_batches.batch_id"))
    input_ref: Mapped[dict] = mapped_column(JSONB_T, nullable=False)
    skill_name: Mapped[str] = mapped_column(String, nullable=False)
    plugins_invoked: Mapped[list] = mapped_column(JSONB_T, nullable=False, default=list)
    params_summary: Mapped[dict] = mapped_column(JSONB_T, nullable=False, default=dict)
    model_version: Mapped[str | None] = mapped_column(String)
    structured_output: Mapped[dict | None] = mapped_column(JSONB_T)
    validation_result: Mapped[str] = mapped_column(
        SAEnum(*TRACE_VALIDATION, native_enum=False, create_type=False, length=40),
        nullable=False,
        default="partial",
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    human_action: Mapped[dict | None] = mapped_column(JSONB_T)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# 5.1.12 reports
# ---------------------------------------------------------------------------


class Report(Base):
    __tablename__ = "reports"

    report_id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("delivery_batches.batch_id"), nullable=False
    )
    report_type: Mapped[str] = _enum(REPORT_TYPE)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.evidence_id"), nullable=False
    )
    agent_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_task_traces.task_id"))
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[dict] = mapped_column(JSONB_T, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("batch_id", "report_type", "version", name="uq_report_versioned"),
    )


# ---------------------------------------------------------------------------
# 5.1.13 rule_version_events（规则版本管理审计）
# ---------------------------------------------------------------------------


class RuleVersionEvent(Base):
    __tablename__ = "rule_version_events"

    event_id: Mapped[uuid.UUID] = _uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_rule_versions.rule_version_id"), nullable=False
    )
    action: Mapped[str] = _enum(RULE_VERSION_ACTION)
    operator: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB_T, nullable=False, default=dict)
    created_at: Mapped[datetime] = _ts()


__all__ = [
    "Base",
    "QualityRuleVersion",
    "Evidence",
    "DeliveryBatch",
    "ModuleInspection",
    "QualityIncident",
    "Reinspection",
    "IncidentSnLink",
    "Sample",
    "SampleConclusion",
    "IsoSamplingRow",
    "AgentTaskTrace",
    "Report",
    "RuleVersionEvent",
    "RULE_VERSION_ACTION",
]
