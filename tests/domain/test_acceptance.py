"""Acceptance consumption of the fixtures via the judge (P0 spec §5.5, samples 一/二).

normal  => all 10 components judge to Pass (样例一 正常放行).
boundary => lower-limit 533.5 Pass, control 533.4 Fail (样例二 边界值, Review Focus #1).
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/fixtures is not a package; put the repo root on sys.path so
# `app.domain.*` and the fixture dir resolve under plain pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.fixtures.loader import judge_all, load_acceptance


def test_normal_all_pass() -> None:
    ns = load_acceptance("normal")
    decisions = judge_all(ns)
    assert len(decisions) == 10
    for decision in decisions:
        assert decision.result == "Pass"
        assert all(check.passed for check in decision.checks)


def test_boundary_pass_and_fail() -> None:
    ns = load_acceptance("boundary")
    by_sn = {d.sn: d for d in judge_all(ns)}
    assert by_sn["SN-B-LOW"].result == "Pass"
    assert by_sn["SN-B-BELOW"].result == "Fail"
    # control fails specifically on the power range, not on another check
    failed = [c.rule for c in by_sn["SN-B-BELOW"].checks if not c.passed]
    assert failed == ["power_range"]


def test_decision_rule_snapshot_matches_seed() -> None:
    ns = load_acceptance("normal")
    decision = judge_all(ns)[0]
    # recomputable: the embedded snapshot equals the rule seed for JH-550W
    assert decision.rule_snapshot.nameplate_power == 550.0
    assert decision.rule_snapshot.power_tolerance == 0.03
    assert decision.rule_snapshot.parameter_standards["ff"].std_value == 0.80
    assert len(decision.rule_snapshot.current_bands) == 2
