"""E2E: incident close — 复检通过后的闭环流转（P0 缺口 #1，设计文档 §9.8）。

修复前的行为：复检通过后批次停留在 ``pending_recheck``，没有任何 API 能把
异常转 ``closed``、把批次转 ``pending_release``（既有样例三测试只能直接
改库绕过）。本文件钉住修复后的 API 语义：

- ``POST /api/incidents/{id}/close``：五条件校验（owner / CAPA 字段 /
  独立复检记录 / 复检通过 / CAPA 已验证）→ 异常 ``pending_recheck ->
  closed``；仅当该批次**全部**异常处于终态时，批次 ``pending_recheck ->
  pending_release``（同事务 + 轨迹）。
- ``POST /api/incidents/{id}/capa`` 扩展：异常处于 ``pending_recheck``
  时允许更新 CAPA 字段与 verified 标记（复检通过后补勾），无状态转移。
  L3 门禁沿用：verified=true 必须已有通过的复检记录。

测试脚手架说明（沿用样例四的先例）：多异常批次的第二只异常的状态预备
使用直接写库（样例四 L231/L250-266 同款），被测对象——close 端点与
批次联动——始终走 API。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from app.infra.models import DeliveryBatch, QualityIncident
from app.plugins.trace_store import TraceStore
from tests.e2e.test_sample3_incident_closure import (
    B1,
    _INS_OK,
    _INS_OODB,
    _client,
    _seed_batch,
)
from tests.fixtures.loader import load_acceptance

_EV = "ab000000-1111-2222-3333-444444444403"  # EV1 (decision_snapshot, seeded)


def _create_incident(client: TestClient, affected_sn: str) -> UUID:
    r = client.post(
        "/api/incidents",
        json={
            "batch_id": str(B1),
            "trigger_type": "rule_fail",
            "severity": "major",
            "affected_sns": [affected_sn],
            "evidence_ids": [_EV],
        },
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["incident_id"])


def _isolate(client: TestClient, inc_id: UUID) -> None:
    r = client.post(f"/api/incidents/{inc_id}/isolate")
    assert r.status_code == 200, r.text


def _assign(client: TestClient, inc_id: UUID, containment_action: str | None = "isolate affected SNs") -> None:
    r = client.post(
        f"/api/incidents/{inc_id}/assign",
        json={
            "owner": "ZHL-QA",
            "due_at": "2026-06-05T00:00:00+00:00",
            "containment_action": containment_action,
        },
    )
    assert r.status_code == 200, r.text


def _reinspect_pass(client: TestClient, inc_id: UUID, inspection_id: UUID) -> None:
    r = client.post(
        f"/api/incidents/{inc_id}/reinspections",
        json={
            "inspection_id": str(inspection_id),
            "pmax": 550.0,
            "temp": 25.0,
            "isc": 10.5,
            "voc": 48.0,
            "vm": 40.0,
            "im": 13.0,
            "ff": 0.80,
            "result": "pass",
            "executor": "ZHL-QA",
            "executed_at": "2026-06-02T00:00:00+00:00",
            "scope_type": "minimal",
        },
    )
    assert r.status_code == 201, r.text


def _capa(client: TestClient, inc_id: UUID, verified: bool) -> Any:
    return client.post(
        f"/api/incidents/{inc_id}/capa",
        json={
            "confirmed_root_cause": "cell mismatch -> Impp drift",
            "corrective_action": "replace lead cell",
            "verified": verified,
        },
    )


def _close(client: TestClient, inc_id: UUID) -> Any:
    return client.post(f"/api/incidents/{inc_id}/close")


def test_close_after_recheck_pass_releases_batch() -> None:
    """复检通过 + CAPA 补勾 verified 后 close：异常 closed、批次 pending_release。"""
    sample = load_acceptance("incident")
    client, session, engine = _client()
    try:
        _seed_batch(session, sample, batch_no=sample["batch"]["batch_id"])
        inc_id = _create_incident(client, sample["incidents"][0]["affected_sns"][0])
        _isolate(client, inc_id)
        _assign(client, inc_id)
        _reinspect_pass(client, inc_id, _INS_OODB)

        # CAPA 先以 verified=false 进入待复检（复检记录已存在的顺序）
        r = _capa(client, inc_id, verified=False)
        assert r.status_code == 200, r.text
        assert session.get(DeliveryBatch, B1).status == "pending_recheck"

        # 未验证就 close：409 且指名缺失项（Review Focus #1）
        r = _close(client, inc_id)
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "close_conditions_unmet"
        assert "capa_verified" in detail["missing"]
        assert session.get(DeliveryBatch, B1).status == "pending_recheck"

        # capa 扩展：pending_recheck 态补勾 verified（L3 门禁此时可通过）
        r = _capa(client, inc_id, verified=True)
        assert r.status_code == 200, r.text
        assert session.get(QualityIncident, inc_id).capa_verified is True
        assert session.get(QualityIncident, inc_id).status == "pending_recheck"

        # close：异常 closed + 批次 pending_release（同事务）
        r = _close(client, inc_id)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["incident_status"] == "closed"
        assert body["batch_status"] == "pending_release"
        assert session.get(QualityIncident, inc_id).status == "closed"
        assert session.get(DeliveryBatch, B1).status == "pending_release"

        # 轨迹留痕
        skill_names = {t.skill_name for t in TraceStore().list_for_batch(session, B1)}
        assert "incident_close" in skill_names

        # §9.8 #3：close 恢复被隔离 SN → 放行断言④首次真实生效
        from app.infra.models import ModuleInspection, SampleConclusion

        sn_row = (
            session.query(ModuleInspection)
            .filter(ModuleInspection.batch_id == B1, ModuleInspection.sn == "SN-I-OODB")
            .first()
        )
        assert sn_row is not None and sn_row.intake_status == "confirmed"
        session.add(
            SampleConclusion(
                conclusion_id=UUID("ab000000-1111-2222-3333-444444444404"),
                batch_id=B1,
                inspection_type="iv",
                rule_version_id=session.get(DeliveryBatch, B1).rule_version_id,
                sample_round=1,
                n=2,
                ac=0,
                re=1,
                result="pass",
                evidence_id=UUID(_EV),
            )
        )
        session.commit()
        r = client.post(f"/api/batches/{B1}/release", json={"approved_by": "ZHL-ENG"})
        assert r.status_code == 200, r.text
        assert r.json()["passed"] is True, r.json()
    finally:
        session.close()
        engine.dispose()


def test_close_names_each_unmet_condition() -> None:
    """五条件缺失项逐条指名（Review Focus #1）：capa_fields / 复检记录缺失。"""
    sample = load_acceptance("incident")
    client, session, engine = _client()
    try:
        _seed_batch(session, sample, batch_no=sample["batch"]["batch_id"])
        inc_id = _create_incident(client, sample["incidents"][0]["affected_sns"][0])
        _isolate(client, inc_id)
        _assign(client, inc_id)

        # 未录复检就转待复检：missing 同时含「无复检记录」「未验证」
        r = _capa(client, inc_id, verified=False)
        assert r.status_code == 200, r.text
        r = _close(client, inc_id)
        assert r.status_code == 409, r.text
        missing = r.json()["detail"]["missing"]
        assert "reinspection_record" in missing
        assert "capa_verified" in missing

        # CAPA 字段清空（空串）→ missing 含 capa_fields
        r = _capa(client, inc_id, verified=False)  # 已在 pending_recheck：更新分支
        # 用空串覆盖字段：直接构造请求（capa 端点允许空串通过 pydantic）
        r2 = client.post(
            f"/api/incidents/{inc_id}/capa",
            json={"confirmed_root_cause": "", "corrective_action": "", "verified": False},
        )
        assert r2.status_code == 200, r2.text
        r = _close(client, inc_id)
        assert r.status_code == 409, r.text
        assert "capa_fields" in r.json()["detail"]["missing"]
    finally:
        session.close()
        engine.dispose()


