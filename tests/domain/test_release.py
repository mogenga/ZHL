"""Release-assertion tests (Task 6, P0 spec §3.5, 总 spec §2.4).

Pins Review Focus #4: assert_release returns six named checks (never a bare
bool); an open critical incident or an isolated SN blocks release.
"""

from __future__ import annotations

from app.domain.release import (
    assert_release,
    check_all_capa_verified,
    check_all_evidence_present,
    check_no_isolated_sn,
    check_no_open_critical_incident,
    check_sample_passed,
    check_state,
)
from app.domain.types import (
    BatchView,
    CapaView,
    EvidenceView,
    IncidentView,
    SampleView,
)


def _ok_inputs():
    batch = BatchView(batch_id="B-1", status="pending_release", release_decision="undecided")
    sample = SampleView(result="pass")
    incidents = []
    isolated = []
    evidence = [EvidenceView(evidence_id="EV-1", evidence_type="decision_snapshot")]
    capa = [CapaView(incident_id="INC-0", verified=True)]
    return batch, sample, incidents, isolated, evidence, capa


def test_release_all_satisfied() -> None:
    b, s, i, iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, i, iso, ev, cp)
    assert r.passed is True
    assert len(r.checks) == 6
    assert all(c.passed for c in r.checks)


def test_release_open_critical_incident_blocks() -> None:
    b, s, _i, iso, ev, cp = _ok_inputs()
    open_critical = IncidentView(
        incident_id="INC-1",
        batch_id="B-1",
        trigger_type="rule_fail",
        severity="critical",
        status="new",
    )
    r = assert_release(b, s, [open_critical], iso, ev, cp)
    assert r.passed is False
    by_name = {c.name: c for c in r.checks}
    assert by_name["no_open_critical_incident"].passed is False
    assert by_name["no_open_critical_incident"].reason


def test_release_isolated_sn_blocks() -> None:
    b, s, i, _iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, i, ["SN-X"], ev, cp)
    by_name = {c.name: c for c in r.checks}
    assert by_name["no_isolated_sn"].passed is False
    assert r.passed is False


def test_release_unverified_capa_blocks() -> None:
    b, s, i, iso, ev, _cp = _ok_inputs()
    bad_capa = [CapaView(incident_id="INC-1", verified=False)]
    r = assert_release(b, s, i, iso, ev, bad_capa)
    by_name = {c.name: c for c in r.checks}
    assert by_name["all_capa_verified"].passed is False


def test_release_missing_evidence_blocks() -> None:
    b, s, i, iso, _ev, cp = _ok_inputs()
    r = assert_release(b, s, i, iso, [], cp)
    by_name = {c.name: c for c in r.checks}
    assert by_name["all_evidence_present"].passed is False


def test_each_check_named() -> None:
    b, s, i, iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, i, iso, ev, cp)
    names = [c.name for c in r.checks]
    assert names == [
        "state",
        "sample_passed",
        "no_open_critical_incident",
        "no_isolated_sn",
        "all_evidence_present",
        "all_capa_verified",
    ]


def test_state_check_wrong_state_fails() -> None:
    b = BatchView(batch_id="B-1", status="pending_judge")
    s = SampleView(result="pass")
    r = assert_release(b, s, [], [], [EvidenceView("E", "decision_snapshot")], [])
    by_name = {c.name: c for c in r.checks}
    assert by_name["state"].passed is False


def test_individual_check_functions() -> None:
    assert check_state(BatchView(batch_id="B-1", status="pending_release")).passed is True
    assert check_sample_passed(SampleView(result="pass")).passed is True
    assert check_no_open_critical_incident([]).passed is True
    assert check_no_isolated_sn([]).passed is True
    assert check_all_evidence_present([EvidenceView("E", "decision_snapshot")]).passed is True
    assert check_all_capa_verified([CapaView("INC-1", verified=True)]).passed is True
    assert check_sample_passed(SampleView(result="fail")).passed is False
    assert check_no_isolated_sn(["SN-X"]).passed is False
