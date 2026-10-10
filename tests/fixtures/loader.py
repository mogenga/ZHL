"""Fixture loader for acceptance tests (P0 spec §5.5).

``load_acceptance(name)`` returns a dict with the namespaces consumed by the
domain pure functions:
    {"batch": dict, "inspections": [ModuleInspectionIn],
     "rules": RuleSnapshotIn, "incidents": [dict], "reinspections": [dict]}

It is test-only: no app.infra / langgraph / DB. ``rules`` is the embedded
rule snapshot of the sample; the loader turns its JSON into the frozen
``RuleSnapshotIn`` dataclass so the judge can be fed directly.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.domain.judge import judge
from app.domain.types import (
    CurrentBand,
    Decision,
    ModuleInspectionIn,
    ParamStandard,
    RuleSnapshotIn,
)

_FIXTURES_DIR = Path(__file__).resolve().parent

# Fields ModuleInspectionIn requires; loader fills optional ones with "" default
# so a missing key never crashes reconstruction (the check ① missing-field
# semantics is exercised explicitly by passing sn="", not by omitting the key).
_INSPECTION_FIELDS: tuple[str, ...] = (
    "sn",
    "project",
    "grade",
    "captured_date",
    "pmax",
    "temp",
    "isc",
    "voc",
    "vm",
    "im",
    "ff",
    "appearance_result",
)


def _load_json(name: str) -> dict:
    """Find ``acceptance_<name>.json`` first, then ``<name>.json``.

    Acceptance samples are named by their bare sample id (normal / boundary /
    incident / consistency); seed / iso / mock fixtures keep their full names.
    """
    candidates = (_FIXTURES_DIR / f"acceptance_{name}.json", _FIXTURES_DIR / f"{name}.json")
    for path in candidates:
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(f"fixture not found: {name}")


def build_rule_snapshot(raw: dict) -> RuleSnapshotIn:
    """Reconstruct a frozen RuleSnapshotIn from a JSON rule dict."""
    parameter_standards = {
        name: ParamStandard(
            std_value=spec["std_value"],
            tolerance=spec["tolerance"],
            enabled=spec.get("enabled", True),
        )
        for name, spec in raw.get("parameter_standards", {}).items()
    }
    current_bands = tuple(
        CurrentBand(lower=b["lower"], upper=b["upper"], grade=b.get("grade", ""))
        for b in raw.get("current_bands", [])
    )
    return RuleSnapshotIn(
        nameplate_power=raw["nameplate_power"],
        power_tolerance=raw["power_tolerance"],
        parameter_standards=parameter_standards,
        current_bands=current_bands,
        sample_plan=dict(raw.get("sample_plan", {})),
    )


def build_inspection(raw: dict) -> ModuleInspectionIn:
    """Reconstruct a ModuleInspectionIn from a JSON inspection row.

    Missing optional keys default to "" so the required-field check ① can be
    exercised deliberately by passing sn="".
    """
    kwargs = {field_name: raw.get(field_name, "") for field_name in _INSPECTION_FIELDS}
    return ModuleInspectionIn(**kwargs)


def load_acceptance(name: str) -> dict:
    """Load a named acceptance fixture into a domain-consumable namespace."""
    raw = _load_json(name)
    return {
        "batch": dict(raw.get("batch", {})),
        "inspections": [build_inspection(row) for row in raw.get("inspections", [])],
        "rules": build_rule_snapshot(raw["rules"]),
        "incidents": list(raw.get("incidents", [])),
        "reinspections": list(raw.get("reinspections", [])),
        "raw": raw,
    }


def judge_all(namespace: dict, *, batch_id: str = "") -> list[Decision]:
    """Judge every inspection in a namespace against its embedded rule snapshot."""
    batch_id = batch_id or namespace["batch"].get("batch_id", "")
    return [
        judge(inspection, namespace["rules"], batch_id=batch_id)
        for inspection in namespace["inspections"]
    ]


__all__ = [
    "build_inspection",
    "build_rule_snapshot",
    "judge_all",
    "load_acceptance",
]
