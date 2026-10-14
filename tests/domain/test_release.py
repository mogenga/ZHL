"""Release-assertion tests (Task 6 P0 + PB Task 2 D7-7 option A).

Pins Review Focus #4: assert_release returns seven named checks (never a bare
bool); an open critical incident, an isolated SN, or an uncovered reinspection
scope blocks release.

PB D7-7 adds item 7 (`reinspection_scope_covered`): for every open incident
(status ∉ closed/rejected/scrapped), its affected SNs must be covered by the
provided ReinspectionScopeIn rows, and iso_lookup must resolve a non-None row
for that scope. Closed incidents need no coverage.
"""

from __future__ import annotations

from app.domain.release import (
    assert_release,
    check_all_capa_verified,
    check_all_evidence_present,
    check_no_isolated_sn,
    check_no_open_critical_incident,
    check_reinspection_scope_covered,
    check_sample_passed,
    check_state,
)
from app.domain.types import (
    BatchView,
    CapaView,
    EvidenceView,
    IncidentView,
    IsoRow,
    ReinspectionScopeIn,
    SampleView,
)


def _iso_row() -> IsoRow:
    return IsoRow(
        inspection_type="IV",
        inspection_level="II",
        aql=1.0,
        lot_size_min=2,
        lot_size_max=8,
        n=3,
        ac=1,
        re=2,
    )


def _scope_pass() -> list[ReinspectionScopeIn]:
    """Scope row covering INC-1's SNs with a valid ISO combo."""
    return [
        ReinspectionScopeIn(
            incident_id="INC-1",
            scope_sns=("SN-A", "SN-B"),
            lot_size=5,
            inspection_type="IV",
            inspection_level="II",
            aql=1.0,
        )
    ]


def _ok_inputs():
    batch = BatchView(batch_id="B-1", status="pending_release", release_decision="undecided")
    sample = SampleView(result="pass")
    incidents = []
    isolated = []
    evidence = [
        EvidenceView(
            evidence_id="EV-1",
            evidence_type="decision_snapshot",
            object_ref={"kind": "batch", "id": "B-1"},
        )
    ]
    capa = [CapaView(incident_id="INC-0", verified=True)]
    return batch, sample, incidents, isolated, evidence, capa


def test_release_all_satisfied() -> None:
    b, s, i, iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, i, iso, ev, cp, [], [_iso_row()])
    assert r.passed is True
    assert len(r.checks) == 7
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
    r = assert_release(b, s, [open_critical], iso, ev, cp, [], [_iso_row()])
    assert r.passed is False
    by_name = {c.name: c for c in r.checks}
    assert by_name["no_open_critical_incident"].passed is False
    assert by_name["no_open_critical_incident"].reason


def test_release_isolated_sn_blocks() -> None:
    b, s, i, _iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, i, ["SN-X"], ev, cp, [], [_iso_row()])
    by_name = {c.name: c for c in r.checks}
    assert by_name["no_isolated_sn"].passed is False
    assert r.passed is False


def test_release_unverified_capa_blocks() -> None:
    b, s, i, iso, ev, _cp = _ok_inputs()
    bad_capa = [CapaView(incident_id="INC-1", verified=False)]
    r = assert_release(b, s, i, iso, ev, bad_capa, [], [_iso_row()])
    by_name = {c.name: c for c in r.checks}
    assert by_name["all_capa_verified"].passed is False


def test_release_missing_evidence_blocks() -> None:
    b, s, i, iso, _ev, cp = _ok_inputs()
    r = assert_release(b, s, i, iso, [], cp, [], [_iso_row()])
    by_name = {c.name: c for c in r.checks}
    assert by_name["all_evidence_present"].passed is False


def test_each_check_named() -> None:
    b, s, i, iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, i, iso, ev, cp, [], [_iso_row()])
    names = [c.name for c in r.checks]
    assert names == [
        "state",
        "sample_passed",
        "no_open_critical_incident",
        "no_isolated_sn",
        "all_evidence_present",
        "all_capa_verified",
        "reinspection_scope_covered",
    ]


