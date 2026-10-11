"""Main-chain batch workflow state (PA spec §5.4 / A10, B5 / Q3).

``BatchWorkflowState`` is the LangGraph state carried across all six
nodes of ``main_chain_graph``. Fields:

- ``batch_id`` / ``rule_version_id``: identity context (thread_id = batch_id, R2/Q2)
- ``inspections``: list of B5-shape dicts (sn + six electrical params +
  appearance_result + intake_status) flowing intake -> judge
- ``confidence``: Q3 — min over all SN extraction confidences (graph-node
  aggregation; the extract Skill itself stays per-SN, AGENT.md §3.3)
- ``low_conf_sns``: Q3 — SNs whose extraction confidence < 0.8, surfaced to
  the confirmation UI
- ``intake_status``: Q3 — batch-level intake verdict (pending / confirmed /
  rejected), set by the human_confirm node from the resume payload
- ``decision`` / ``sample`` / ``transition_result`` / ``evidence_ids``:
  downstream artifacts (B5 dict shapes, not flat booleans)

Routing (spec §5.3):
    human_confirm -> confirmed : judge -> sample -> batch_transition
    human_confirm -> rejected : END (incident_subgraph mount point, R1)
    batch_transition: pending_release (all pass + sample pass)
                      isolated   (any fail / rule_missing, R1 mount point)
"""

from __future__ import annotations

from typing import TypedDict


class BatchWorkflowState(TypedDict):
    batch_id: str
    rule_version_id: str
    batch_status: str
    inspections: list[dict]
    extraction: dict
    confidence: float
    low_conf_sns: list[str]
    intake_status: str
    decision: dict
    sample: dict
    transition_result: dict
    evidence_ids: list[str]


__all__ = ["BatchWorkflowState"]
