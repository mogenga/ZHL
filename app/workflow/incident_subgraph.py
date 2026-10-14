"""Incident subgraph — LangGraph StateGraph wiring (PB Task 6, D9).

Topological contract (plan Task 6):

    generate_hypotheses
        -> validate_evidence
        -> human_adopt          (interrupt: 3-way choice payload = hypotheses)
        -> run_reinspection     (stub callable in this task; Task 7 wires real)
        -> route_reinspection_result
             pass -> END
             fail -> rejected_route -> END

State (TypedDict):
    incident_id, batch_id, hypotheses, human_decision,
    reinspection_result, reinspection_payload, route_marker

``build_incident_subgraph(checkpointer, *, reinspection_callable=None)``
returns a compiled graph. The subgraph can be compiled standalone and
replayed (``graph.get_state(config).next`` reveals the pending node).

The LLM call (inside ``generate_hypotheses``) happens in the node body,
which is a LangGraph node — LangGraph nodes do not hold a DB transaction
open across the interrupt boundary, so the AGENT.md §4.3 discipline
(LLM outside the transaction) is naturally satisfied: the node runs,
calls the LLM, writes the hypotheses to state; the *persistence* of those
hypotheses to the DB happens in the API layer (Task 6's ``POST /assign``
etc.) or in Task 5's Skill — never inside a subgraph node that spans the
interrupt.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class IncidentSubgraphState(TypedDict, total=False):
    incident_id: str
    batch_id: str
    hypotheses: list[dict]
    human_decision: dict | None
    reinspection_result: str | None
    reinspection_payload: dict | None
    route_marker: str | None
    # C9: audit markers so trace consumers can tell stub pass-throughs
    # apart from real skill output (no LLM / no evidence validation yet).
    hypotheses_source: str | None
    evidence_validation: str | None


#: Mount point constant for the (future) main_chain_graph.
INCIDENT_ENTRY = "incident_subgraph"


def _generate_hypotheses(
    state: IncidentSubgraphState,
    attribution_ctx: dict | None = None,
) -> dict:
    """Node 1: produce the hypothesis list.

    - ``stub``: no ctx (or pre-seeded hypotheses) -> keep the C9 pass-through
      marker ``"stub"`` (no LLM / no evidence gate ran in this node).
    - ``attribution``: empty seed + ``attribution_ctx`` supplied -> run the
      real ``AttributionSkill`` (closure-injected: incident / session / llm /
      evidence_store / trace are bound here, NOT serialized into LangGraph
      state). The Skill runs its own D2 evidence gate + trace write.
    """
    seeded = state.get("hypotheses") or []
    if seeded or attribution_ctx is None:
        # C9: a path that did NOT run the real Skill *inside this node* keeps
        # the "stub" marker. Exception: when the caller already ran the Skill
        # it stamps the marker itself (the endpoint generates the hypotheses
        # and seeds them, so the model is not asked twice) — honour that
        # stamp rather than understating a validation that did happen.
        return {
            "hypotheses": seeded,
            "hypotheses_source": state.get("hypotheses_source") or "stub",
        }
    from app.plugins.attribution import AttributionSkill

    validated = AttributionSkill(
        attribution_ctx["llm"],
        attribution_ctx["evidence_store"],
    ).run(
        attribution_ctx["incident"],
        trace=attribution_ctx["trace"],
        session=attribution_ctx["session"],
    )
    return {"hypotheses": validated, "hypotheses_source": "attribution"}


def _validate_evidence(
    state: IncidentSubgraphState,
    attribution_ctx: dict | None = None,
) -> dict:
    """Node 2: evidence gate.

    On the attribution path the gate already ran inside the Skill
    (``TraceabilityPlugin.validate_evidence``); here we mark
    ``evidence_validation="skill"``. Seeded / stub paths keep the C9 marker
    ``"stub"`` so trace consumers do not mistake a pass-through for a real
    validation pass.
    """
    source = state.get("hypotheses_source")
    marker = "skill" if source == "attribution" else "stub"
    return {
        "hypotheses": state.get("hypotheses") or [],
        "evidence_validation": marker,
    }


def _human_adopt(state: IncidentSubgraphState) -> dict:
    """Node 3: interrupt with the 3-way choice payload (D9).

    The payload is the hypothesis list so a human reviewer can pick
    ``adopt`` (with a cause), ``reject``, or ``defer``. The resume value
    (``Command(resume={"decision": ..., "cause": ...})``) becomes the
    return of ``interrupt``.
    """
    decision = interrupt(
        {"choice": "adopt | reject | defer", "hypotheses": state.get("hypotheses") or []}
    )
    return {"human_decision": decision}


def _run_reinspection(
    state: IncidentSubgraphState, reinspection_callable: Callable[[dict], dict] | None
) -> dict:
    """Node 4: report the reinspection outcome the API layer already recorded.

    The reinspection itself is a real business action owned by
    ``POST /api/incidents/{id}/reinspections`` (it writes a ``Reinspection``
    row, validates scope, and transitions the incident). This node does NOT
    invent a verdict: the API layer puts the recorded result into
    ``reinspection_payload`` before resuming, and here we simply route on it.

    - explicit ``result`` in the payload -> that verdict;
    - payload present but no result -> ``unknown`` (routes to END, no
      invented pass — a caller that has not recorded anything gets no
      closure);
    - no payload and no injected callable -> ``None`` (the replay/test
      scaffold path, unchanged).
    """
    payload = state.get("reinspection_payload") or {}
    if reinspection_callable is not None:
        result = reinspection_callable(payload)
        return {"reinspection_result": result.get("result")}
    if payload:
        return {"reinspection_result": str(payload.get("result") or "unknown")}
    return {"reinspection_result": None}


def _route_reinspection_result(state: IncidentSubgraphState) -> str:
    """Node 5 (conditional router): fail -> rejected_route, else END.

    Only an explicit recorded verdict routes: ``fail`` -> ``rejected_route``;
    ``pass`` / ``unknown`` / absent -> END. "No failure recorded" is not a
    pass — the API layer (close endpoint / release assertions) is the
    authority that refuses closure without a passing reinspection. Inventing
    a pass here (the old stub) claimed a closure the business layer had never
    established.
    """
    if state.get("reinspection_result") == "fail":
        return "rejected_route"
    return END


def _rejected_route(state: IncidentSubgraphState) -> dict:
    """Terminal marker node for the fail branch (API layer maps this to
    the incident/batch state transition to ``rejected``)."""
    return {"route_marker": "rejected"}


def build_incident_subgraph(
    checkpointer: BaseCheckpointSaver,
    *,
    reinspection_callable: Callable[[dict], dict] | None = None,
    attribution_ctx: dict | None = None,
) -> Any:
    """Compile the incident subgraph with the given checkpointer.

    ``attribution_ctx`` (T-1): optional dict binding live dependencies into
    the generate/validate node closures — ``{"incident", "session", "llm",
    "evidence_store", "trace"}``. Live deps are closure-captured (the
    ``_make_run_reinspection`` pattern), never serialized into
    ``IncidentSubgraphState`` (state is JSON-checkpointed; a live session
    is not serializable).

    Cross-request HITL (2026-10-14 rework): ``build_incident_subgraph``
    supports the *interrupted* form — run to the ``human_adopt`` interrupt
    on one request (``start_incident_run``), then resume on a **later**
    request with the reviewer's decision (``POST /api/incidents/{id}/adopt``).
    ``run_reinspection`` reports the outcome **already recorded by the API
    layer** (a real ``Reinspection`` row); it never invents a pass.
    """
    builder = StateGraph(IncidentSubgraphState)
    builder.add_node("generate_hypotheses", _make_generate_hypotheses(attribution_ctx))
    builder.add_node("validate_evidence", _make_validate_evidence(attribution_ctx))
    builder.add_node("human_adopt", _human_adopt)

    # ``run_reinspection`` needs the closure value, so bind it via a lambda
    # factory (LangGraph nodes are plain callables).
    builder.add_node("run_reinspection", _make_run_reinspection(reinspection_callable))
    builder.add_node("route_reinspection_result", _route_reinspection_result)
    builder.add_node("rejected_route", _rejected_route)

    builder.add_edge(START, "generate_hypotheses")
    builder.add_edge("generate_hypotheses", "validate_evidence")
    builder.add_edge("validate_evidence", "human_adopt")
    builder.add_edge("human_adopt", "run_reinspection")
    builder.add_conditional_edges(
        "run_reinspection",
        _route_reinspection_result,
        {"rejected_route": "rejected_route", END: END},
    )
    builder.add_edge("rejected_route", END)

    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Cross-request HITL helpers (2026-10-14): start -> [days pass] -> resume
# ---------------------------------------------------------------------------


def incident_run_config(incident_id: str) -> dict:
    """The subgraph's thread config (one open attribution per incident)."""
    return {"configurable": {"thread_id": f"incident:{incident_id}"}}


