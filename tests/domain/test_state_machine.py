"""State-machine tests (Task 6, P0 spec §3.4, 总 spec §2.5/§2.6).

Pins Review Focus #2: legal transitions are accepted, illegal jumps
(e.g. pending_judge -> released) are rejected with the missing-precondition
reason, and close_incident enforces the five hard constraints.
"""

from __future__ import annotations

from app.domain.state_machine import (
    BATCH_TRANSITIONS,
    INCIDENT_TRANSITIONS,
    close_incident,
    transition,
)
from app.domain.types import IncidentView, ReinspectionView


def test_legal_pending_judge_to_pending_release() -> None:
    r = transition(
        "pending_judge",
        "pending_release",
        {"all_pass": True, "sample_pass": True},
        "batch",
    )
    assert r.accepted is True


def test_illegal_skip_judge_to_released() -> None:
    r = transition("pending_judge", "released", {}, "batch")
    assert r.accepted is False
    assert r.reason  # must name the missing precondition / missing intermediate state


def test_pending_judge_to_isolated_on_fail() -> None:
    r = transition(
        "pending_judge",
        "isolated",
        {"any_fail": True},
        "batch",
    )
    assert r.accepted is True


def test_pending_recheck_to_rejected_on_fail() -> None:
    r = transition(
        "pending_recheck",
        "rejected",
        {"recheck_passed": False},
        "batch",
    )
    assert r.accepted is True


def test_pending_recheck_to_pending_release_on_pass() -> None:
    r = transition(
        "pending_recheck",
        "pending_release",
        {"recheck_passed": True, "measures_effective": True},
        "batch",
    )
    assert r.accepted is True


def test_unknown_state_rejected() -> None:
    r = transition("nonsense", "released", {}, "batch")
    assert r.accepted is False


def test_unknown_transition_kind_rejected() -> None:
    r = transition("pending_judge", "isolated", {}, "widget")
    assert r.accepted is False
    assert "unknown" in r.reason.lower()


def test_batch_table_covers_states() -> None:
    seen = {src for (src, _t), _ in BATCH_TRANSITIONS.items()}
    assert "pending_judge" in seen and "pending_release" in seen and "pending_recheck" in seen


def test_incident_table_covers_states() -> None:
    seen = {src for (src, _t), _ in INCIDENT_TRANSITIONS.items()}
    assert "new" in seen and "assigned" in seen and "pending_recheck" in seen


def _incident(**over) -> IncidentView:
    base = dict(
        incident_id="INC-1",
        batch_id="B-1",
        trigger_type="rule_fail",
        severity="major",
        status="pending_recheck",
        owner="qe",
        confirmed_root_cause="root",
        corrective_action="fix",
    )
    base.update(over)
    return IncidentView(**base)


def _reinspection(passed: bool = True) -> ReinspectionView:
    return ReinspectionView(
        reinspection_id="RE-1",
        incident_id="INC-1",
        sn="SN-1",
        result="pass" if passed else "fail",
    )


def test_close_all_five_satisfied() -> None:
    r = close_incident(_incident(), _reinspection(True), capa_verified=True)
    assert r.can_close is True
    assert r.missing == ()


def test_close_missing_reinspection() -> None:
    r = close_incident(_incident(), None, capa_verified=True)
    assert r.can_close is False
    assert "reinspection" in " ".join(r.missing).lower()


def test_close_reinspection_failed() -> None:
    r = close_incident(_incident(), _reinspection(False), capa_verified=True)
    assert r.can_close is False


def test_close_capa_not_verified() -> None:
    r = close_incident(_incident(), _reinspection(True), capa_verified=False)
    assert r.can_close is False


def test_close_missing_root_cause() -> None:
    r = close_incident(
        _incident(confirmed_root_cause="", corrective_action=""),
        _reinspection(True),
        capa_verified=True,
    )
    assert r.can_close is False


def test_close_missing_owner() -> None:
    r = close_incident(
        _incident(owner=""),
        _reinspection(True),
        capa_verified=True,
    )
    assert r.can_close is False


# ---------------------------------------------------------------------------
# acceptance_incident fixture consumption (P0 plan Task 6, 样例三 钉死)
# ---------------------------------------------------------------------------


def _view_from_fixture(incident: dict, reinspection: dict) -> tuple[IncidentView, ReinspectionView]:
    return (
        IncidentView(
            incident_id=incident["incident_id"],
            batch_id=incident["batch_id"],
            trigger_type=incident["trigger_type"],
            severity=incident["severity"],
            status=incident["status"],
            owner=incident.get("owner", ""),
            confirmed_root_cause=incident.get("confirmed_root_cause", ""),
            corrective_action=incident.get("corrective_action", ""),
        ),
        ReinspectionView(
            reinspection_id=reinspection["reinspection_id"],
            incident_id=reinspection["incident_id"],
            sn=reinspection["sn"],
            result=reinspection["result"],
            rule_version_id=reinspection.get("rule_version_id", ""),
            executor=reinspection.get("executor", ""),
            evidence_ids=tuple(reinspection.get("evidence_ids", ())),
        ),
    )


def test_close_incident_fixture_blocked() -> None:
    """样例三中间态：CAPA 未确认 + CAPA 字段缺失 → 不可关闭，missing 列表非空。"""
    from tests.fixtures.loader import load_acceptance

    data = load_acceptance("incident")
    inc, re_insp = _view_from_fixture(data["incidents"][0], data["reinspections"][0])
    capa = data["incidents"][0]["capa"]
    r = close_incident(inc, re_insp, capa_verified=capa["verified"])
    assert r.can_close is False
    assert "capa_fields" in r.missing
    assert "capa_verified" in r.missing


def test_close_incident_fixture_full_path_succeeds() -> None:
    """补齐五条件（复检通过 + CAPA 全字段 + 已验证）→ 可关闭。"""
    from tests.fixtures.loader import load_acceptance

    data = load_acceptance("incident")
    inc, re_insp = _view_from_fixture(data["incidents"][0], data["reinspections"][0])
    complete = IncidentView(
        incident_id=inc.incident_id,
        batch_id=inc.batch_id,
        trigger_type=inc.trigger_type,
        severity=inc.severity,
        status="pending_recheck",
        owner=inc.owner,
        confirmed_root_cause="impp band shift due to bypass diode fault",
        corrective_action="replace affected modules, retest line",
    )
    r = close_incident(complete, re_insp, capa_verified=True)
    assert r.can_close is True
    assert r.missing == ()