def test_state_check_wrong_state_fails() -> None:
    b = BatchView(batch_id="B-1", status="pending_judge")
    s = SampleView(result="pass")
    ev = [EvidenceView("E", "decision_snapshot", {"kind": "batch", "id": "B-1"})]
    r = assert_release(b, s, [], [], ev, [], [], [_iso_row()])
    by_name = {c.name: c for c in r.checks}
    assert by_name["state"].passed is False


def test_individual_check_functions() -> None:
    assert check_state(BatchView(batch_id="B-1", status="pending_release")).passed is True
    assert check_sample_passed(SampleView(result="pass")).passed is True
    assert check_no_open_critical_incident([]).passed is True
    assert check_no_isolated_sn([]).passed is True
    assert (
        check_all_evidence_present(
            [EvidenceView("E", "decision_snapshot", {"kind": "batch", "id": "B-1"})]
        ).passed
        is True
    )
    assert check_all_capa_verified([CapaView("INC-1", verified=True)]).passed is True
    assert check_sample_passed(SampleView(result="fail")).passed is False
    assert check_no_isolated_sn(["SN-X"]).passed is False


def test_evidence_check_rejects_unanchored_rows_and_empty_sample_result() -> None:
    """断言⑤ / ② 不得因"字段恒真"或"缺省值"而放行。

    ``evidence_id`` 是主键、``result`` 缺省曾为 ``"pass"`` —— 两处都让检查
    退化成恒真。这里钉死：没有 object_ref 的证据不算齐备；没有抽样结论不算
    抽样合格。
    """
    assert check_all_evidence_present([]).passed is False
    assert check_all_evidence_present([EvidenceView("E", "decision_snapshot")]).passed is False
    no_id = EvidenceView("", "decision_snapshot", {"id": "B"})
    assert check_all_evidence_present([no_id]).passed is False
    assert check_sample_passed(SampleView()).passed is False
    assert check_sample_passed(SampleView(result="")).reason
    assert check_sample_passed(SampleView(result="")).passed is False


# ---------------------------------------------------------------------------
# PB D7-7: item 7 — reinspection scope coverage
# ---------------------------------------------------------------------------


def _open_incident() -> IncidentView:
    return IncidentView(
        incident_id="INC-1",
        batch_id="B-1",
        trigger_type="rule_fail",
        severity="major",
        status="assigned",
    )


def test_scope_covered_pass() -> None:
    """D7-7: open incident's SNs fully covered by scope + ISO row present → item 7 passes."""
    b, s, _i, iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, [_open_incident()], iso, ev, cp, _scope_pass(), [_iso_row()])
    by_name = {c.name: c for c in r.checks}
    assert by_name["reinspection_scope_covered"].passed is True


def test_scope_missing_incident() -> None:
    """D7-7: open incident INC-1 has no corresponding scope row → Fail, reason names INC-1."""
    b, s, _i, iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, [_open_incident()], iso, ev, cp, [], [_iso_row()])
    by_name = {c.name: c for c in r.checks}
    c7 = by_name["reinspection_scope_covered"]
    assert c7.passed is False
    assert "INC-1" in c7.reason


def test_scope_sn_subset() -> None:
    """D7-7: scope row covers only a subset of the required SNs → Fail, names the missing SN."""
    b, s, _i, iso, ev, cp = _ok_inputs()
    # required set for INC-1 = {SN-A, SN-B} (the full affected set);
    # provided scope row only covers SN-A → SN-B missing → Fail
    partial = [
        ReinspectionScopeIn(
            incident_id="INC-1",
            scope_sns=("SN-A",),
            lot_size=5,
            inspection_type="IV",
            inspection_level="II",
            aql=1.0,
        )
    ]
    r = check_reinspection_scope_covered(
        required_by_incident={"INC-1": ("SN-A", "SN-B")},
        reinspection_scopes=partial,
        iso_table=[_iso_row()],
    )
    assert r.passed is False
    assert "SN-B" in r.reason, f"reason should name missing SN-B, got: {r.reason!r}"


