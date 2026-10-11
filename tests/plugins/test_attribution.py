"""TraceabilityPlugin.validate_evidence tests (PB Task 5, D2/D3, Review Focus #1/#5).

Pure plugin: drops hypotheses with empty evidence_ids (D2 gate), drops
hypotheses whose evidence_ids are not all present in the evidence store
(Review Focus #5), re-numbers ``rank`` by descending confidence (D3,
confidence is a *sort* signal only — it never gates), and attaches an
``impact_scope`` dict per hypothesis (cross-batch hit counts are
caller-supplied — the plugin never reads the DB).
"""

from __future__ import annotations

from typing import Any

from app.plugins.attribution import TraceabilityPlugin


class _StubEvidenceStore:
    """Duck-typed EvidenceStore: only ``get`` is consumed by the plugin."""

    def __init__(self, ids: set[str]) -> None:
        self._ids = ids

    def get(self, evidence_id: str) -> Any:
        if evidence_id not in self._ids:
            raise KeyError(f"unknown evidence_id: {evidence_id}")
        return object()


def _hyp(
    cause: str, confidence: float, evidence_ids: list[str], action: str = "act"
) -> dict[str, Any]:
    return {
        "cause": cause,
        "confidence": confidence,
        "evidence_ids": evidence_ids,
        "recommended_action": action,
    }


def test_evidence_gate_drops_empty() -> None:
    """D2: three hypotheses (0.87 w/ evidence, 0.62 w/ evidence, 0.40 empty)
    -> exactly 2 survive, ranked 1/2; the 0.40 one is absent."""
    store = _StubEvidenceStore({"EV-ATT-001", "EV-ATT-002", "EV-ATT-003"})
    hypotheses = [
        _hyp("cell mismatch", 0.87, ["EV-ATT-001", "EV-ATT-002"]),
        _hyp("IV drift", 0.62, ["EV-ATT-003"]),
        _hyp("bypass-diode hot spot", 0.40, []),  # empty -> dropped
    ]
    out = TraceabilityPlugin().validate_evidence(hypotheses, store)
    assert len(out) == 2
    assert [h["rank"] for h in out] == [1, 2]
    assert [h["cause"] for h in out] == ["cell mismatch", "IV drift"]
    # the 0.40 hypothesis must not exist in the output
    assert all(h["cause"] != "bypass-diode hot spot" for h in out)
    # D3: confidence only orders — both survivors keep their confidence
    assert [h["confidence"] for h in out] == [0.87, 0.62]


def test_confidence_only_orders() -> None:
    """D3: shuffled-confidence input -> output rank is by descending confidence,
    not by input order or any confidence threshold."""
    store = _StubEvidenceStore({"E1", "E2", "E3"})
    hypotheses = [
        _hyp("low conf first", 0.30, ["E1"]),
        _hyp("mid conf", 0.55, ["E2"]),
        _hyp("high conf last", 0.90, ["E3"]),
    ]
    out = TraceabilityPlugin().validate_evidence(hypotheses, store)
    assert [h["confidence"] for h in out] == [0.90, 0.55, 0.30]
    assert [h["rank"] for h in out] == [1, 2, 3]
    assert [h["cause"] for h in out] == ["high conf last", "mid conf", "low conf first"]


def test_evidence_missing_in_store() -> None:
    """Review Focus #5: a hypothesis citing a nonexistent evidence_id is dropped
    (evidence must actually exist, not merely be non-empty)."""
    store = _StubEvidenceStore({"E1"})  # "E-GHOST" is NOT in the store
    hypotheses = [
        _hyp("good hypothesis", 0.80, ["E1"]),
        _hyp("ghost hypothesis", 0.95, ["E-GHOST"]),  # highest conf but missing evidence
    ]
    out = TraceabilityPlugin().validate_evidence(hypotheses, store)
    assert len(out) == 1
    assert out[0]["cause"] == "good hypothesis"
    # the 0.95 ghost must not leak in despite the higher confidence
    assert all(h["cause"] != "ghost hypothesis" for h in out)


def test_impact_scope_attached_from_cross_batch_counts() -> None:
    """The caller-supplied cross_batch_counts becomes the impact_scope block;
    affected_sns counts the hypothesis's own evidence_ids (D1 evidence layer)."""
    store = _StubEvidenceStore({"E1", "E2"})
    hypotheses = [
        _hyp("double-evidence cause", 0.80, ["E1", "E2"]),
        _hyp("single-evidence cause", 0.70, ["E1"]),
    ]
    cross_batch_counts = {"E1": 3, "E2": 1}
    out = TraceabilityPlugin().validate_evidence(
        hypotheses, store, cross_batch_counts=cross_batch_counts
    )
    top = out[0]  # 0.80 first
    assert top["impact_scope"]["cross_batch_hits"] == 4  # 3 (E1) + 1 (E2)
    assert top["impact_scope"]["affected_sns"] == 2  # its own evidence_ids count
    assert out[1]["impact_scope"]["cross_batch_hits"] == 3  # only E1
    assert out[1]["impact_scope"]["affected_sns"] == 1
