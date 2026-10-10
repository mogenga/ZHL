"""Unit tests for the judge engine (Task 3, P0 spec §5.2 / §3.3).

These tests pin the deterministic judge semantics: the five fixed-order
checks, closed-interval tolerance boundaries (Review Focus #1), disabled
parameter skipping, and the four-tuple evidence shape (总 spec §6.3).
"""

from __future__ import annotations

from app.domain.judge import (
    check_appearance,
    check_current_band,
    check_parameter_tolerances,
    check_power_range,
    check_required_fields,
    judge,
    tolerance_interval,
)
from app.domain.types import (
    CurrentBand,
    ModuleInspectionIn,
    ParamStandard,
    RuleSnapshotIn,
)

POWER_TOL = 0.03
NAMEPLATE = 550.0


def _inspection(**overrides) -> ModuleInspectionIn:
    base = dict(
        sn="SN001",
        project="P1",
        grade="A",
        captured_date="2026-06-01",
        pmax=550.0,
        temp=25.0,
        isc=10.5,
        voc=48.0,
        vm=40.0,
        im=13.0,
        ff=0.80,
        appearance_result="pass",
    )
    base.update(overrides)
    return ModuleInspectionIn(**base)


def _rules(**overrides) -> RuleSnapshotIn:
    base = dict(
        nameplate_power=NAMEPLATE,
        power_tolerance=POWER_TOL,
        parameter_standards={
            "temp": ParamStandard(std_value=25.0, tolerance=0.10),
            "isc": ParamStandard(std_value=10.5, tolerance=0.05),
            "voc": ParamStandard(std_value=48.0, tolerance=0.05),
            "vm": ParamStandard(std_value=40.0, tolerance=0.05),
            "im": ParamStandard(std_value=13.0, tolerance=0.05),
            "ff": ParamStandard(std_value=0.80, tolerance=0.02),
        },
        current_bands=(CurrentBand(lower=12.0, upper=14.0, grade="A"),),
        sample_plan={"inspection_type": "iv", "aql": 1.0},
    )
    base.update(overrides)
    return RuleSnapshotIn(**base)


def test_tolerance_interval_exact() -> None:
    assert tolerance_interval(550.0, 0.03) == (533.5, 566.5)


def test_power_at_lower_boundary_pass() -> None:
    ins = _inspection(pmax=533.5)
    check = check_power_range(ins, _rules())
    assert check.passed is True
    assert check.detail["computed"]["lower"] == 533.5


def test_power_below_boundary_fail() -> None:
    ins = _inspection(pmax=533.4)
    assert check_power_range(ins, _rules()).passed is False


def test_power_at_upper_boundary_pass() -> None:
    ins = _inspection(pmax=566.5)
    assert check_power_range(ins, _rules()).passed is True


def test_power_above_boundary_fail() -> None:
    ins = _inspection(pmax=566.6)
    assert check_power_range(ins, _rules()).passed is False


def test_required_fields_all_present() -> None:
    assert check_required_fields(_inspection()).passed is True


def test_required_fields_missing_sn() -> None:
    ins = _inspection(sn="")
    check = check_required_fields(ins)
    assert check.passed is False
    assert "sn" in check.detail["missing"]


def test_parameter_tolerance_disabled_skipped() -> None:
    rules = _rules(
        parameter_standards={"temp": ParamStandard(std_value=25.0, tolerance=0.10, enabled=False)}
    )
    ins = _inspection(temp=99.0)  # would fail if checked
    check = check_parameter_tolerances(ins, rules)
    assert check.passed is True


def test_parameter_tolerance_in_range() -> None:
    ins = _inspection(temp=25.0)
    assert check_parameter_tolerances(ins, _rules()).passed is True


def test_parameter_tolerance_out_of_range() -> None:
    ins = _inspection(temp=99.0)
    assert check_parameter_tolerances(ins, _rules()).passed is False


def test_current_band_in_range() -> None:
    ins = _inspection(im=13.0)
    assert check_current_band(ins, _rules()).passed is True


def test_current_band_out_of_range() -> None:
    ins = _inspection(im=9.0)
    check = check_current_band(ins, _rules())
    assert check.passed is False


def test_appearance_pass() -> None:
    assert check_appearance(_inspection()).passed is True


def test_appearance_fail() -> None:
    ins = _inspection(appearance_result="fail")
    assert check_appearance(ins).passed is False


def test_judge_all_pass() -> None:
    decision = judge(_inspection(), _rules())
    assert decision.result == "Pass"
    assert len(decision.checks) == 5
    assert all(c.passed for c in decision.checks)


def test_judge_one_fail() -> None:
    decision = judge(_inspection(pmax=400.0), _rules())
    assert decision.result == "Fail"
    failed = [c for c in decision.checks if not c.passed]
    assert len(failed) == 1
    assert failed[0].rule == "power_range"


def test_check_detail_has_four_tuple() -> None:
    check = check_power_range(_inspection(pmax=550.0), _rules())
    detail = check.detail
    for key in ("input", "formula", "computed", "result"):
        assert key in detail


def test_judge_result_includes_rule_snapshot() -> None:
    decision = judge(_inspection(), _rules())
    assert decision.rule_snapshot.nameplate_power == NAMEPLATE
