"""Release endpoint tests (PB Task 8, D4 fail-keeps-state; D7-7 item 7).

The release API assembles all seven ``assert_release`` inputs server-side
from the DB and calls the pure domain function. Pins:

- all-pass -> ``released`` + release-evidence row + trace; NOT auto-archived
  in this transaction (archive happens at the report call, D6a semantics)
- any assertion fail -> batch stays ``pending_release``, ``release_decision``
  stays ``undecided``, the failing check's reason names the incident/SN;
  no state transition is written
- item 7 scope gap -> ``reinspection_scope_covered`` fail, reason lists the
  missing SNs
- retry after fixing the blocker -> all-pass -> released (idempotent
  semantics: the assertion re-runs, only the final pass writes state)
- batch not pending_release -> 409; archived -> 409 (D8)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import release as release_api
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    IncidentSnLink,
    IsoSamplingRow,
    ModuleInspection,
    QualityIncident,
    QualityRuleVersion,
    Reinspection,
    SampleConclusion,
)

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_BATCH = UUID("eeeeeeee-1111-2222-3333-444444444401")
_RULE = UUID("eeeeeeee-1111-2222-3333-444444444402")
_INSP1 = UUID("eeeeeeee-1111-2222-3333-444444444403")
_INSP2 = UUID("eeeeeeee-1111-2222-3333-444444444404")
_INSP3 = UUID("eeeeeeee-1111-2222-3333-444444444405")
_INC = UUID("eeeeeeee-1111-2222-3333-444444444406")
_EV1 = UUID("eeeeeeee-1111-2222-3333-444444444407")
_RE1 = UUID("eeeeeeee-1111-2222-3333-444444444408")
_CONC1 = UUID("eeeeeeee-1111-2222-3333-444444444409")


def _mk_ins(session: Any, iid: UUID, sn: str, pmax: float) -> ModuleInspection:
    ins = ModuleInspection(
        inspection_id=iid,
        batch_id=_BATCH,
        sn=sn,
        source_type="IV_CSV",
        captured_at=_TS,
        pmax=pmax,
        temp=25.0,
        isc=10.5,
        voc=0.5,
        vm=48.0,
        im=11.0,
        ff=0.75,
        appearance_result="pass",
    )
    session.add(ins)
    return ins


#: 生产形状的规则抽样方案（001_init.sql 种子：iv / normal / AQL 1.0）。
_DEFAULT_PLAN = {"inspection_type": "iv", "inspection_level": "normal", "aql": 1.0}


def _seed(
    session: Any,
    *,
    incident_capa_verified: bool = True,
    reinspection_scope: list[str] | None = None,
    reinspection_present: bool = True,
    sample_result: str = "pass",
    incident_status: str = "pending_recheck",
    sample_plan: dict | None = None,
    with_conclusion: bool = True,
    conclusion_inspection_type: str = "iv",
    evidence_object_ref: dict | None = None,
    iso_rows: list[tuple] | None = None,
    planned_quantity: int = 3,
) -> dict[str, Any]:
    rule = QualityRuleVersion(
        rule_version_id=_RULE,
        project_id="P-1",
        model="JH-550W",
        name="rv-1",
        nameplate_power=550.0,
        power_tolerance=0.5,
        parameter_standards={},
        current_bands={},
        sample_plan=dict(_DEFAULT_PLAN if sample_plan is None else sample_plan),
        effective_at=_TS,
        version_no=1,
    )
    batch = DeliveryBatch(
        batch_id=_BATCH,
        batch_no="B-REL-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=planned_quantity,
        rule_version_id=_RULE,
        status="pending_release",
    )
    ev = Evidence(
        evidence_id=_EV1,
        evidence_type="decision_snapshot",
        object_ref=(
            {"kind": "batch", "id": "B-REL-001"}
            if evidence_object_ref is None
            else dict(evidence_object_ref)
        ),
        content_hash="sha256:222",
        generated_at=_TS,
        generated_by="test",
    )
    conclusion = SampleConclusion(
        conclusion_id=_CONC1,
        batch_id=_BATCH,
        inspection_type=conclusion_inspection_type,
        rule_version_id=_RULE,
        sample_round=1,
        n=3,
        ac=0,
        re=1,
        result=sample_result,
        evidence_id=_EV1,
    )
    # ISO 行按规则 sample_plan 的真实组合（iv / normal / AQL 1.0）落库；测试
    # 不再靠"实现里硬编码的 combo"来配套，否则实现写死什么测试就跟着塞什么。
    iso_rows = iso_rows if iso_rows is not None else [("iv", "normal", 1.0, 1, 100, 3, 0, 1)]
    iso_objs = [
        IsoSamplingRow(
            inspection_type=t,
            inspection_level=lvl,
            aql=aql,
            lot_size_min=lo,
            lot_size_max=hi,
            n=n,
            ac=ac,
            re=re,
        )
        for (t, lvl, aql, lo, hi, n, ac, re) in iso_rows
    ]
    incident = QualityIncident(
        incident_id=_INC,
        batch_id=_BATCH,
        trigger_type="rule_fail",
        severity="major",
        evidence_ids=[str(_EV1)],
        status=incident_status,
        owner="ZHL-QA",
        due_at=_TS,
        confirmed_root_cause="cell mismatch",
        corrective_action="replace lead cell",
        capa_verified=incident_capa_verified,
    )
    session.add_all([rule, ev] + iso_objs)
    session.flush()
    session.add(batch)
    session.flush()
    ins1 = _mk_ins(session, _INSP1, "SN-A", 550.0)
    ins2 = _mk_ins(session, _INSP2, "SN-B", 545.0)
    ins3 = _mk_ins(session, _INSP3, "SN-C", 540.0)
    session.add_all([ins1, ins2, ins3, incident])
    session.flush()
    if with_conclusion:
        session.add(conclusion)
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP1))
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP2))
    session.add(IncidentSnLink(incident_id=_INC, inspection_id=_INSP3))

    if reinspection_present:
        scope = reinspection_scope if reinspection_scope is not None else ["SN-A", "SN-B", "SN-C"]
        re_ = Reinspection(
            reinspection_id=_RE1,
            incident_id=_INC,
            inspection_id=_INSP1,
            rule_version_id=_RULE,
            pmax=550.0,
            temp=25.0,
            isc=10.5,
            voc=0.5,
            vm=48.0,
            im=11.0,
            ff=0.75,
            result="pass",
            executor="ZHL-QA",
            executed_at=_TS,
            evidence_id=_EV1,
            scope_type="minimal",
            scope_sns=scope,
        )
        session.add(re_)
        # 循环外键：复检行先落库再回填（同 reinspections.py 生产修复）
        session.flush()
        incident.verification_id = re_.reinspection_id
    session.commit()
    return {"incident": incident, "batch": batch}


@pytest.fixture()
def api_client() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(release_api.router)
    app.dependency_overrides[release_api.get_session] = lambda: session
    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_release_all_pass_releases(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    assert body["status"] == "released"
    assert session.get(DeliveryBatch, _BATCH).status == "released"
    # NOT auto-archived in this transaction (archive happens at the report call)
    assert session.get(DeliveryBatch, _BATCH).status != "archived"
    # release evidence row written
    ev = session.query(Evidence).filter(Evidence.evidence_type == "human_confirmation").all()
    assert ev


def test_release_assertion_fail_keeps_state(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, incident_capa_verified=False)  # CAPA not verified -> item 6 fails
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is False
    # batch stays pending_release, release_decision unchanged
    batch = session.get(DeliveryBatch, _BATCH)
    assert batch.status == "pending_release"
    assert batch.release_decision == "undecided"
    # the failing check names the incident
    failing = [c for c in body["checks"] if not c["passed"]]
    assert any("INC" in c["reason"] or _INC is not None for c in failing)
    reason_text = " ".join(c["reason"] for c in failing)
    assert "all_capa_verified" in reason_text or any(
        c["name"] == "all_capa_verified" for c in failing
    )


def test_release_scope_gap_blocks(api_client: Any) -> None:
    client, session, _ = api_client
    # reinspection covers only 1 of the 3 required SNs -> item 7 fails
    _seed(session, reinspection_scope=["SN-A"])
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is False
    failing = [c for c in body["checks"] if c["name"] == "reinspection_scope_covered"]
    assert failing and not failing[0]["passed"]
    # reason lists the missing SNs
    assert "SN-B" in failing[0]["reason"] and "SN-C" in failing[0]["reason"]
    assert session.get(DeliveryBatch, _BATCH).status == "pending_release"


def test_release_retry_after_fix_releases(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session, incident_capa_verified=False)
    r1 = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert r1.json()["passed"] is False
    assert session.get(DeliveryBatch, _BATCH).status == "pending_release"
    # engineer verifies the CAPA, then retries
    inc = session.get(QualityIncident, _INC)
    inc.capa_verified = True
    session.commit()
    r2 = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert r2.status_code == 200
    assert r2.json()["passed"] is True
    assert session.get(DeliveryBatch, _BATCH).status == "released"


def test_release_archived_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    session.get(DeliveryBatch, _BATCH).status = "archived"
    session.commit()
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 409


def test_release_wrong_state_409(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    session.get(DeliveryBatch, _BATCH).status = "isolated"
    session.commit()
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 409


def test_release_evidence_hash_is_stable_real_sha256(api_client: Any) -> None:
    """C8 regression: the release evidence row's content_hash must be a real
    sha256 of the evidence content (canonical JSON, sort_keys=True) - stable
    across recomputation, not a random uuid fragment."""
    import hashlib
    import json

    client, session, _ = api_client
    _seed(session)
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 200 and resp.json()["passed"] is True
    ev = session.query(Evidence).filter(Evidence.evidence_type == "human_confirmation").one()
    assert ev.content is not None
    digest = hashlib.sha256(
        json.dumps(ev.content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert ev.content_hash == f"sha256:{digest}"
    assert len(digest) == 64
    # Stable: recomputing from the same content yields the identical digest.
    digest2 = hashlib.sha256(
        json.dumps(ev.content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert digest == digest2


# ---------------------------------------------------------------------------
# B1 回归：放行口径不得"看着在检查、实际不生效"
# ---------------------------------------------------------------------------


def test_item2_no_sample_conclusion_blocks(api_client: Any) -> None:
    """断言②：批次没有抽样结论时不得默认通过。

    ``_latest_sample`` 原本在查不到结论时返回 ``SampleView(result="pass")``
    —— "没抽样"被当成"抽样合格"，等于放行闸门缺一环。
    """
    client, session, _ = api_client
    _seed(session, with_conclusion=False)
    resp = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["sample_passed"]["passed"] is False
    assert by_name["sample_passed"]["reason"]
    assert body["passed"] is False
    assert session.get(DeliveryBatch, _BATCH).status == "pending_release"


def test_item2_conclusion_of_another_inspection_type_does_not_count(api_client: Any) -> None:
    """断言②：只认批次规则抽样类型的结论。

    别类型（visual）的合格结论、且 sample_round 更大，不能被当成"最新结论"
    —— 这正是 ``SampleRepository.get_latest_conclusion`` 强制要求
    ``inspection_type`` 的原因，放行端点不得绕开它。
    """
    client, session, _ = api_client
    _seed(session, with_conclusion=False)
    session.add(
        SampleConclusion(
            conclusion_id=UUID("eeeeeeee-1111-2222-3333-44444444440a"),
            batch_id=_BATCH,
            inspection_type="visual",
            rule_version_id=_RULE,
            sample_round=9,
            n=3,
            ac=0,
            re=1,
            result="pass",
            evidence_id=_EV1,
        )
    )
    session.commit()
    body = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"}).json()
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["sample_passed"]["passed"] is False
    assert body["passed"] is False


def test_item5_evidence_without_anchor_blocks(api_client: Any) -> None:
    """断言⑤：没有锚点（object_ref 为空）的证据行不算"证据齐备"。

    原来的 ``all(e.evidence_id for e in evidence)`` 对主键取真值判断，恒为
    真；"证据齐备"实际退化成"列表非空"。证据必须能指回它证明的对象。
    """
    client, session, _ = api_client
    _seed(session, evidence_object_ref={})
    body = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"}).json()
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["all_evidence_present"]["passed"] is False
    assert body["passed"] is False


def test_item7_uses_rule_sample_plan_and_passes(api_client: Any) -> None:
    """断言⑦：ISO 查询参数取自批次规则的 sample_plan，生产形状下必须能解析。

    种子库的 ISO 表只有 ``normal`` 级别（001_init.sql），而实现原来硬编码
    ``inspection_level="full"`` —— 生产里查不到任何行。这里用生产形状
    （iv / normal / AQL 1.0）就能过。
    """
    client, session, _ = api_client
    _seed(session)  # 默认即生产形状
    body = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"}).json()
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["reinspection_scope_covered"]["passed"] is True, by_name[
        "reinspection_scope_covered"
    ]["reason"]
    assert body["passed"] is True


def test_item7_does_not_resolve_against_a_different_iso_level(api_client: Any) -> None:
    """断言⑦：表里"碰巧有别的级别/AQL 的行"不算解析成功。

    只给 (iv, full, AQL 0.65) 这类与规则方案不符的行时，ISO 必须解析失败
    —— 这正是旧实现配的那条假行；留下它，检查就变成了自证。
    """
    client, session, _ = api_client
    _seed(session, iso_rows=[("iv", "full", 0.65, 1, 100, 3, 0, 1)])
    body = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"}).json()
    by_name = {c["name"]: c for c in body["checks"]}
    item7 = by_name["reinspection_scope_covered"]
    assert item7["passed"] is False
    assert "ISO" in item7["reason"]


def test_item7_fails_closed_when_rule_sample_plan_is_incomplete(api_client: Any) -> None:
    """断言⑦：规则没配抽样方案时不得静默放行（D9：缺规则即异常）。

    原来实现用 ``iso_rows[0].aql`` + 硬编码 "full" 兜底，恰好掩盖了规则
    缺失。现在必须指名"规则方案缺项"。
    """
    client, session, _ = api_client
    _seed(session, sample_plan={})
    body = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"}).json()
    by_name = {c["name"]: c for c in body["checks"]}
    item7 = by_name["reinspection_scope_covered"]
    assert item7["passed"] is False
    assert "sample_plan" in item7["reason"] or "抽样" in item7["reason"]
    assert body["passed"] is False


def test_item7_lot_size_is_the_batch_lot_not_the_reinspected_count(api_client: Any) -> None:
    """断言⑦：查 ISO 的 lot_size 是批次批量，不是"本次复检覆盖了几只"。

    原实现 ``lot_size=len(actual_sns) or 1``：批次计划 50 只、复检只覆盖 3 只
    时，lot 被当成 3，于是按错误的批量区间去匹配 ISO 行。
    """
    client, session, _ = api_client
    _seed(
        session,
        planned_quantity=50,
        iso_rows=[("iv", "normal", 1.0, 26, 50, 32, 1, 2)],
    )
    body = client.post(f"/api/batches/{_BATCH}/release", json={"approved_by": "ZHL-ENG"}).json()
    by_name = {c["name"]: c for c in body["checks"]}
    item7 = by_name["reinspection_scope_covered"]
    assert item7["passed"] is True, item7["reason"]
    assert body["passed"] is True
