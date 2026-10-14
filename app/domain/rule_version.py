"""Rule version lifecycle state machine (pure, no DB import).

Transition table: draft -> published -> disabled (terminal). 不可逆，无回滚
（回到旧口径=发新 version_no）。对齐 AGENT.md §8 红线"修改已发布规则版本"。
"""

from __future__ import annotations

RULE_LIFECYCLE: dict[str, frozenset] = {
    "draft": frozenset({"published"}),
    "published": frozenset({"disabled"}),
    "disabled": frozenset(),  # 终态
}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in RULE_LIFECYCLE.get(from_status, frozenset())


def transition_rule_status(from_status: str, to_status: str) -> tuple[str | None, str | None]:
    if can_transition(from_status, to_status):
        return to_status, None
    return None, f"cannot transition from {from_status} to {to_status}"
