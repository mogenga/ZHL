"""Rule version lifecycle state machine (pure)."""

from app.domain.rule_version import RULE_LIFECYCLE, can_transition, transition_rule_status


def test_lifecycle_table_shape() -> None:
    assert RULE_LIFECYCLE["draft"] == frozenset({"published"})
    assert RULE_LIFECYCLE["published"] == frozenset({"disabled"})
    assert RULE_LIFECYCLE["disabled"] == frozenset()


def test_legal_transitions() -> None:
    assert can_transition("draft", "published")
    assert can_transition("published", "disabled")
    assert not can_transition("draft", "disabled")
    assert not can_transition("disabled", "published")
    assert not can_transition("published", "published")


def test_transition_rule_status_legal() -> None:
    assert transition_rule_status("draft", "published") == ("published", None)
    assert transition_rule_status("published", "disabled") == ("disabled", None)


def test_transition_rule_status_illegal() -> None:
    new, reason = transition_rule_status("disabled", "published")
    assert new is None
    assert "cannot transition" in reason
    new2, reason2 = transition_rule_status("draft", "disabled")
    assert new2 is None