def start_incident_run(
    graph: Any,
    incident_id: str,
    seed: dict | None = None,
) -> dict:
    """Run the subgraph until the ``human_adopt`` interrupt; return its payload.

    The payload is what a reviewer must decide on (``{"choice": ...,
    "hypotheses": [...]}``). The graph is checkpointed per-incident, so the
    resume happens later, on another request — the whole point of a real HITL
    over the previous same-request "interrupt".
    """
    from langgraph.errors import GraphInterrupt  # noqa: F401  (documented import point)

    cfg = incident_run_config(incident_id)
    initial = {"incident_id": incident_id}
    if seed:
        initial.update(seed)
    state = graph.invoke(initial, config=cfg)
    pending = graph.get_state(cfg)
    interrupts = list(getattr(pending, "interrupts", ()) or ())
    payload = interrupts[0].value if interrupts else None
    return {
        "hypotheses": state.get("hypotheses") or [],
        "hypotheses_source": state.get("hypotheses_source"),
        "evidence_validation": state.get("evidence_validation"),
        "pending": bool(pending.next),
        "interrupt": payload,
    }


def resume_incident_run(graph: Any, incident_id: str, decision: dict) -> dict:
    """Resume a parked incident run with the reviewer's decision.

    ``decision`` = ``{"decision": "adopt"|"reject"|"defer", "cause"?: str}``.
    Returns the post-resume state. The *business* persistence of the decision
    (``confirmed_root_cause`` etc.) belongs to the API endpoint, exactly as
    the main chain's confirm endpoint owns the confirmation record.
    """
    from langgraph.types import Command

    cfg = incident_run_config(incident_id)
    state = graph.invoke(Command(resume=decision), config=cfg)
    return {
        "human_decision": state.get("human_decision"),
        "reinspection_result": state.get("reinspection_result"),
        "route_marker": state.get("route_marker"),
    }


def _make_run_reinspection(
    reinspection_callable: Callable[[dict], dict] | None,
) -> Callable[[IncidentSubgraphState], dict]:
    """Bind the optional callable into a plain node function."""
    if reinspection_callable is None:
        return lambda state: _run_reinspection(state, None)
    return lambda state: _run_reinspection(state, reinspection_callable)


def _make_generate_hypotheses(
    attribution_ctx: dict | None,
) -> Callable[[IncidentSubgraphState], dict]:
    """Bind the optional attribution context into the generate node."""
    return lambda state: _generate_hypotheses(state, attribution_ctx)


def _make_validate_evidence(
    attribution_ctx: dict | None,
) -> Callable[[IncidentSubgraphState], dict]:
    return lambda state: _validate_evidence(state, attribution_ctx)