def test_close_requires_evidence_and_containment() -> None:
    """需求五条件②③：无原始判定证据 / 无临时措施 → close 409 指名缺失。"""
    sample = load_acceptance("incident")
    client, session, engine = _client()
    try:
        _seed_batch(session, sample, batch_no=sample["batch"]["batch_id"])
        # evidence_ids 为空（人工创建端点允许）→ 关闭时必须被指名
        r = client.post(
            "/api/incidents",
            json={
                "batch_id": str(B1),
                "trigger_type": "rule_fail",
                "severity": "major",
                "affected_sns": [sample["incidents"][0]["affected_sns"][0]],
                "evidence_ids": [],
            },
        )
        assert r.status_code == 201, r.text
        inc_id = UUID(r.json()["incident_id"])
        _isolate(client, inc_id)
        _assign(client, inc_id, containment_action=None)  # 临时措施缺省 None
        _reinspect_pass(client, inc_id, _INS_OODB)
        r = _capa(client, inc_id, verified=True)
        assert r.status_code == 200, r.text

        r = _close(client, inc_id)
        assert r.status_code == 409, r.text
        missing = r.json()["detail"]["missing"]
        assert "original_decision_evidence" in missing
        assert "containment_action" in missing
        assert session.get(DeliveryBatch, B1).status == "pending_recheck"
    finally:
        session.close()
        engine.dispose()