def test_scope_iso_missing() -> None:
    """D7-7: iso_lookup returns None for the scope's combo → Fail, reason mentions ISO."""
    r = check_reinspection_scope_covered(
        required_by_incident={"INC-1": ("SN-A",)},
        reinspection_scopes=[_scope_pass()[0]],
        iso_table=[],  # empty ISO table → iso_lookup returns None
    )
    assert r.passed is False
    assert "ISO" in r.reason, f"reason should mention ISO, got: {r.reason!r}"


def test_closed_incidents_not_required() -> None:
    """D7-7: an already-closed incident needs no scope coverage → item 7 passes."""
    closed = IncidentView(
        incident_id="INC-2",
        batch_id="B-1",
        trigger_type="rule_fail",
        severity="major",
        status="closed",
    )
    b, s, _i, iso, ev, cp = _ok_inputs()
    r = assert_release(b, s, [closed], iso, ev, cp, [], [_iso_row()])
    by_name = {c.name: c for c in r.checks}
    assert by_name["reinspection_scope_covered"].passed is True


def test_required_sns_authoritative_closes_self_reference_gap() -> None:
    """PB Task 8 handoff: the caller supplies the authoritative required set
    (from incident_sn_links). A reinspection covering only part of it must
    now Fail and name the missing SNs — the self-reference the legacy
    derivation hid."""
    scope = ReinspectionScopeIn(
        incident_id="I1",
        scope_sns=("SN-A",),  # actual reinspection coverage: 1 of 3
        lot_size=3,
        inspection_type="module",
        inspection_level="full",
        aql=0.65,
    )
    iso = [
        IsoRow(
            inspection_type="module",
            inspection_level="full",
            aql=0.65,
            lot_size_min=1,
            lot_size_max=10,
            n=3,
            ac=0,
            re=1,
        )
    ]
    incident = IncidentView(
        incident_id="I1",
        batch_id="B",
        trigger_type="rule_fail",
        severity="major",
        status="pending_recheck",
    )
    ev = [
        EvidenceView(
            evidence_id="E1",
            evidence_type="decision_snapshot",
            object_ref={"kind": "batch", "id": "B"},
        )
    ]
    result = assert_release(
        batch=BatchView(batch_id="B", status="pending_release"),
        sample=SampleView(result="pass"),
        incidents=[incident],
        isolated_sns=[],
        evidence=ev,
        capa=[],
        reinspection_scopes=[scope],
        iso_table=iso,
        required_sns={"I1": ("SN-A", "SN-B", "SN-C")},  # authoritative from incident_sn_links
    )
    item7 = result.checks[6]
    assert item7.name == "reinspection_scope_covered"
    assert item7.passed is False
    # the missing SNs are named in the reason
    assert "SN-B" in item7.reason and "SN-C" in item7.reason
    assert result.passed is False


def test_required_sns_full_coverage_passes() -> None:
    """Authoritative required set fully covered by the reinspection row ->
    item 7 passes."""
    scope = ReinspectionScopeIn(
        incident_id="I1",
        scope_sns=("SN-A", "SN-B", "SN-C"),
        lot_size=3,
        inspection_type="module",
        inspection_level="full",
        aql=0.65,
    )
    iso = [
        IsoRow(
            inspection_type="module",
            inspection_level="full",
            aql=0.65,
            lot_size_min=1,
            lot_size_max=10,
            n=3,
            ac=0,
            re=1,
        )
    ]
    incident = IncidentView(
        incident_id="I1",
        batch_id="B",
        trigger_type="rule_fail",
        severity="major",
        status="pending_recheck",
    )
    ev = [
        EvidenceView(
            evidence_id="E1",
            evidence_type="decision_snapshot",
            object_ref={"kind": "batch", "id": "B"},
        )
    ]
    result = assert_release(
        batch=BatchView(batch_id="B", status="pending_release"),
        sample=SampleView(result="pass"),
        incidents=[incident],
        isolated_sns=[],
        evidence=ev,
        capa=[],
        reinspection_scopes=[scope],
        iso_table=iso,
        required_sns={"I1": ("SN-A", "SN-B", "SN-C")},
    )
    assert result.checks[6].passed is True
    assert result.passed is True
