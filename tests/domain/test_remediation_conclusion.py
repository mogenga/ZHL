"""第 2 轮（复检后）抽样结论的纯函数契约。

复检闭环前：抽样 fail → 批次隔离 → 异常处置 → 复检 → 关闭 → 待放行。
闭环后：第一轮样本里判 fail 的 SN 必须由**已关闭异常 + 覆盖它的通过复检**
解除，否则第 2 轮结论仍是 fail —— 「复检没做到位」绝不能被读成「自动通过」
（D9：无默认放行）。
"""

from __future__ import annotations

from app.domain.sampling import derive_remediation_conclusion
from app.domain.types import SnIncidentState, SnRemediationView


def _sn(
    judge_result: str,
    incidents: tuple[SnIncidentState, ...] = (),
    reinspection_covered: bool = False,
    sn: str = "SN-A",
) -> SnRemediationView:
    return SnRemediationView(
        sn=sn,
        judge_result=judge_result,
        incidents=incidents,
        reinspection_covered=reinspection_covered,
    )


def _closed(incident_id: str = "INC-1") -> SnIncidentState:
    return SnIncidentState(incident_id=incident_id, status="closed")


def test_round_two_passes_when_no_sampled_sn_failed() -> None:
    c = derive_remediation_conclusion([_sn("pass"), _sn("pass", sn="SN-B")])
    assert c.result == "pass"
    assert c.reason == ""


def test_round_two_passes_when_failed_sn_is_discharged_by_closed_incident() -> None:
    """复检通过 + 异常关闭 = 该 SN 的不合格已闭环，第 2 轮不再算它 fail。"""
    c = derive_remediation_conclusion(
        [
            _sn("pass"),
            _sn("fail", incidents=(_closed(),), reinspection_covered=True, sn="SN-B"),
        ]
    )
    assert c.result == "pass"
    assert c.reason == ""


def test_failed_sn_without_incident_is_not_discharged() -> None:
    """判 fail 却没有任何异常关联 —— 说明闭环流程根本没走，不得通过。"""
    c = derive_remediation_conclusion([_sn("fail")])
    assert c.result == "fail"
    assert "SN-A" in c.reason


def test_failed_sn_with_open_incident_is_not_discharged() -> None:
    c = derive_remediation_conclusion(
        [
            _sn(
                "fail",
                incidents=(_closed("INC-1"), SnIncidentState("INC-2", "pending_recheck")),
                reinspection_covered=True,
            )
        ]
    )
    assert c.result == "fail"
    assert "INC-2" in c.reason


def test_rejected_incident_does_not_discharge() -> None:
    """rejected = 复检失败路径（批次本就转拒绝）。它绝不能解除 SN。"""
    c = derive_remediation_conclusion(
        [_sn("fail", incidents=(SnIncidentState("INC-1", "rejected"),), reinspection_covered=True)]
    )
    assert c.result == "fail"
    assert "SN-A" in c.reason


def test_closed_incident_without_covering_reinspection_is_not_discharged() -> None:
    c = derive_remediation_conclusion(
        [_sn("fail", incidents=(_closed(),), reinspection_covered=False)]
    )
    assert c.result == "fail"
    assert "SN-A" in c.reason


def test_reason_names_every_undischarged_sn() -> None:
    c = derive_remediation_conclusion(
        [
            _sn("fail", sn="SN-A"),
            _sn("pass", sn="SN-B"),
            _sn("fail", incidents=(_closed(),), reinspection_covered=False, sn="SN-C"),
        ]
    )
    assert c.result == "fail"
    assert "SN-A" in c.reason
    assert "SN-C" in c.reason
    assert "SN-B" not in c.reason


def test_empty_sample_is_not_a_pass() -> None:
    """没有样本 ⇒ 没有可判通过的结论（与断言②同一条 D9 原则）。"""
    c = derive_remediation_conclusion([])
    assert c.result == "fail"
    assert c.reason
