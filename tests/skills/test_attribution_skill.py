"""AttributionSkill integration tests (PB Task 5, D2/D3; AGENT.md §4.3).

Runs the full Skill path on SQLite: MockLLMClient (routing on
``incident_id`` extracted from the prompt) + FileEvidenceStore + the
trace store. Pins:

- ``run`` writes ``quality_incidents.root_cause_hypotheses`` (the D2/D3
  validated list) and leaves a ``agent_task_traces`` row
  (``skill_name="attribution"``).
- The LLM call happens OUTSIDE the business transaction; the trace
  ``params_summary`` records only a prompt digest (prompt length + first
  64 chars), never the full prompt / any secret material.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.infra.models import (
    Base,
    DeliveryBatch,
    QualityIncident,
    QualityRuleVersion,
)
from app.plugins.attribution import AttributionSkill
from app.plugins.evidence_store import FileEvidenceStore
from app.plugins.llm_client import MockLLMClient
from app.plugins.trace_store import TraceStore

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_INCIDENT_UUID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeffff0001")
_BATCH_UUID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeffff0002")
_RULE_UUID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeffff0003")


@pytest.fixture()
def sqlite_session() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    rule = QualityRuleVersion(
        rule_version_id=_RULE_UUID,
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
        batch_id=_BATCH_UUID,
        batch_no="B-AT-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=100,
        rule_version_id=_RULE_UUID,
        status="isolated",
    )
    incident = QualityIncident(
        incident_id=_INCIDENT_UUID,
        batch_id=_BATCH_UUID,
        trigger_type="rule_fail",
        severity="major",
        status="isolated",
        evidence_ids=["EV-ATT-001", "EV-ATT-002"],
    )
    session.add(rule)
    session.flush()
    session.add(batch)
    session.flush()
    session.add(incident)
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _make_skill(tmp_path: Any) -> AttributionSkill:
    root = tmp_path / "evidence"
    store = FileEvidenceStore(root=root)
    store.save("attribution", {"kind": "incident", "id": "INC-001"}, content=b"ev-1")
    store.save("attribution", {"kind": "incident", "id": "INC-002"}, content=b"ev-2")
    # The mock routes on incident_id; our incident UUID is the routing key.
    import json

    fixture = tmp_path / "attribution.json"
    fixture.write_text(
        json.dumps(
            {
                str(_INCIDENT_UUID): {
                    "hypotheses": [
                        {
                            "cause": "cell mismatch",
                            "confidence": 0.87,
                            "evidence_ids": [
                                store._by_id and list(store._by_id)[0],
                                list(store._by_id)[1],
                            ],
                            "recommended_action": "re-test",
                        },
                        {
                            "cause": "no-evidence cause",
                            "confidence": 0.40,
                            "evidence_ids": [],
                            "recommended_action": "visual",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    llm = MockLLMClient(fixture_path=fixture)
    return AttributionSkill(llm=llm, evidence_store=store)


def test_skill_writes_hypotheses_and_trace(sqlite_session: Any, tmp_path: Any) -> None:
    skill = _make_skill(tmp_path)
    trace = TraceStore()
    incident: QualityIncident = sqlite_session.get(
        QualityIncident, _INCIDENT_UUID
    )  # type: ignore[union-attr]
    result = skill.run(
        incident,
        trace=trace,
        session=sqlite_session,  # type: ignore[arg-type]
    )
    # run returns the validated hypotheses list (not a dict)
    assert result  # non-empty
    # D2 gate: the no-evidence 0.40 hypothesis was dropped
    assert [h["confidence"] for h in result] == [0.87]
    assert [h["rank"] for h in result] == [1]
    # DB write landed
    incident = sqlite_session.get(QualityIncident, _INCIDENT_UUID)  # type: ignore[union-attr]
    assert incident.root_cause_hypotheses  # non-empty JSONB
    # trace row exists
    rows = trace.list_for_batch(sqlite_session, _BATCH_UUID)  # type: ignore[arg-type]
    assert any(r.skill_name == "attribution" for r in rows)
    assert any(r.structured_output and r.structured_output.get("hypotheses") for r in rows)


def test_skill_trace_records_prompt_digest_not_secret(sqlite_session: Any, tmp_path: Any) -> None:
    skill = _make_skill(tmp_path)
    trace = TraceStore()
    incident: QualityIncident = sqlite_session.get(
        QualityIncident, _INCIDENT_UUID
    )  # type: ignore[union-attr]
    skill.run(incident, trace=trace, session=sqlite_session)  # type: ignore[arg-type]
    rows = trace.list_for_batch(sqlite_session, _BATCH_UUID)  # type: ignore[arg-type]
    att = [r for r in rows if r.skill_name == "attribution"]
    assert att
    summary = att[0].params_summary or {}
    # digest recorded: prompt length + truncated head only
    assert "prompt_len" in summary
    assert "prompt_head" in summary
    assert len(summary["prompt_head"]) <= 64
    # never the full prompt text
    assert "incident_id" not in summary.get("prompt_full", "")
    assert summary.get("model") == "mock"


# ---------------------------------------------------------------------------
# 真模型回复形状容错（2026-10-15：真 endpoint 返回 hypotheses_ranked 导致
# 归因「暂无可用假设」，前端看起来像点击无反应）
# ---------------------------------------------------------------------------


def test_normalize_accepts_live_model_renamed_keys() -> None:
    """真模型把 hypotheses 叫成 hypotheses_ranked，并带额外顶层键。

    这是真实发生的回复形状（agnes-3.0-flash）。严格取 ``hypotheses`` 会
    得到 0 条假设，UI 显示「暂无可用假设」——用户以为按钮坏了。
    """
    from app.plugins.attribution import _normalize_hypotheses

    live_reply = {
        "incident_id": "71b389ad",
        "key_observations": {"pmax_W": 470.0, "ff": 0.74},
        "hypotheses_ranked": [
            {
                "rank": 1,
                "cause": "电池片隐裂",
                "confidence": 0.82,
                "evidence_ids": ["ev-1", "ev-2"],
                "recommended_action": "EL 成像",
            },
            {
                "rank": 2,
                "cause": "层压缺陷",
                "confidence": 0.55,
                "evidence_ids": ["ev-2"],
                "recommended_action": "多光强 IV",
            },
        ],
        "next_step_summary": "先做 EL",
    }
    out = _normalize_hypotheses(live_reply)
    assert len(out) == 2
    assert out[0]["cause"] == "电池片隐裂"
    assert out[0]["confidence"] == 0.82
    assert out[0]["evidence_ids"] == ["ev-1", "ev-2"]
    assert out[0]["recommended_action"] == "EL 成像"


def test_normalize_accepts_alias_keys_and_bad_types() -> None:
    from app.plugins.attribution import _normalize_hypotheses

    # 单数字符串形式的 evidence / 别名键 / 置信度是字符串
    out = _normalize_hypotheses(
        {
            "root_causes": [
                {
                    "description": "焊接虚焊",
                    "score": "0.7",
                    "evidence": "ev-9",
                    "suggestion": "补焊",
                }
            ]
        }
    )
    assert len(out) == 1
    assert out[0]["cause"] == "焊接虚焊"
    assert out[0]["confidence"] == 0.7
    assert out[0]["evidence_ids"] == ["ev-9"]
    assert out[0]["recommended_action"] == "补焊"


def test_normalize_empty_or_unusable_stays_empty() -> None:
    """读不出假设时返回空列表，不凭空编造。"""
    from app.plugins.attribution import _normalize_hypotheses

    assert _normalize_hypotheses({}) == []
    assert _normalize_hypotheses({"hypotheses": []}) == []
    assert _normalize_hypotheses({"hypotheses": [{"unrelated": 1}]}) == []
    assert _normalize_hypotheses("not a dict") == []


def test_skill_uses_normalizer_and_reports_real_model_version() -> None:
    """Skill 走归一化，且 trace 的 model_version 不再谎报 mock。"""
    from app.plugins.attribution import AttributionSkill

    class _RenamedReply:
        model = "agnes-3.0-flash"

        def chat(self, prompt: str, schema: dict) -> dict:
            return {
                "hypotheses_ranked": [
                    {
                        "cause": "c",
                        "confidence": 0.9,
                        "evidence_ids": ["EV-ATT-001"],
                        "recommended_action": "a",
                    }
                ]
            }

    class _Store:
        def get(self, eid: str) -> object:
            if eid == "EV-ATT-001":
                return object()
            raise KeyError(eid)

    captured: dict = {}

    class _Trace:
        def record(self, session: object, **kw: object) -> None:
            captured.update(kw)

    class _Session:
        def flush(self) -> None: ...

    class _Incident:
        incident_id = uuid.UUID("8f6e2a10-0000-4000-8000-0000000000aa")
        batch_id = uuid.UUID("bbbbbbbb-1111-2222-3333-444444444410")
        severity = "major"
        trigger_type = "rule_fail"
        evidence_ids = ["EV-ATT-001"]
        root_cause_hypotheses = None
        confirmed_root_cause = None

    validated = AttributionSkill(_RenamedReply(), _Store()).run(
        _Incident(), trace=_Trace(), session=_Session()
    )
    assert len(validated) == 1, "重命名字段必须仍被采纳"
    assert captured["model_version"] == "agnes-3.0-flash"
    assert captured["params_summary"]["model"] == "agnes-3.0-flash"
