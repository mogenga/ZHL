"""Release assertions (P0 spec §3.5, 总 spec §2.4; PB D7-7 adds item 7).

``assert_release`` runs seven named checks in a fixed order and returns an
``AssertionResult`` carrying every individual check — never a bare boolean.
Pure function: no DB reads, no clock, no infra/langgraph imports.

PB D7-7 (option A): item 7 ``check_reinspection_scope_covered`` validates
that every *open* incident (status ∉ closed/rejected/scrapped) has at least
one ``ReinspectionScopeIn`` row whose ``scope_sns`` covers the caller-supplied
required SN set, and that ``iso_lookup`` resolves a non-None row for the scope's
ISO combo. Closed incidents need no coverage.
"""

from __future__ import annotations

from app.domain.sampling import iso_lookup
from app.domain.types import (
    AssertionCheck,
    AssertionResult,
    BatchView,
    CapaView,
    EvidenceView,
    IncidentView,
    IsoRow,
    ReinspectionScopeIn,
    SampleView,
)


def check_state(batch: BatchView) -> AssertionCheck:
    """① batch must be in pending_release."""
    passed = batch.status == "pending_release"
    return AssertionCheck(
        name="state",
        passed=passed,
        reason="" if passed else f"batch status is {batch.status}, expected pending_release",
    )


def check_sample_passed(sample: SampleView) -> AssertionCheck:
    """② latest sampling conclusion must be pass."""
    passed = sample.result == "pass"
    return AssertionCheck(
        name="sample_passed",
        passed=passed,
        reason="" if passed else f"sample result is {sample.result}, expected pass",
    )


def check_no_open_critical_incident(incidents: list[IncidentView]) -> AssertionCheck:
    """③ no open critical incident remains."""
    open_critical = [
        i
        for i in incidents
        if i.severity == "critical" and i.status not in ("closed", "rejected", "scrapped")
    ]
    passed = not open_critical
    reason = (
        ""
        if passed
        else f"{len(open_critical)} open critical incident(s): "
        + ", ".join(i.incident_id for i in open_critical)
    )
    return AssertionCheck(name="no_open_critical_incident", passed=passed, reason=reason)


def check_no_isolated_sn(isolated_sns: list[str]) -> AssertionCheck:
    """④ no isolated SN remains in the batch."""
    passed = not isolated_sns
    reason = "" if passed else f"isolated SNs remain: {', '.join(isolated_sns)}"
    return AssertionCheck(name="no_isolated_sn", passed=passed, reason=reason)


def check_all_evidence_present(evidence: list[EvidenceView]) -> AssertionCheck:
    """⑤ required evidence is present and non-empty."""
    passed = bool(evidence) and all(e.evidence_id for e in evidence)
    reason = "" if passed else "missing required evidence"
    return AssertionCheck(name="all_evidence_present", passed=passed, reason=reason)


def check_all_capa_verified(capa: list[CapaView]) -> AssertionCheck:
    """⑥ every CAPA is verified."""
    unverified = [c for c in capa if not c.verified]
    passed = not unverified
    reason = "" if passed else f"unverified CAPA: {', '.join(c.incident_id for c in unverified)}"
    return AssertionCheck(name="all_capa_verified", passed=passed, reason=reason)


def check_reinspection_scope_covered(
    required_by_incident: dict[str, tuple[str, ...]],
    reinspection_scopes: list[ReinspectionScopeIn],
    iso_table: list[IsoRow],
) -> AssertionCheck:
    """⑦ (PB D7-7) every open incident's required SN set is covered by at least
    one reinspection scope row, and each row's ISO combo resolves.

    - ``required_by_incident``: caller-supplied mapping of open-incident_id →
      the tuple of SNs that must be covered (resolved by Task 8 from
      ``incident_sn_links``). Closed incidents are excluded by the caller, so
      they never appear here.
    - A scope row covers an incident when its ``scope_sns`` is a superset of
      the incident's required set.
    - ``iso_lookup`` must return a non-None result for each row; a ``None``
      result is a Fail with reason naming "ISO".

    No DB reads, no clock: ``iso_lookup`` is a pure function over the
    caller-supplied ``iso_table``.
    """
    # Group provided scope rows by incident for lookup.
    provided: dict[str, list[ReinspectionScopeIn]] = {}
    for row in reinspection_scopes:
        provided.setdefault(row.incident_id, []).append(row)

    problems: list[str] = []

    # 1. Every required incident must have at least one covering scope row.
    for inc_id, required_sns in required_by_incident.items():
        rows = provided.get(inc_id, [])
        if not rows:
            problems.append(f"open incident {inc_id} has no reinspection scope row")
            continue
        # Only run the subset check when the caller actually declared a
        # required set (non-empty). An empty required set means "no SNs
        # declared" — the row is present, that's all we can check here.
        if required_sns:
            required_set = set(required_sns)
            covered = set()
            for row in rows:
                covered.update(row.scope_sns)
            missing = required_set - covered
            if missing:
                missing_sorted = sorted(missing)
                problems.append(
                    f"incident {inc_id}: scope covers {sorted(covered)} but required "
                    f"{sorted(required_set)}; missing {missing_sorted}"
                )

    # 2. ISO lookup must resolve for every provided row.
    for row in reinspection_scopes:
        result = iso_lookup(
            row.inspection_type,
            row.inspection_level,
            row.aql,
            row.lot_size,
            iso_table,
        )
        if result is None:
            problems.append(
                f"incident {row.incident_id}: ISO lookup returned None for "
                f"({row.inspection_type}, {row.inspection_level}, AQL={row.aql}, "
                f"lot_size={row.lot_size})"
            )

    passed = not problems
    reason = "; ".join(problems)
    return AssertionCheck(name="reinspection_scope_covered", passed=passed, reason=reason)


