"""State machines for batch (10 states) and incident (5 states).

Pure data-driven transition tables plus a deterministic ``transition``
function. Preconditions are fixed in domain (AGENT.md §3.4 / 总 spec §2.5/§2.6):
the model can never rewrite these.
"""

from __future__ import annotations

from app.domain.types import (
    CloseResult,
    IncidentView,
    ReinspectionView,
    TransitionResult,
)

# ---------------------------------------------------------------------------
# Transition tables (data, not code). Key: (from, to); value: required
# preconditions that must all be truthy in the caller-supplied dict.
# ---------------------------------------------------------------------------

BATCH_TRANSITIONS: dict[tuple[str, str], frozenset] = {
    ("draft", "pending_intake"): frozenset(),
    ("pending_intake", "pending_judge"): frozenset(),
    # 全部 Pass + 抽样 Pass
    ("pending_judge", "pending_release"): frozenset({"all_pass", "sample_pass"}),
    # 任一 Fail / 数据冲突 / 规则缺失
    ("pending_judge", "isolated"): frozenset({"any_fail"}),
    # 已指派责任人与时限
    ("isolated", "disposing"): frozenset({"assigned_owner", "due_at_set"}),
    # 措施已确认
    ("disposing", "pending_recheck"): frozenset({"measures_confirmed"}),
    # 复检通过 + 措施有效
    ("pending_recheck", "pending_release"): frozenset({"recheck_passed", "measures_effective"}),
    # 复检失败
    ("pending_recheck", "rejected"): frozenset(),  # recheck-failed branch, caller routes here
    # 断言全通过 + 人工批准
    ("pending_release", "released"): frozenset({"assertions_passed", "human_approved"}),
    # 任意非归档 -> rejected（人工拒绝）
    ("draft", "rejected"): frozenset({"human_rejected"}),
    ("pending_intake", "rejected"): frozenset({"human_rejected"}),
    ("pending_judge", "rejected"): frozenset({"human_rejected"}),
    ("pending_release", "rejected"): frozenset({"human_rejected"}),
    ("isolated", "rejected"): frozenset({"human_rejected"}),
    ("rejected", "archived"): frozenset(),
    ("released", "archived"): frozenset(),
}

INCIDENT_TRANSITIONS: dict[tuple[str, str], frozenset] = {
    ("new", "isolated"): frozenset({"sn_linked"}),
    ("isolated", "assigned"): frozenset({"owner_assigned"}),
    ("assigned", "pending_recheck"): frozenset({"reinspection_recorded"}),
    ("pending_recheck", "closed"): frozenset({"close_conditions_met"}),
    ("pending_recheck", "rejected"): frozenset(),  # recheck-failed branch, caller routes here
    ("assigned", "scrapped"): frozenset({"scrap_approved"}),
}

_KINDS = {"batch": BATCH_TRANSITIONS, "incident": INCIDENT_TRANSITIONS}

#: 未关闭异常状态（close 端点的批次联动 / §8.1 自动创建幂等查重的单一事实来源）
OPEN_INCIDENT_STATUSES = ("new", "isolated", "assigned", "pending_recheck")


def transition(
    current: str,
    target: str,
    preconditions: dict,
    kind: str,
) -> TransitionResult:
    """Request a state transition; deterministic, table-driven.

    Returns ``accepted=False`` with a reason when the transition is unknown
    (either a missing (from,to) pair or an unmet precondition) or when
    ``kind`` is not a known machine.
    """
    table = _KINDS.get(kind)
    if table is None:
        return TransitionResult(accepted=False, reason=f"unknown kind: {kind}")
    required = table.get((current, target))
    if required is None:
        return TransitionResult(
            accepted=False,
            reason=f"no transition {kind}:{current} -> {target} (skip/invalid)",
        )
    unmet = [p for p in required if not preconditions.get(p)]
    if unmet:
        return TransitionResult(
            accepted=False,
            reason=f"precondition(s) not met: {', '.join(sorted(unmet))}",
        )
    return TransitionResult(accepted=True)


def close_incident(
    incident: IncidentView, reinspection: ReinspectionView | None, capa_verified: bool
) -> CloseResult:
    """Five hard constraints for closing an incident (总 spec §2.6).

    1. SN linked or whole-lot declared
    2. original decision evidence present
    3. owner + containment + confirmed root cause + corrective action
    4. independent reinspection record
    5. reinspection passed and CAPA verified
    Missing any => can_close False with the missing list.
    """
    missing: list[str] = []

    if not incident.owner:
        missing.append("owner")
    if not (incident.confirmed_root_cause and incident.corrective_action):
        missing.append("capa_fields")
    if reinspection is None:
        missing.append("reinspection_record")
    elif reinspection.result != "pass":
        missing.append("reinspection_passed")
    if not capa_verified:
        missing.append("capa_verified")

    return CloseResult(can_close=not missing, missing=tuple(missing))
