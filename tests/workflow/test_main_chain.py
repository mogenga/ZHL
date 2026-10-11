"""Main-chain graph replay tests (PA plan Task 7 / AGENT.md §3.3 workflow).

InMemorySaver + stub deps (deterministic LLMClient / counting evidence store /
stub repositories + a real SQLite session so the P0-frozen judge Skill and
sampling Plugin run for real). Pins:

- Review Focus #1: confidence boundary 0.8 (auto-confirm) vs 0.79 (interrupt)
- Review Focus #2: resume does NOT replay upstream nodes (checkpoint semantics)
- Review Focus #5: sample rule_missing -> isolated branch (incident mount point)
- thread isolation on one shared InMemorySaver
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.domain.types import IsoRow
from app.infra.models import Base, DeliveryBatch, QualityRuleVersion
from app.infra.repositories import InspectionRepository

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.plugins.evidence_store import EvidenceStore
    from app.plugins.llm_client import LLMClient

_ROOT = Path(__file__).resolve().parents[2]
_SEED = json.loads((_ROOT / "tests" / "fixtures" / "rules_seed.json").read_text(encoding="utf-8"))[
    "rules"
][0]
_ISO_TABLE = [
    IsoRow(**row)
    for row in json.loads(
        (_ROOT / "tests" / "fixtures" / "iso2859_1_aql.json").read_text(encoding="utf-8")
    )["rows"]
]
_INSPECTION_TYPE = _SEED["sample_plan"]["inspection_type"]
_LEVEL = _SEED["sample_plan"]["inspection_level"]
_AQL = float(_SEED["sample_plan"]["aql"])
_LOT = 10  # planned_quantity used by the hit-path tests ([9,15] interval)
# The single preset row matching the seed plan's combo at the hit lot size
_HIT_COMBOS = [
    r
    for r in _ISO_TABLE
    if r.inspection_type == _INSPECTION_TYPE
    and r.inspection_level == _LEVEL
    and abs(r.aql - _AQL) < 1e-9
    and r.lot_size_min <= _LOT <= r.lot_size_max
]
# A table where the seed plan's combo is absent -> iso_lookup None -> rule_missing
_RULE_MISSING_TABLE = [r for r in _ISO_TABLE if r not in _HIT_COMBOS]
assert _HIT_COMBOS, "fixture must offer a hit combo for the seed plan at lot 10"
assert _RULE_MISSING_TABLE, "fixture must offer a table with the combo missing"


class CountingLLM:
    """Deterministic LLMClient stub: routes on the [SOURCE_SN] marker, returns
    a fixed confidence (per extraction), counts calls (Review Focus #2 probe).
    """

    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        self.call_count = 0

    def chat(self, prompt: str, schema: dict) -> dict:
        self.call_count += 1
        sn = prompt.split("[SOURCE_SN:")[-1].rstrip("]")
        return {
            "source_sn": sn,
            "extracted_params": {},
            "appearance_findings": [],
            "confidence": self.confidence,
        }


class CountingEvidence:
    """EvidenceStore stub: counts saves per type; records evidence ids.

    Mirrors the protocol of ``FileEvidenceStore`` (save / get /
    list_by_object) without touching the filesystem; ``save`` returns an
    ``EvidenceRef`` (frozen dataclass) so graph nodes can read
    ``evidence_id`` from the return value, exactly like the real store.
    """

    def __init__(self) -> None:
        self.count = 0
        self.by_type: dict[str, int] = {}
        self.evidence_ids: list[str] = []

    def save(
        self,
        etype: str,
        object_ref: dict,
        *,
        content: bytes | None = None,
        path: str | None = None,
        meta: dict | None = None,
        now: datetime | None = None,
    ):
        from app.plugins.evidence_store import EvidenceRef

        self.count += 1
        self.by_type[etype] = self.by_type.get(etype, 0) + 1
        ref_id = f"ev-{self.count}"
        self.evidence_ids.append(ref_id)
        return EvidenceRef(
            evidence_id=ref_id,
            evidence_type=etype,
            object_ref=dict(object_ref),
            content_hash="",
            meta=dict(meta or {}),
        )

    def get(self, evidence_id: str):

        raise KeyError("stub store keeps no payloads")

    def list_by_object(self, object_ref: dict) -> list:
        from app.plugins.evidence_store import EvidenceRef

        want = {k: object_ref.get(k) for k in ("kind", "id")}
        return [
            EvidenceRef(evidence_id=eid, evidence_type="", object_ref=dict(want), content_hash="")
            for eid in self.evidence_ids
        ]


class StubInspectRepo:
    """InspectionRepository stand-in: deterministic row data, no DB.

    ``create_inspections`` counts intake writes (Review Focus #2 probe);
    ``list_batch_rows`` returns the seeded inspection rows (B5 shape) so
    judge / sampling nodes run over them.
    """

    def __init__(self, rows: list[dict]) -> None:
        self._rows = [dict(r) for r in rows]
        self.created = 0

    def create_inspections(self, session, batch_id, rows: list[dict]):
        self.created += 1
        return [dict(r, inspection_id=uuid.uuid4()) for r in rows]

    def get_batch_sns(self, session, batch_id) -> list[str]:
        return [r["sn"] for r in self._rows]

    def confirm_inspection(self, session, inspection_id, fields: dict | None):
        return None

    def reject_inspection(self, session, inspection_id):
        return None

    def list_batch_rows(self, session, batch_id) -> list[dict]:
        return [dict(r) for r in self._rows]


def _inspection_rows(sns: list[str], pmax_by_sn: dict[str, float] | None = None) -> list[dict]:
    return [
        {
            "sn": sn,
            "source_type": "IV_CSV",
            "captured_at": datetime(2026, 10, 1, tzinfo=UTC),
            "pmax": (pmax_by_sn or {}).get(sn, 550.0),
            "temp": 25.0,
            "isc": 10.5,
            "voc": 48.0,
            "vm": 40.0,
            "im": 13.0,
            "ff": 0.8,
            "appearance_result": "pass",
            "intake_status": "confirmed",
        }
        for sn in sns
    ]


def _make_env(
    confidence: float,
    sns: list[str],
    rule_missing: bool = False,
    pmax_by_sn: dict[str, float] | None = None,
    lot_override: int | None = None,
) -> dict:
    """Real SQLite engine + batch + seeded inspections; stub LLM / evidence.

    The graph's sample node reads lot size from the batch row
    (``planned_quantity``); the default is ``len(sns)``. The rule-missing
    case keeps the lot inside the [9,15] interval so the hit combo works,
    but swaps the ISO table for one missing the seed plan's combo.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    lot = lot_override if lot_override is not None else len(sns)
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    rule = QualityRuleVersion(
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        name="JH-550W default",
        nameplate_power=_SEED["nameplate_power"],
        power_tolerance=_SEED["power_tolerance"],
        parameter_standards=_SEED["parameter_standards"],
        current_bands=_SEED["current_bands"],
        sample_plan=_SEED["sample_plan"],
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="published",
        version_no=_SEED["version_no"],
    )
    s.add(rule)
    s.flush()
    s.commit()
    batch = DeliveryBatch(
        batch_no=f"B-G-{uuid.uuid4().hex[:6]}",
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        planned_quantity=lot,
        rule_version_id=rule.rule_version_id,
    )
    s.add(batch)
    s.flush()
    _seed_inspections(s, batch, sns, pmax_by_sn)

    rows = _inspection_rows(sns, pmax_by_sn)
    return {
        "engine": engine,
        "session": s,
        "rule_version_id": rule.rule_version_id,
        "batch_id": str(batch.batch_id),
        "llm": CountingLLM(confidence),
        "evidence": CountingEvidence(),
        "inspect_repo": StubInspectRepo(rows),
        "iso_table": _RULE_MISSING_TABLE if rule_missing else _HIT_COMBOS,
    }


def _seed_inspections(
    session: Session, batch: DeliveryBatch, sns: list[str], pmax_by_sn: dict[str, float] | None
) -> None:
    rows = [
        {
            "sn": sn,
            "source_type": "IV_CSV",
            "captured_at": datetime(2026, 10, 1, tzinfo=UTC),
            "pmax": (pmax_by_sn or {}).get(sn, 550.0),
            "temp": 25.0,
            "isc": 10.5,
            "voc": 48.0,
            "vm": 40.0,
            "im": 13.0,
            "ff": 0.8,
            "appearance_result": "pass",
        }
        for sn in sns
    ]
    created = InspectionRepository().create_inspections(session, batch.batch_id, rows)
    for c in created:
        c.intake_status = "confirmed"
    session.flush()


def _initial_inputs(env: dict) -> dict:
    rows = env["inspect_repo"].list_batch_rows(None, None)
    return {
        "batch_id": env["batch_id"],
        "rule_version_id": str(env["rule_version_id"]),
        "batch_status": "pending_intake",
        "inspections": rows,
        "extraction": {},
        "confidence": 0.0,
        "low_conf_sns": [],
        "intake_status": "pending",
        "decision": {},
        "sample": {},
        "transition_result": {},
        "evidence_ids": [],
    }


class MainChainDepsLocal(TypedDict, total=False):
    """Test-side mirror of ``MainChainDeps`` keys the graph reads."""

    session: Session
    inspect_repo: object
    llm: LLMClient
    evidence: EvidenceStore
    iso_table: list
    rule_version_id: uuid.UUID
    inspection_type: str
    aql: float
    inspection_level: str
    lot_size: int


def _deps(env: dict) -> dict:
    return {
        "session": env["session"],
        "inspect_repo": env["inspect_repo"],
        "llm": env["llm"],
        "evidence": env["evidence"],
        "iso_table": env["iso_table"],
        "rule_version_id": env["rule_version_id"],
        "inspection_type": _INSPECTION_TYPE,
        "inspection_level": _LEVEL,
    }


def _run_to_completion(graph, inputs: dict, thread_id: str) -> dict:
    """Stream until the graph reaches END (or pauses); return the final values."""
    for _chunk in graph.stream(inputs, config={"configurable": {"thread_id": thread_id}}):
        pass
    return _final_state(graph, thread_id)


def _final_state(graph, thread_id: str) -> dict:
    return graph.get_state({"configurable": {"thread_id": thread_id}}).values or {}


# ---------------------------------------------------------------------------
# Test cases (PA plan Task 7 Step 1)
# ---------------------------------------------------------------------------


def test_full_pass_path() -> None:
    from app.workflow.main_chain import build_main_chain_graph

    sns = [f"SN-G-{i:02d}" for i in range(_LOT)]
    env = _make_env(confidence=0.9, sns=sns)
    graph = build_main_chain_graph(_deps(env), checkpointer=InMemorySaver())
    state = _run_to_completion(graph, _initial_inputs(env), thread_id=env["batch_id"])

    assert not state["low_conf_sns"]
    assert state["intake_status"] == "confirmed"
    assert state["decision"]["result"] == "Pass"
    assert state["sample"]["result"] == "pass"
    assert state["sample"]["n"] == 13  # ISO row n at lot 10, aql 1.0
    assert state["batch_status"] == "pending_release"
    assert state["transition_result"]["accepted"] is True
    assert state["evidence_ids"]


def test_confidence_boundary_auto_confirm() -> None:
    """Review Focus #1: confidence == 0.8 exactly -> no interrupt, auto-confirm."""
    from app.workflow.main_chain import build_main_chain_graph

    sns = [f"SN-B-{i:02d}" for i in range(_LOT)]
    env = _make_env(confidence=0.8, sns=sns)
    graph = build_main_chain_graph(_deps(env), checkpointer=InMemorySaver())
    state = _run_to_completion(graph, _initial_inputs(env), thread_id=env["batch_id"])

    assert env["llm"].call_count == _LOT  # one extraction per SN
    assert state["intake_status"] == "confirmed"
    assert state["low_conf_sns"] == []
    assert state["batch_status"] == "pending_release"


def test_low_conf_interrupt() -> None:
    """Review Focus #1: confidence 0.79 -> interrupt; resume confirm -> release."""
    from app.workflow.main_chain import build_main_chain_graph

    sns = [f"SN-L-{i:02d}" for i in range(_LOT)]
    env = _make_env(confidence=0.79, sns=sns)
    graph = build_main_chain_graph(_deps(env), checkpointer=InMemorySaver())
    inputs = _initial_inputs(env)

    for _chunk in graph.stream(inputs, config={"configurable": {"thread_id": env["batch_id"]}}):
        pass
    snapshot = graph.get_state({"configurable": {"thread_id": env["batch_id"]}})
    assert snapshot.next, "confidence 0.79 < 0.8 must interrupt at human_confirm"
    state = snapshot.values
    assert state["intake_status"] == "pending"
    assert set(state["low_conf_sns"]) == {f"SN-L-{i:02d}" for i in range(_LOT)}

    for _chunk in graph.stream(
        Command(resume={"action": "confirm"}),
        config={"configurable": {"thread_id": env["batch_id"]}},
    ):
        pass
    state2 = _final_state(graph, env["batch_id"])
    assert state2["intake_status"] == "confirmed"
    assert state2["batch_status"] == "pending_release"
    assert state2["decision"]["result"] == "Pass"


def test_reject_path() -> None:
    """Review Focus #1 (reject side): resume reject -> END, judge/sample not run."""
    from app.workflow.main_chain import build_main_chain_graph

    env = _make_env(confidence=0.7, sns=[f"SN-R-{i:02d}" for i in range(3)])
    graph = build_main_chain_graph(_deps(env), checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": env["batch_id"]}}
    for _chunk in graph.stream(_initial_inputs(env), config=cfg):
        pass
    assert graph.get_state(cfg).next
    for _chunk in graph.stream(Command(resume={"action": "reject"}), config=cfg):
        pass
    state = _final_state(graph, env["batch_id"])
    assert state["intake_status"] == "rejected"
    assert state["decision"] in ({}, None)
    assert state["sample"] in ({}, None)
    # extraction evidence kept, no decision_snapshot written
    assert "extraction_result" in env["evidence"].by_type
    assert "decision_snapshot" not in env["evidence"].by_type


def test_resume_does_not_replay_upstream() -> None:
    """Review Focus #2: after resume, upstream side effects are unchanged."""
    from app.workflow.main_chain import build_main_chain_graph

    env = _make_env(confidence=0.79, sns=[f"SN-P-{i:02d}" for i in range(_LOT)])
    graph = build_main_chain_graph(_deps(env), checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": env["batch_id"]}}
    for _chunk in graph.stream(_initial_inputs(env), config=cfg):
        pass
    assert graph.get_state(cfg).next
    llm_at_interrupt = env["llm"].call_count
    intake_writes_at_interrupt = env["inspect_repo"].created
    assert llm_at_interrupt == _LOT and intake_writes_at_interrupt == 1

    for _chunk in graph.stream(Command(resume={"action": "confirm"}), config=cfg):
        pass
    # upstream nodes (intake + extract) must NOT re-run:
    assert env["llm"].call_count == llm_at_interrupt
    assert env["inspect_repo"].created == intake_writes_at_interrupt


def test_rule_missing_routes_isolated() -> None:
    """Review Focus #5: sample combo missing -> rule_missing -> isolated."""
    from app.workflow.main_chain import build_main_chain_graph

    sns = [f"SN-M-{i:02d}" for i in range(_LOT)]
    env = _make_env(confidence=0.9, sns=sns, rule_missing=True)
    graph = build_main_chain_graph(_deps(env), checkpointer=InMemorySaver())
    state = _run_to_completion(graph, _initial_inputs(env), thread_id=env["batch_id"])

    assert state["decision"]["result"] == "Pass"
    assert state["sample"]["result"] == "rule_missing"
    assert state["batch_status"] == "isolated"
    assert state["transition_result"]["accepted"] is True


def test_thread_isolation() -> None:
    """Two thread_ids on ONE shared InMemorySaver: state must not leak."""
    from app.workflow.main_chain import build_main_chain_graph

    saver = InMemorySaver()
    env_a = _make_env(confidence=0.9, sns=[f"SN-A-{i:02d}" for i in range(_LOT)])
    env_b = _make_env(confidence=0.79, sns=[f"SN-B-{i:02d}" for i in range(_LOT)])
    graph_a = build_main_chain_graph(_deps(env_a), checkpointer=saver)
    graph_b = build_main_chain_graph(_deps(env_b), checkpointer=saver)

    graph_a.invoke(_initial_inputs(env_a), config={"configurable": {"thread_id": "thread-A"}})
    state_a = graph_a.get_state({"configurable": {"thread_id": "thread-A"}}).values

    graph_b.invoke(_initial_inputs(env_b), config={"configurable": {"thread_id": "thread-B"}})
    snap_b = graph_b.get_state({"configurable": {"thread_id": "thread-B"}})
    assert snap_b.next, "thread B must be paused at the interrupt"

    # A's completed state is untouched by B's interruption:
    assert state_a["batch_status"] == "pending_release"
    assert state_a["decision"]["result"] == "Pass"
    # B has not reached judge yet:
    assert not snap_b.values.get("decision") or not snap_b.values.get("decision", {}).get("result")
