"""Deterministic judge engine (P0 spec §3.3 / §5.2, 总 spec §6).

Pure functions: no DB reads, no clock, no randomness, no model calls, no
infra/langgraph imports (AGENT.md §8 red line). The judge runs the five
checks in a fixed order and returns a recomputable ``Decision``.
"""

from __future__ import annotations

from app.domain.types import (
    CurrentBand,
    Decision,
    ModuleInspectionIn,
    RuleCheck,
    RuleSnapshotIn,
)

# Closed-interval tolerance: lower = std * (1 - tol), upper = std * (1 + tol).
# A measured value equal to either bound passes (boundary semantics).
_TOLERANCE_FORMULA = "lower = std * (1 - tol); upper = std * (1 + tol)"
_POWER_FORMULA = "lower = nameplate * (1 - tol); upper = nameplate * (1 + tol)"


def tolerance_interval(std: float, tol: float) -> tuple[float, float]:
    """Return the closed [lower, upper] interval for a standard value."""
    lower = std * (1.0 - tol)
    upper = std * (1.0 + tol)
    return (lower, upper)


def check_required_fields(inspection: ModuleInspectionIn) -> RuleCheck:
    """Check ①: base fields present — SN / grade / date / six e-parameters."""
    required: dict[str, object] = {
        "sn": inspection.sn,
        "grade": inspection.grade,
        "captured_date": inspection.captured_date,
        "pmax": inspection.pmax,
        "temp": inspection.temp,
        "isc": inspection.isc,
        "voc": inspection.voc,
        "vm": inspection.vm,
        "im": inspection.im,
        "ff": inspection.ff,
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    passed = not missing
    detail = {
        "rule": "required_fields",
        "input": {k: v for k, v in required.items()},
        "formula": "all base fields non-empty",
        "computed": {"missing": missing},
        "result": "Pass" if passed else "Fail",
        "missing": missing,
    }
    return RuleCheck(rule="required_fields", passed=passed, detail=detail)


def check_power_range(inspection: ModuleInspectionIn, rule: RuleSnapshotIn) -> RuleCheck:
    """Check ②: pmax within the closed nameplate-power tolerance band."""
    lower, upper = tolerance_interval(rule.nameplate_power, rule.power_tolerance)
    passed = lower <= inspection.pmax <= upper
    detail = {
        "rule": "power_range",
        "input": {
            "nameplate_power": rule.nameplate_power,
            "tolerance": rule.power_tolerance,
            "pmax": inspection.pmax,
        },
        "formula": _POWER_FORMULA,
        "computed": {"lower": lower, "upper": upper},
        "result": "Pass" if passed else "Fail",
    }
    return RuleCheck(rule="power_range", passed=passed, detail=detail)


def check_parameter_tolerances(inspection: ModuleInspectionIn, rule: RuleSnapshotIn) -> RuleCheck:
    """Check ③: each enabled parameter within its closed tolerance band."""
    actual: dict[str, float] = {
        "temp": inspection.temp,
        "isc": inspection.isc,
        "voc": inspection.voc,
        "vm": inspection.vm,
        "im": inspection.im,
        "ff": inspection.ff,
    }
    failures: list[dict] = []
    computed: dict[str, object] = {}
    for name, std in rule.parameter_standards.items():
        if not std.enabled:
            continue
        if name not in actual:
            continue
        lower, upper = tolerance_interval(std.std_value, std.tolerance)
        ok = lower <= actual[name] <= upper
        computed[name] = {"lower": lower, "upper": upper, "value": actual[name]}
        if not ok:
            failures.append({"param": name, "value": actual[name], "lower": lower, "upper": upper})
    passed = not failures
    detail = {
        "rule": "parameter_tolerances",
        "input": actual,
        "formula": _TOLERANCE_FORMULA,
        "computed": computed,
        "result": "Pass" if passed else "Fail",
    }
    return RuleCheck(rule="parameter_tolerances", passed=passed, detail=detail)


def check_current_band(inspection: ModuleInspectionIn, rule: RuleSnapshotIn) -> RuleCheck:
    """Check ④: impp (im) lands in one of the declared current bands."""
    band = _match_band(inspection.im, rule.current_bands)
    passed = band is not None
    detail = {
        "rule": "current_band",
        "input": {"im": inspection.im},
        "formula": "im within one declared band",
        "computed": {"matched_band": _band_dict(band)},
        "result": "Pass" if passed else "Fail",
    }
    return RuleCheck(rule="current_band", passed=passed, detail=detail)


def check_appearance(inspection: ModuleInspectionIn) -> RuleCheck:
    """Check ⑤: appearance result must be pass."""
    passed = inspection.appearance_result == "pass"
    detail = {
        "rule": "appearance",
        "input": {"appearance_result": inspection.appearance_result},
        "formula": "appearance == pass",
        "computed": {},
        "result": "Pass" if passed else "Fail",
    }
    return RuleCheck(rule="appearance", passed=passed, detail=detail)


def _band_dict(band: CurrentBand | None) -> dict | None:
    if band is None:
        return None
    return {"lower": band.lower, "upper": band.upper, "grade": band.grade}


def _match_band(value: float, bands: tuple) -> CurrentBand | None:
    for band in bands:
        if band.lower <= value <= band.upper:
            return band
    return None


def judge(
    inspection: ModuleInspectionIn, rule_snapshot: RuleSnapshotIn, *, batch_id: str = ""
) -> Decision:
    """Run the five checks in fixed order; all pass => Pass, else Fail."""
    checks = (
        check_required_fields(inspection),
        check_power_range(inspection, rule_snapshot),
        check_parameter_tolerances(inspection, rule_snapshot),
        check_current_band(inspection, rule_snapshot),
        check_appearance(inspection),
    )
    result = "Pass" if all(c.passed for c in checks) else "Fail"
    return Decision(
        result=result,
        checks=checks,
        rule_snapshot=rule_snapshot,
        batch_id=batch_id,
        sn=inspection.sn,
    )