def assert_release(
    batch: BatchView,
    sample: SampleView,
    incidents: list[IncidentView],
    isolated_sns: list[str],
    evidence: list[EvidenceView],
    capa: list[CapaView],
    reinspection_scopes: list[ReinspectionScopeIn] = (),
    iso_table: list[IsoRow] = (),
    required_sns: dict[str, tuple[str, ...]] | None = None,
) -> AssertionResult:
    """Run all seven release checks; ``passed`` is the conjunction.

    PB D7-7: items 7 (``reinspection_scopes`` + ``iso_table``) default to
    empty so Phase 0 callers that don't yet pass them still work; the Phase B
    release API (Task 8) always supplies both.

    ``required_sns`` (PB Task 8 handoff, closes the self-reference
    deviation logged in the Task 2 spec review): the *authoritative*
    per-open-incident required SN set, resolved by the caller from
    ``incident_sn_links`` (the DB the domain layer must not read). When
    supplied, item 7 compares each incident's required set against the
    union of its reinspection rows' ``scope_sns`` — so a reinspection
    covering only part of the affected set is a Fail naming the missing
    SNs. When omitted (Phase 0 callers), the legacy self-referential
    derivation from the scope rows themselves is used, which only
    detects "no scope row at all" and keeps backward compatibility.
    """
    open_incident_ids = [
        i.incident_id for i in incidents if i.status not in ("closed", "rejected", "scrapped")
    ]
    if required_sns is not None:
        # Authoritative mode: caller declares the full required set per
        # open incident (from incident_sn_links). Open incidents absent from
        # the mapping get an empty required set (nothing declared to cover).
        required_by_incident: dict[str, tuple] = {
            inc_id: tuple(required_sns.get(inc_id, ())) for inc_id in open_incident_ids
        }
    else:
        # Legacy self-referential mode (Phase 0 backward compatibility):
        # build required_by_incident from the scope rows themselves.
        #
        # Known limitation (H-3, review 2026-10-12): in this mode an open
        # incident with NO reinspection scope row gets an empty required set
        # (``required_by_incident[inc_id] = ()`` below). check_reinspection_
        # scope_covered short-circuits ``if required_sns:`` for an empty
        # tuple, so "no reinspection at all" does NOT fail item 7 here — the
        # open incident with zero coverage is silently treated as covered.
        # The authoritative ``required_sns`` mode (Task 8 release API, fed
        # from incident_sn_links) is the correct path and always names the
        # gap. Legacy mode is retained only for Phase 0 backward
        # compatibility; callers that want item 7 to catch missing
        # reinspection coverage MUST pass ``required_sns``.
        required_by_incident = {}
        for row in reinspection_scopes:
            required_by_incident.setdefault(row.incident_id, ())
            existing = set(required_by_incident.get(row.incident_id, ()))
            existing.update(row.scope_sns)
            required_by_incident[row.incident_id] = tuple(existing)
        for inc_id in open_incident_ids:
            if inc_id not in required_by_incident:
                required_by_incident[inc_id] = ()  # no row → "no scope row" branch fails

    checks = (
        check_state(batch),
        check_sample_passed(sample),
        check_no_open_critical_incident(incidents),
        check_no_isolated_sn(isolated_sns),
        check_all_evidence_present(evidence),
        check_all_capa_verified(capa),
        check_reinspection_scope_covered(required_by_incident, reinspection_scopes, iso_table),
    )
    return AssertionResult(passed=all(c.passed for c in checks), checks=checks)