def test_reinspection_fail_then_close_is_rejected() -> None:
    """复检不通过 → 异常/批次双转拒绝；此后 close 返回 409（M-7 行锁钉住语义）。"""
    sample = load_acceptance("incident")
    client, session, engine = _client()
    try:
        _seed_batch(session, sample, batch_no=sample["batch"]["batch_id"])
        inc_id = _create_incident(client, sample["incidents"][0]["affected_sns"][0])
        _isolate(client, inc_id)
        _assign(client, inc_id)
        _capa(client, inc_id, verified=False)  # 无复检记录 → 不勾验证

        r = client.post(
            f"/api/incidents/{inc_id}/reinspections",
            json={
                "inspection_id": str(_INS_OODB),
                "pmax": 540.0,
                "temp": 25.0,
                "isc": 10.5,
                "voc": 48.0,
                "vm": 40.0,
                "im": 13.0,
                "ff": 0.80,
                "result": "fail",
                "executor": "ZHL-QA",
                "executed_at": "2026-06-02T00:00:00+00:00",
                "scope_type": "minimal",
            },
        )
        assert r.status_code == 201, r.text
        assert session.get(QualityIncident, inc_id).status == "rejected"
        assert session.get(DeliveryBatch, B1).status == "rejected"

        # M-7：close 对 rejected 异常必须 409（行锁保证读到最新态，非竞态旧值）
        r = _close(client, inc_id)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "incident_not_closeable"
    finally:
        session.close()
        engine.dispose()


def test_capa_blocked_after_batch_archived() -> None:
    """M-8：批次归档后（D8 只读），capa 待复检补勾分支必须 409。"""
    sample = load_acceptance("incident")
    client, session, engine = _client()
    try:
        _seed_batch(session, sample, batch_no=sample["batch"]["batch_id"])
        inc_id = _create_incident(client, sample["incidents"][0]["affected_sns"][0])
        _isolate(client, inc_id)
        _assign(client, inc_id)
        _capa(client, inc_id, verified=False)  # 批次 pending_recheck

        # 批次主动拒绝（任意非归档→rejected 边）→ 处置报告 → 自动归档
        r = client.post(
            f"/api/batches/{B1}/reject",
            json={"reason": "测试归档", "rejected_by": "ZHL-QA"},
        )
        assert r.status_code == 200, r.text
        assert session.get(DeliveryBatch, B1).status == "archived"

        # 归档后对仍处 pending_recheck 的异常补勾 → 409 归档终态（RED：当前 200）
        r = _capa(client, inc_id, verified=False)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "batch archived, terminal state"
    finally:
        session.close()
        engine.dispose()


def test_close_gates_on_all_incidents_terminal() -> None:
    """批次存在第二只未关闭异常时，close 单只不推进批次（Review Focus #2）。"""
    sample = load_acceptance("incident")
    client, session, engine = _client()
    try:
        _seed_batch(session, sample, batch_no=sample["batch"]["batch_id"])

        # 两只异常都在批次 isolated 时创建（create 端点要求 isolated）
        inc1 = _create_incident(client, sample["incidents"][0]["affected_sns"][0])
        inc2 = _create_incident(client, "SN-I-OK")
        _isolate(client, inc1)
        _isolate(client, inc2)
        _assign(client, inc1)  # 批次 isolated -> disposing
        _reinspect_pass(client, inc1, _INS_OODB)
        r = _capa(client, inc1, verified=True)
        assert r.status_code == 200, r.text
        assert session.get(DeliveryBatch, B1).status == "pending_recheck"

        # 第二只异常的状态预备（测试脚手架，沿用样例四直接写库先例）：
        # isolated -> assigned -> 待复检，并补齐五条件中的 CAPA 字段与复检。
        inc2_row = session.get(QualityIncident, inc2)
        inc2_row.status = "assigned"
        inc2_row.owner = "ZHL-QA"
        inc2_row.containment_action = "quarantine SN-I-OK"
        session.commit()
        _reinspect_pass(client, inc2, _INS_OK)
        inc2_row = session.get(QualityIncident, inc2)
        inc2_row.confirmed_root_cause = "cosmetic scratch"
        inc2_row.corrective_action = "rework frame"
        inc2_row.capa_verified = True
        inc2_row.status = "pending_recheck"
        session.commit()

        # 关闭第一只：批次仍有未关闭异常 → 批次保持待复检
        r = _close(client, inc1)
        assert r.status_code == 200, r.text
        assert r.json()["incident_status"] == "closed"
        assert r.json()["batch_status"] == "pending_recheck"
        assert session.get(QualityIncident, inc1).status == "closed"
        assert session.get(DeliveryBatch, B1).status == "pending_recheck"

        # 关闭第二只：全部终态 → 批次转待放行
        r = _close(client, inc2)
        assert r.status_code == 200, r.text
        assert r.json()["batch_status"] == "pending_release"
        assert session.get(DeliveryBatch, B1).status == "pending_release"
    finally:
        session.close()
        engine.dispose()
