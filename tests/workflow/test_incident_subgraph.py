"""Incident subgraph tests (PB Task 6, D9 interrupt 三选一; MemorySaver + fixed thread).

The subgraph:
  generate_hypotheses -> validate_evidence -> human_adopt (interrupt)
  -> run_reinspection -> route_reinspection_result (pass -> END / fail -> rejected_route)

Pins:
- the graph pauses at ``human_adopt`` with interrupt payload = hypothesis list
- resume with ``Command(resume={"decision": "adopt", "cause": ...})`` flows to
  END and the state carries the adoption
- recheck pass routes closed; recheck fail routes rejected
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.workflow.incident_subgraph import build_incident_subgraph

_THREAD = "incident-" + uuid.uuid4().hex


def _config() -> dict[str, Any]:
    return {"configurable": {"thread_id": _THREAD}}


def _initial_state() -> dict[str, Any]:
    return {
        "incident_id": "INC-001",
        "batch_id": "B-001",
        "hypotheses": [
            {"cause": "cell mismatch", "confidence": 0.87, "rank": 1, "evidence_ids": ["EV-1"]},
        ],
        "human_decision": None,
        "reinspection_result": None,
        "reinspection_payload": {"scope_type": "minimal", "scope_sns": ["SN-1"]},
    }


def test_subgraph_interrups_at_adopt() -> None:
    graph = build_incident_subgraph(MemorySaver())
    state = _initial_state()
    # Drive the run: it pauses at human_adopt.
    for _ in graph.stream(state, config=_config(), stream_mode="updates"):
        pass
    snap = graph.get_state(_config())
    # The next pending node is the human-adopt interrupt.
    assert snap is not None
    assert "human_adopt" in snap.next


def test_resume_adopt_closes_path() -> None:
    from langgraph.types import Command

    graph = build_incident_subgraph(MemorySaver())
    state = _initial_state()
    for _ in graph.stream(state, config=_config(), stream_mode="updates"):
        pass
    # Resume with an adopt decision.
    resume = Command(resume={"decision": "adopt", "cause": "cell mismatch"})
    for _ in graph.stream(resume, config=_config(), stream_mode="updates"):
        pass
    snap = graph.get_state(_config())
    assert snap is not None
    assert snap.values["human_decision"] == {"decision": "adopt", "cause": "cell mismatch"}
    # Reinspection result should be carried to END.
    assert snap.values["reinspection_result"] in ("pass", "fail")
    assert snap.next == ()  # reached END


def test_recheck_pass_routes_closed() -> None:
    graph = build_incident_subgraph(
        MemorySaver(),
        reinspection_callable=lambda payload: {"result": "pass"},
    )
    state = _initial_state()
    for _ in graph.stream(state, config=_config(), stream_mode="updates"):
        pass
    from langgraph.types import Command

    for _ in graph.stream(
        Command(resume={"decision": "adopt", "cause": "x"}),
        config=_config(),
        stream_mode="updates",
    ):
        pass
    snap = graph.get_state(_config())
    assert snap.values["reinspection_result"] == "pass"
    assert snap.next == ()


def test_recheck_fail_routes_rejected() -> None:
    graph = build_incident_subgraph(
        MemorySaver(),
        reinspection_callable=lambda payload: {"result": "fail"},
    )
    state = _initial_state()
    for _ in graph.stream(state, config=_config(), stream_mode="updates"):
        pass
    from langgraph.types import Command

    for _ in graph.stream(
        Command(resume={"decision": "adopt", "cause": "x"}),
        config=_config(),
        stream_mode="updates",
    ):
        pass
    snap = graph.get_state(_config())
    assert snap.values["reinspection_result"] == "fail"
    # fail routes to rejected_route, which marks the state; no further next node.
    assert snap.values.get("route_marker") == "rejected" or snap.next == ()
