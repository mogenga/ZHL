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
from pathlib import Path
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


def test_c9_stub_markers_in_state() -> None:
    """C9 regression: the pass-through stub nodes must mark themselves in
    state so trace consumers can store validation_result='stub' instead of
    mistaking a stub for a real skill output."""
    graph = build_incident_subgraph(MemorySaver())
    state = _initial_state()
    for _ in graph.stream(state, config=_config(), stream_mode="updates"):
        pass
    snap = graph.get_state(_config())
    assert snap is not None
    # Both stub nodes must have run and marked themselves.
    assert snap.values.get("hypotheses_source") == "stub"
    assert snap.values.get("evidence_validation") == "stub"


# ---------------------------------------------------------------------------
# Task 1: attribution wiring (closure-injected AttributionSkill replaces stub)
# ---------------------------------------------------------------------------
# UUID fixture key (Task 2 adds it to mock_llm_attribution.json; reused
# verbatim by Task 3 endpoint tests).
_TASK1_UUID_INCIDENT = "8f6e2a10-0000-4000-8000-0000000000aa"


def test_seeded_path_keeps_stub_source() -> None:
    """C9 contract regression: pre-seeded hypotheses without attribution_ctx
    must keep the pass-through ``stub`` marker so trace consumers never
    mistake a pre-seed for a real attribution result. (T-1 adds the
    attribution marker; this test pins that the seed path stays honest.)
    """
    graph = build_incident_subgraph(MemorySaver())
    state = _initial_state()  # hypotheses non-empty
    for _ in graph.stream(state, config=_config(), stream_mode="updates"):
        pass
    snap = graph.get_state(_config())
    assert snap is not None
    assert snap.values.get("hypotheses_source") == "stub"
    assert snap.values.get("evidence_validation") == "stub"


