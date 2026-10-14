"""Domain pure-function types.

Frozen dataclasses that the judge / release / state-machine / sampling pure
functions in ``app/domain`` consume and produce.

Purity contract (AGENT.md §8, P0 spec §3.2): this module must NOT import
``sqlalchemy``, ``app.infra.*``, ``langgraph``, any model service, nor read
the DB / clock / random. It is the shared contract frozen after Phase 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Inspection input & rule snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleInspectionIn:
    """A single module inspection record fed to the judge engine.

    Carries the six electrical parameters plus SN / grade / capture date
    required by the base-validation check (P0 spec §3.3 check ①).
    """

    sn: str
    project: str
    grade: str
    captured_date: str
    pmax: float
    temp: float
    isc: float
    voc: float
    vm: float
    im: float
    ff: float
    appearance_result: str

    def to_dict(self) -> dict:
        """Return a plain-dict view for JSON rule snapshots / fixtures."""
        return {
            "sn": self.sn,
            "project": self.project,
            "grade": self.grade,
            "captured_date": self.captured_date,
            "pmax": self.pmax,
            "temp": self.temp,
            "isc": self.isc,
            "voc": self.voc,
            "vm": self.vm,
            "im": self.im,
            "ff": self.ff,
            "appearance_result": self.appearance_result,
        }


@dataclass(frozen=True)
class ParamStandard:
    """Standard value + tolerance for one parameter (disabled items skipped)."""

    std_value: float
    tolerance: float
    enabled: bool = True


@dataclass(frozen=True)
class CurrentBand:
    """An Impp current band with a grade label."""

    lower: float
    upper: float
    grade: str = ""


@dataclass(frozen=True)
class RuleSnapshotIn:
    """Immutable rule snapshot the decision is computed against (C-1)."""

    nameplate_power: float
    power_tolerance: float
    parameter_standards: dict = field(default_factory=dict)
    current_bands: tuple = field(default_factory=tuple)
    sample_plan: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Plain-dict view for storing as the inspection's rule_snapshot."""
        return {
            "nameplate_power": self.nameplate_power,
            "power_tolerance": self.power_tolerance,
            "parameter_standards": dict(self.parameter_standards),
            "current_bands": [dict(b) for b in self.current_bands],
            "sample_plan": dict(self.sample_plan),
        }


# ---------------------------------------------------------------------------
# Judge output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleCheck:
    """One rule-item verdict with recomputable four-tuple evidence.

    ``detail`` follows the shape pinned by 总 spec §6.3:
    ``{"rule", "input", "formula", "computed", "result"}``.
    """

    rule: str
    passed: bool
    detail: dict = field(default_factory=dict)


DecisionResult = Literal["Pass", "Fail"]


@dataclass(frozen=True)
class Decision:
    """Judge output. Interface contract per AGENT.md §6.2."""

    result: DecisionResult
    checks: tuple
    rule_snapshot: RuleSnapshotIn
    batch_id: str = ""
    sn: str = ""

    def to_dict(self) -> dict:
        """Plain-dict view for persistence / API responses."""
        return {
            "result": self.result,
            "checks": [
                {"rule": c.rule, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
            "rule_snapshot": self.rule_snapshot.to_dict(),
            "batch_id": self.batch_id,
            "sn": self.sn,
        }


# ---------------------------------------------------------------------------
# Release-assertion inputs (assert_release views)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchView:
    """Minimal batch state seen by the release assertions."""

    batch_id: str
    status: str
    release_decision: str = "undecided"


@dataclass(frozen=True)
class SampleView:
    """Latest sampling conclusion for a batch.

    ``result`` defaults to ``""`` (not ``"pass"``): there is no such thing as
    a *default* sampling conclusion. A batch with no conclusion — or one the
    caller could not resolve — must fail ``check_sample_passed``, never pass
    it by omission (D9: no silent default release).
    """

    result: str = ""
    inspection_type: str = ""
    rule_version_id: str = ""
    sample_round: int = 1


@dataclass(frozen=True)
class IncidentView:
    """Minimal incident state seen by the release assertions."""

    incident_id: str
    batch_id: str
    trigger_type: str
    severity: str
    status: str
    owner: str = ""
    confirmed_root_cause: str = ""
    corrective_action: str = ""


@dataclass(frozen=True)
class EvidenceView:
    """A required evidence reference."""

    evidence_id: str
    evidence_type: str
    object_ref: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CapaView:
    """CAPA verification state for an incident."""

    incident_id: str
    containment_action: str = ""
    confirmed_root_cause: str = ""
    corrective_action: str = ""
    verified: bool = False


@dataclass(frozen=True)
class ReinspectionView:
    """A reinspection record (references the original, never overwrites it)."""

    reinspection_id: str
    incident_id: str
    sn: str
    result: str
    rule_version_id: str = ""
    executor: str = ""
    evidence_ids: tuple = field(default_factory=tuple)
    # PB L2: explicit reinspection scope (SNs actually re-measured). The domain
    # layer stays read-free; Phase B Task 7 API supplies scope_sns and the
    # Task 8 release API checks it via assert_release item 7.
    scope_sns: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class ReinspectionScopeIn:
    """PB D7-7: input to assert_release item 7 (check_reinspection_scope_covered).

    One row per reinspection scope check. lot_size / inspection_type /
    inspection_level / aql are NOT stored in the reinspections or
    quality_incidents tables; the Phase B release API (Task 8) resolves them
    from module_inspections + samples and passes them here, keeping the
    domain layer read-free (P0 purity: no DB reads in app/domain).
    """

    incident_id: str
    scope_sns: tuple
    lot_size: int
    inspection_type: str
    inspection_level: str
    aql: float
    #: 非空表示"这条 scope 的 ISO 组合没能从规则方案解析出来"（规则 sample_plan
    #: 缺项 / 无对应抽样方案）。不许用兜底值造一个假组合去查表——那会让检查
    #: 自证通过；由 check_reinspection_scope_covered 原样报为 Fail 原因。
    plan_error: str = ""


# ---------------------------------------------------------------------------
# Release-assertion output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssertionCheck:
    """One named release precondition result (never a bare boolean)."""

    name: str
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class AssertionResult:
    """Per-item release assertion results (P0 spec §3.5)."""

    passed: bool
    checks: tuple

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "reason": c.reason} for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# State-machine output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a requested state transition (deterministic)."""

    accepted: bool
    reason: str = ""


@dataclass(frozen=True)
class CloseResult:
    """Outcome of the five-condition incident-close gate."""

    can_close: bool
    missing: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IsoRow:
    """One row of the preset ISO sampling table (D9)."""

    inspection_type: str
    inspection_level: str
    aql: float
    lot_size_min: int
    lot_size_max: int
    n: int
    ac: int
    re: int


@dataclass(frozen=True)
class IsoResult:
    """Resolved sampling parameters (n / ac / re)."""

    n: int
    ac: int
    re: int


@dataclass(frozen=True)
class SampleEntry:
    """One frozen sample selection."""

    sn: str
    selected: bool = True


# ---------------------------------------------------------------------------
# Cross-phase interface contracts (AGENT.md §6.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncidentTrigger:
    """Exception trigger exchanged between Phase A and Phase B."""

    trigger_type: str
    batch_id: str
    affected_sns: tuple = field(default_factory=tuple)
    evidence_ids: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class ReinspectionResult:
    """Reinspection outcome exchanged between Phase A and Phase B."""

    reinspection_id: str
    incident_id: str
    sn: str
    passed: bool
    evidence_ids: tuple = field(default_factory=tuple)
