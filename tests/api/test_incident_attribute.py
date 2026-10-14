"""Attribute endpoint tests (T-1 Task 3 — single-request 3-way attribution).

httpx ASGITransport + SQLite session factory bound via dependency_overrides
(same pattern as tests/api/test_incidents.py). The attribution LLM client and
evidence store are injected as additional dependencies so the router is
testable without a live model or a real data/evidence dir.

Pins:
- adopt writes confirmed_root_cause + persists root_cause_hypotheses
- D8: archived batch -> 409
- unknown incident id (no mock fixture key) -> 200 + empty hypotheses + hint
- D2: no-evidence hypotheses dropped
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import incidents as incidents_router
from app.infra.models import (
    Base,
    DeliveryBatch,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
)

# Same canonical UUID key Task 2 added to the shared fixture. The fixture's
# hypotheses reference "EV-ATT-*" labels; the D2 gate resolves them because
# the test seeds those labels THROUGH the endpoint's overridden
# FileEvidenceStore (save() populates the in-memory ref table the gate's
# store.get reads). The endpoint returns the LLM fixture's label ids.
_ATTRIBUTION_INCIDENT = "8f6e2a10-0000-4000-8000-0000000000aa"
_ATTRIBUTION_INCIDENT_ID = UUID(_ATTRIBUTION_INCIDENT)
# An incident whose UUID has no mock fixture key -> 200 + empty + hint.
_UNKNOWN_INCIDENT_ID = UUID("99999999-1111-2222-3333-444444444409")
_BATCH = UUID("bbbbbbbb-1111-2222-3333-444444444410")
_RULE = UUID("bbbbbbbb-1111-2222-3333-444444444411")
_INSP1 = UUID("bbbbbbbb-1111-2222-3333-444444444412")
_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)


def _seed(session: Any, *, batch_status: str = "isolated") -> None:
    """Seed a minimal batch + module inspection + incidents for the attribute
    endpoint. The attribution incident uses the Task-2 UUID key; the unknown
    incident uses a UUID with no fixture key.
    """
    rule = QualityRuleVersion(
        rule_version_id=_RULE,
        project_id="P-1",
        model="JH-550W",
        name="rv-1",
        nameplate_power=550.0,
        power_tolerance=0.5,
        parameter_standards={},
        current_bands={},
        sample_plan={},
        effective_at=_TS,
        version_no=1,
    )
    batch = DeliveryBatch(
        batch_id=_BATCH,
        batch_no="B-ATTR-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=100,
        rule_version_id=_RULE,
        status=batch_status,
    )
    ins = ModuleInspection(
        inspection_id=_INSP1,
        batch_id=_BATCH,
        sn="SN-A",
        source_type="IV_CSV",
        captured_at=_TS,
        pmax=550.0,
        temp=25.0,
        isc=10.5,
        voc=0.5,
        vm=48.0,
        im=11.0,
        ff=0.75,
        appearance_result="pass",
    )
    session.add(rule)
    session.flush()
    session.add(batch)
    session.flush()
    session.add(ins)

    attribution_incident = QualityIncident(
        incident_id=_ATTRIBUTION_INCIDENT_ID,
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[],
        status="isolated",
    )
    unknown_incident = QualityIncident(
        incident_id=_UNKNOWN_INCIDENT_ID,
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[],
        status="isolated",
    )
    session.add_all([attribution_incident, unknown_incident])
    session.commit()


@pytest.fixture()
def api_client(tmp_path: Any) -> Any:
    from app.plugins.evidence_store import FileEvidenceStore
    from app.plugins.llm_client import MockLLMClient

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()

    fixtures_dir = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    llm = MockLLMClient(fixtures_dir)
    evidence_store = FileEvidenceStore(tmp_path, db=session)
    # Seed the fixture evidence ids THROUGH the store so the D2 gate (the
    # endpoint's overridden FileEvidenceStore) finds them; the store is
    # DB-bound so the same save also INSERTs the evidence rows.
    for _eid in ("EV-ATT-001", "EV-ATT-002", "EV-ATT-003", "EV-ATT-009"):
        evidence_store.save(
            "attribution_fixture",
            {"kind": "fixture", "id": _eid},
            content=b"t",
        )
    session.commit()

    app = FastAPI()
    app.include_router(incidents_router.router)
    app.dependency_overrides[incidents_router.get_session] = lambda: session
    app.dependency_overrides[incidents_router.get_llm] = lambda: llm
    app.dependency_overrides[incidents_router.get_evidence_store] = lambda: evidence_store

    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_attribute_adopt_writes_confirmed_cause(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute",
        json={"decision": "adopt", "cause": "焊接虚焊，建议重焊"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "adopt"
    assert body["incident_id"] == str(_ATTRIBUTION_INCIDENT_ID)
    # Hypotheses persisted to the incident row.
    incident = session.get(QualityIncident, _ATTRIBUTION_INCIDENT_ID)
    assert incident.confirmed_root_cause == "焊接虚焊，建议重焊"
    assert incident.root_cause_hypotheses is not None
    hypos = incident.root_cause_hypotheses
    assert isinstance(hypos, list) and hypos, "D2: at least one with-evidence hypothesis"


def test_attribute_d8_archived_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, batch_status="archived")
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute",
        json={"decision": "adopt", "cause": "x"},
    )
    assert resp.status_code == 409
    assert "archived" in str(resp.json().get("detail", ""))


def test_attribute_unknown_incident_empty_hypotheses(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_UNKNOWN_INCIDENT_ID}/attribute",
        json={"decision": "defer"},
    )
    # No mock fixture key for this UUID -> 200 + empty + hint, not 500/404.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hypotheses"] == []
    assert "hint" in body
    assert body["decision"] == "defer"
    incident = session.get(QualityIncident, _UNKNOWN_INCIDENT_ID)
    # defer must not write confirmed_root_cause.
    assert incident.confirmed_root_cause is None


def test_attribute_d2_no_evidence_dropped(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute",
        json={"decision": "defer"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # D2 gate: every surviving hypothesis carries >=1 evidence id.
    for h in body["hypotheses"]:
        assert h.get("evidence_ids"), "no-evidence hypotheses must be dropped (D2)"


# ---------------------------------------------------------------------------
# Cross-request HITL (2026-10-14 rework): generate/park -> later adopt
# ---------------------------------------------------------------------------


def test_attribute_without_decision_parks_for_human(api_client: Any) -> None:
    """Phase 1: POST /attribute with no decision generates + parks.

    The response carries the hypotheses and ``status=awaiting_human`` — the
    run is waiting on a real human decision, not self-answered.
    """
    client, session, _ = api_client
    _seed(session)
    resp = client.post(f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "awaiting_human"
    assert body["hypotheses"], "the run must return the generated hypotheses"
    assert body["hypotheses_source"] == "attribution"
    # Nothing was adopted yet.
    incident = session.get(QualityIncident, _ATTRIBUTION_INCIDENT_ID)
    assert incident.confirmed_root_cause is None


def test_adopt_resumes_the_parked_run_on_a_later_request(api_client: Any) -> None:
    """Phase 2: a separate request resumes the parked run and adopts."""
    client, session, _ = api_client
    _seed(session)
    start = client.post(f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute", json={})
    assert start.status_code == 200, start.text
    hypotheses = start.json()["hypotheses"]

    adopt = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/adopt",
        json={"decision": "adopt", "cause": "隐裂导致功率衰减"},
    )
    assert adopt.status_code == 200, adopt.text
    body = adopt.json()
    assert body["decision"] == "adopt"
    assert body["hypotheses"] == hypotheses
    incident = session.get(QualityIncident, _ATTRIBUTION_INCIDENT_ID)
    assert incident.confirmed_root_cause == "隐裂导致功率衰减"
    # The human decision is recorded as the graph's resume value too.
    assert body["human_decision"] == {"decision": "adopt", "cause": "隐裂导致功率衰减"}


def test_adopt_without_a_parked_run_is_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/adopt",
        json={"decision": "adopt", "cause": "凭空采纳"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "no_parked_attribution"


def test_adopt_requires_a_cause(api_client: Any) -> None:
    """Adopting a hypothesis without naming the root cause is a 400.

    ``confirmed_root_cause`` is what the closed-incident checks read; an
    adopt that writes nothing would let an incident close with an empty
    root cause.
    """
    client, session, _ = api_client
    _seed(session)
    client.post(f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute", json={})
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/adopt",
        json={"decision": "adopt"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "adopt_requires_cause"


def test_reject_decision_does_not_write_a_root_cause(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    client.post(f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute", json={})
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/adopt",
        json={"decision": "reject"},
    )
    assert resp.status_code == 200, resp.text
    incident = session.get(QualityIncident, _ATTRIBUTION_INCIDENT_ID)
    assert incident.confirmed_root_cause is None


def test_attribute_start_calls_the_model_exactly_once(api_client: Any) -> None:
    """One business action, one model call.

    The endpoint generates the hypotheses; the subgraph it parks must *reuse*
    that list rather than regenerating it — a second call would bill twice and
    write two attribution traces for one action.
    """
    from app.plugins.llm_client import MockLLMClient

    client, session, _ = api_client
    _seed(session)
    calls: list[str] = []
    original = MockLLMClient.chat

    def counting(self: Any, prompt: str, schema: dict) -> dict:
        calls.append(prompt)
        return original(self, prompt, schema)

    MockLLMClient.chat = counting
    try:
        resp = client.post(f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute", json={})
        assert resp.status_code == 200, resp.text
    finally:
        MockLLMClient.chat = original
    assert len(calls) == 1, f"expected exactly one model call, got {len(calls)}"


# ---------------------------------------------------------------------------
# FR-3.6 / §11.3 三选一：采纳 / 驳回 / 修改（+ 保留的 defer）
# ---------------------------------------------------------------------------


def _park(client: Any, incident_id: Any) -> list[dict]:
    """生成假设并停驻，返回假设列表。"""
    r = client.post(f"/api/incidents/{incident_id}/attribute", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_human"
    return r.json()["hypotheses"]


def test_modify_writes_revised_text_and_keeps_original_in_trace(api_client: Any) -> None:
    """「修改」是 FR-3.6 的第三个选项：写人工修订版，trace 保留原假设。

    FR §9.2「AI 给建议，人工担责任」——事后必须能分辨是模型说错了还是
    人改对了，所以原假设与修订版都要留痕。
    """
    client, session, _ = api_client
    _seed(session)
    hyps = _park(client, _ATTRIBUTION_INCIDENT_ID)
    original = hyps[0]["cause"]

    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/adopt",
        json={"decision": "modify", "modified_cause": "现场确认是焊接虚焊", "hypothesis_index": 0},
    )
    assert resp.status_code == 200, resp.text
    incident = session.get(QualityIncident, _ATTRIBUTION_INCIDENT_ID)
    assert incident.confirmed_root_cause == "现场确认是焊接虚焊"
    assert incident.confirmed_root_cause != original, "修改必须写入修订版而非原假设"

    from app.infra.models import AgentTaskTrace

    trace = (
        session.query(AgentTaskTrace)
        .filter(AgentTaskTrace.skill_name == "incident_attribute")
        .order_by(AgentTaskTrace.started_at.desc())
        .first()
    )
    summary = trace.params_summary
    assert summary["decision"] == "modify"
    assert summary["root_cause_source"] == "modified_hypothesis"
    assert summary["hypothesis_cause"] == original, "原假设必须留在轨迹里（可复盘模型错在哪）"
    assert summary["hypothesis_index"] == 0


def test_modify_requires_text(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    _park(client, _ATTRIBUTION_INCIDENT_ID)
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/adopt",
        json={"decision": "modify"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "modify_requires_text"


def test_adopt_uses_stored_hypothesis_not_client_text(api_client: Any) -> None:
    """采纳 = 照抄后端存下来的假设。

    前端传的 cause 改不动它——否则"采纳"与"人工填写"在数据上无法区分，
    FR §8.3「确认根因必须来自人工采纳的假设或人工填写」就证明不了。
    """
    client, session, _ = api_client
    _seed(session)
    hyps = _park(client, _ATTRIBUTION_INCIDENT_ID)

    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/adopt",
        json={"decision": "adopt", "cause": "前端瞎传的文本", "hypothesis_index": 1},
    )
    assert resp.status_code == 200, resp.text
    incident = session.get(QualityIncident, _ATTRIBUTION_INCIDENT_ID)
    assert incident.confirmed_root_cause == hyps[1]["cause"], "采纳必须落库被选假设的原文"

    from app.infra.models import AgentTaskTrace

    trace = (
        session.query(AgentTaskTrace)
        .filter(AgentTaskTrace.skill_name == "incident_attribute")
        .order_by(AgentTaskTrace.started_at.desc())
        .first()
    )
    assert trace.params_summary["root_cause_source"] == "hypothesis"
    assert trace.params_summary["hypothesis_index"] == 1


def test_manual_adopt_marks_source_manual(api_client: Any) -> None:
    """无假设/不认假设时的人工填写走 adopt+cause，trace 记 manual 以区分。"""
    client, session, _ = api_client
    _seed(session)
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/attribute",
        json={"decision": "adopt", "cause": "纯人工判断：来料批次异常"},
    )
    assert resp.status_code == 200, resp.text
    incident = session.get(QualityIncident, _ATTRIBUTION_INCIDENT_ID)
    assert incident.confirmed_root_cause == "纯人工判断：来料批次异常"

    from app.infra.models import AgentTaskTrace

    trace = (
        session.query(AgentTaskTrace)
        .filter(AgentTaskTrace.skill_name == "incident_attribute")
        .order_by(AgentTaskTrace.started_at.desc())
        .first()
    )
    assert trace.params_summary["root_cause_source"] == "manual", "人工填写与采纳必须可区分"


def test_reject_and_defer_write_no_root_cause(api_client: Any) -> None:
    """驳回/搁置都不写业务字段（FR §8.3：未被采纳的不得成为确认根因）。

    两种决策分别在两只异常上验证（同一 fixture 只能播一次种）。
    """
    client, session, _ = api_client
    _seed(session)
    for decision, incident_id in (
        ("reject", _ATTRIBUTION_INCIDENT_ID),
        ("defer", _UNKNOWN_INCIDENT_ID),
    ):
        _park(client, incident_id)
        resp = client.post(
            f"/api/incidents/{incident_id}/adopt",
            json={"decision": decision},
        )
        assert resp.status_code == 200, (decision, resp.text)
        incident = session.get(QualityIncident, incident_id)
        assert incident.confirmed_root_cause is None, decision


def test_defer_is_not_part_of_the_three_way_choice(api_client: Any) -> None:
    """defer 保留但不再是 FR 三选一之一；三选一集合显式可查。"""
    from app.api.incidents import ATTRIBUTION_DECISIONS, ROOT_CAUSE_DECISIONS

    assert set(ROOT_CAUSE_DECISIONS) == {"adopt", "modify"}
    assert "modify" in ATTRIBUTION_DECISIONS
    assert "defer" in ATTRIBUTION_DECISIONS, "搁置作为第四个轻量动作保留"


def test_unknown_decision_lists_allowed_set(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    _park(client, _ATTRIBUTION_INCIDENT_ID)
    resp = client.post(
        f"/api/incidents/{_ATTRIBUTION_INCIDENT_ID}/adopt",
        json={"decision": "approve"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "bad_decision"
    assert "modify" in detail["allowed"]