def _attribution_ctx(tmp_path):
    """Build an attribution_ctx: SQLite incident + MockLLMClient + TraceStore.

    The tmp_path-scoped attribution fixture is written here (test-only), so
    Task 1 is self-contained; Task 2 adds the SAME-structure canonical key to
    the shared tests/fixtures/mock_llm_attribution.json.

    Evidence is saved THROUGH the FileEvidenceStore (``store.save``) so the
    D2 gate (``store.get`` on the in-memory ref table) finds it; the store is
    DB-bound so the same ``save`` also INSERTs the ``evidence`` row.
    """
    import json
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.infra.models import (
        Base,
        DeliveryBatch,
        QualityIncident,
        QualityRuleVersion,
    )
    from app.plugins.evidence_store import FileEvidenceStore
    from app.plugins.llm_client import MockLLMClient
    from app.plugins.trace_store import TraceStore

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    fixture_dir = Path(tmp_path)  # fresh dir per test, doubles as evidence root

    rule = QualityRuleVersion(
        rule_version_id=UUID("cccccccc-0000-4000-8000-000000000002"),
        project_id="P-1",
        model="JH-550W",
        name="rv-1",
        nameplate_power=550.0,
        power_tolerance=0.5,
        parameter_standards={},
        current_bands={},
        sample_plan={},
        effective_at=datetime(2026, 6, 1, tzinfo=UTC),
        version_no=1,
    )
    batch = DeliveryBatch(
        batch_id=UUID("cccccccc-0000-4000-8000-000000000001"),
        batch_no="B-T1",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=10,
        rule_version_id=rule.rule_version_id,
        status="isolated",
    )
    session.add_all([rule, batch])

    evidence_store = FileEvidenceStore(fixture_dir, db=session)
    # Save the evidence THROUGH the store: populates the in-memory ref table
    # (so the D2 gate's store.get finds it) AND INSERTs the DB row (db-bound).
    # The store mints a deterministic evidence_id from the object_ref + etype,
    # so the fixture hypothesis must reference THAT id — written AFTER the save.
    ev_ref = evidence_store.save(
        "decision_snapshot",
        {"kind": "module", "id": "SN-T1"},
        content=b"t1-evidence-bytes",
        now=datetime(2026, 6, 1, tzinfo=UTC),
    )
    # (Re)write the attribution fixture now that we know the real evidence id.
    (fixture_dir / "mock_llm_attribution.json").write_text(
        json.dumps(
            {
                _TASK1_UUID_INCIDENT: {
                    "hypotheses": [
                        {
                            "cause": "cell mismatch",
                            "confidence": 0.9,
                            "evidence_ids": [ev_ref.evidence_id],
                            "recommended_action": "resolder",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    incident = QualityIncident(
        incident_id=UUID(_TASK1_UUID_INCIDENT),
        batch_id=batch.batch_id,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[ev_ref.evidence_id],
        status="isolated",
    )
    session.add(incident)
    session.commit()

    ctx = {
        "incident": incident,
        "session": session,
        "llm": MockLLMClient(fixture_path=fixture_dir / "mock_llm_attribution.json"),
        "evidence_store": evidence_store,
        "trace": TraceStore(),
    }
    return ctx, engine


def test_attribution_path_invokes_skill(tmp_path) -> None:
    """hypotheses empty + attribution_ctx -> Skill runs, source=attribution,
    incident.root_cause_hypotheses written (D2: every hypothesis has evidence)."""
    ctx, engine = _attribution_ctx(tmp_path)
    graph = build_incident_subgraph(MemorySaver(), attribution_ctx=ctx)
    state = {
        "incident_id": _TASK1_UUID_INCIDENT,
        "batch_id": ctx["incident"].batch_id.hex,
        "hypotheses": [],
        "human_decision": None,
        "reinspection_result": None,
        "reinspection_payload": {"scope_type": "minimal", "scope_sns": ["SN-T1"]},
    }
    for _ in graph.stream(state, config=_config(), stream_mode="updates"):
        pass
    snap = graph.get_state(_config())
    assert snap is not None
    assert snap.values.get("hypotheses_source") == "attribution"
    assert snap.values.get("evidence_validation") == "skill"
    hypos = snap.values.get("hypotheses") or []
    assert hypos, "attribution must yield hypotheses from the fixture"
    for h in hypos:
        assert h["evidence_ids"], "D2: no-evidence hypotheses must be dropped"
    assert ctx["incident"].root_cause_hypotheses is not None
    engine.dispose()


def test_attribution_d2_drops_no_evidence(tmp_path) -> None:
    """Fixture hypotheses with empty evidence_ids are dropped by the Skill gate."""
    ctx, engine = _attribution_ctx(tmp_path)
    # Re-point the mock to a fixture whose only hypothesis has no evidence.
    import json
    from pathlib import Path

    from app.plugins.llm_client import MockLLMClient

    bare_dir = Path(tmp_path) / "bare"
    bare_dir.mkdir()
    (bare_dir / "mock_llm_attribution.json").write_text(
        json.dumps(
            {
                _TASK1_UUID_INCIDENT: {
                    "hypotheses": [
                        {
                            "cause": "guess",
                            "confidence": 0.5,
                            "evidence_ids": [],
                            "recommended_action": "none",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    ctx["llm"] = MockLLMClient(fixture_path=bare_dir / "mock_llm_attribution.json")
    graph = build_incident_subgraph(MemorySaver(), attribution_ctx=ctx)
    state = {
        "incident_id": _TASK1_UUID_INCIDENT,
        "batch_id": ctx["incident"].batch_id.hex,
        "hypotheses": [],
        "human_decision": None,
        "reinspection_result": None,
        "reinspection_payload": {"scope_type": "minimal", "scope_sns": ["SN-T1"]},
    }
    for _ in graph.stream(state, config=_config(), stream_mode="updates"):
        pass
    snap = graph.get_state(_config())
    assert snap is not None
    # D2: the no-evidence hypothesis is dropped -> empty list.
    assert snap.values.get("hypotheses") == []
    engine.dispose()
