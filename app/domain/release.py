"""Release assertions (P0 spec §3.5, 总 spec §2.4).

``assert_release`` runs six named checks in a fixed order and returns an
``AssertionResult`` carrying every individual check — never a bare boolean.
Pure function: no DB reads, no clock, no infra/langgraph imports.
"""

from __future__ import annotations

from app.domain.types import (
    AssertionCheck,
    AssertionResult,
    BatchView,
    CapaView,
    EvidenceView,
    IncidentView,
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


def assert_release(
    batch: BatchView,
    sample: SampleView,
    incidents: list[IncidentView],
    isolated_sns: list[str],
    evidence: list[EvidenceView],
    capa: list[CapaView],
) -> AssertionResult:
    """Run all six release checks; ``passed`` is the conjunction."""
    checks = (
        check_state(batch),
        check_sample_passed(sample),
        check_no_open_critical_incident(incidents),
        check_no_isolated_sn(isolated_sns),
        check_all_evidence_present(evidence),
        check_all_capa_verified(capa),
    )
    return AssertionResult(passed=all(c.passed for c in checks), checks=checks)
