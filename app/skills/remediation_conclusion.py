"""第 2 轮（复检后）抽样结论的装配与落库。

**为什么需要这一层**：抽样 fail → 批次隔离 → 异常处置 → 复检 → 关闭 → 待放行
这条闭环里，复检只写 ``reinspections`` 行、关闭只改异常/批次状态，
**没有任何一处回写"结论"侧事实**。放行断言②读的是"最新一轮抽样结论"
（``app/skills/release_inputs.py::_latest_sample`` 按 ``sample_round`` 取最大），
而系统里永远只有第 1 轮那条 fail —— 于是批次一旦抽样 fail 就永久放不出行。

本模块把那句缺失的回写补上：闭环成立（被判 fail 的 SN 全部由「已关闭异常 +
覆盖它的通过复检」解除）时，写一条 ``sample_round=2`` 的结论。放行断言②的
取数逻辑不变，安全边界不变 —— 只是"最新一轮结论"终于有第二份真实事实。

**只写 pass**：复检没做到位时不写第 2 轮行（``skipped="not_discharged"``），
第 1 轮的 fail 仍是"最新一轮"，断言②继续阻断。结论表里每一条行都对应一次
真实闭环事实，不落"复算了一遍还是不行"的噪声行；复算过程由调用方记轨迹。

DB→domain 的装配边界与本层职责同 ``release_inputs``：``app/domain`` 保持无 DB。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.domain.sampling import derive_remediation_conclusion
from app.domain.types import SnIncidentState, SnRemediationView
from app.infra.models import (
    IncidentSnLink,
    ModuleInspection,
    QualityIncident,
    Reinspection,
    Sample,
    SampleConclusion,
)
from app.infra.repositories import SampleRepository

#: 第 2 轮固定轮次（第 1 轮由抽样 Plugin 写；复检闭环是唯一的第二轮来源）。
REMEDIATION_ROUND = 2


@dataclass(frozen=True)
class RemediationOutcome:
    """一次复检闭环结论推导的结果。

    ``written`` 为 True 表示本次新写了第 2 轮结论行；``skipped`` 非空表示
    不需要/无法推导（``no_sample_conclusion`` / ``already_passed`` /
    ``not_discharged``）。
    """

    result: str
    reason: str = ""
    written: bool = False
    skipped: str = ""
    sample_round: int = 0


def _latest_conclusion(session: Any, batch_id: uuid.UUID) -> SampleConclusion | None:
    """批次最新一轮结论（跨检验类型取最大轮次，再锁定该行的类型）。"""
    return session.scalars(
        select(SampleConclusion)
        .where(SampleConclusion.batch_id == batch_id)
        .order_by(SampleConclusion.sample_round.desc())
    ).first()


def _sampled_sns(session: Any, batch_id: uuid.UUID, inspection_type: str) -> list[str]:
    """第 1 轮抽样实际选中的 SN 集合。

    样本行缺失（历史数据/手工造数）时退化为批次全量 SN：那是抽样集合的
    **超集**，只会让判定更严，不会把未闭环的 SN 漏过去。
    """
    rows = (
        session.query(Sample.sn)
        .filter(
            Sample.batch_id == batch_id,
            Sample.inspection_type == inspection_type,
        )
        .all()
    )
    sns = [r[0] for r in rows]
    if sns:
        return sns
    return [
        r[0]
        for r in session.query(ModuleInspection.sn)
        .filter(ModuleInspection.batch_id == batch_id)
        .all()
    ]


def _judge_verdicts(session: Any, batch_id: uuid.UUID) -> dict[str, str]:
    """SN → 判定结论。同一 SN 有多行时取最差的一条（fail 优先），不致漏判。"""
    verdicts: dict[str, str] = {}
    for sn, status in (
        session.query(ModuleInspection.sn, ModuleInspection.decision_status)
        .filter(ModuleInspection.batch_id == batch_id)
        .all()
    ):
        if sn not in verdicts or status == "fail":
            verdicts[sn] = status
    return verdicts


def _incidents_by_sn(session: Any, batch_id: uuid.UUID) -> dict[str, list[SnIncidentState]]:
    """SN → 其关联异常 (id, 状态) 列表（经 ``incident_sn_links``）。"""
    rows = (
        session.query(ModuleInspection.sn, QualityIncident)
        .join(IncidentSnLink, IncidentSnLink.inspection_id == ModuleInspection.inspection_id)
        .join(QualityIncident, QualityIncident.incident_id == IncidentSnLink.incident_id)
        .filter(ModuleInspection.batch_id == batch_id)
        .all()
    )
    out: dict[str, list[SnIncidentState]] = {}
    for sn, inc in rows:
        out.setdefault(sn, []).append(
            SnIncidentState(incident_id=str(inc.incident_id), status=inc.status)
        )
    return out


def _reinspection_covered(
    session: Any, incidents_by_sn: dict[str, list[SnIncidentState]]
) -> set[str]:
    """被"通过且覆盖"的复检记录解除的 SN 集合。

    覆盖口径与放行断言⑦一致：以复检行的 ``scope_sns``（声明并被校验过的
    实际覆盖范围）为准，且复检必须属于该 SN **自己的**异常 —— 别的异常的
    复检结论不能拿来解除本 SN。
    """
    incident_ids = {i.incident_id for states in incidents_by_sn.values() for i in states}
    if not incident_ids:
        return set()
    parsed = {}
    for raw in incident_ids:
        try:
            parsed[str(raw)] = uuid.UUID(raw)
        except (ValueError, TypeError):
            continue
    rows = (
        session.query(Reinspection)
        .filter(
            Reinspection.incident_id.in_(list(parsed.values())),
            Reinspection.result == "pass",
        )
        .all()
    )
    owning_incident = {}
    for sn, states in incidents_by_sn.items():
        for st in states:
            owning_incident.setdefault(st.incident_id, set()).add(sn)
    covered: set[str] = set()
    for row in rows:
        inc_id = str(row.incident_id)
        for sn in row.scope_sns or []:
            if sn in owning_incident.get(inc_id, set()):
                covered.add(sn)
    return covered


def materialize_remediation_conclusion(session: Any, batch: Any) -> RemediationOutcome:
    """闭环成立则写第 2 轮结论；否则只报告推导结果，不落库。

    幂等：最新一轮结论已是 pass 时直接返回（``skipped="already_passed"``）。
    "第 1 轮已通过"与"第 2 轮已补写"在这条判定下是同一种情况 —— 后者写入的
    必定是 pass（本函数只写 pass），于是"最新一轮 = pass"就是"再没什么可补"。
    **若将来改成也写 fail 的第 2 轮行，这个幂等前提即失效**，必须同时补一个
    按轮次的既存检查，否则重复调用会撞 ``uq_conclusion_unique``。
    调用方（关闭端点 / 重算端点）负责事务提交与轨迹留痕。
    """
    batch_id = batch.batch_id
    round_one = _latest_conclusion(session, batch_id)
    if round_one is None:
        return RemediationOutcome(
            result="",
            reason="no sampling conclusion on this batch — nothing to remediate",
            skipped="no_sample_conclusion",
        )
    if round_one.result == "pass":
        # 第 1 轮本就通过（该批次是组件 fail 而非抽样 fail 进的隔离），
        # 或第 2 轮已经补过 —— 两者都无事可做。
        return RemediationOutcome(
            result="pass", skipped="already_passed", sample_round=round_one.sample_round
        )

    inspection_type = round_one.inspection_type
    verdicts = _judge_verdicts(session, batch_id)
    incidents_by_sn = _incidents_by_sn(session, batch_id)
    covered = _reinspection_covered(session, incidents_by_sn)

    sample = [
        SnRemediationView(
            sn=sn,
            judge_result=verdicts.get(sn, ""),
            incidents=tuple(incidents_by_sn.get(sn, ())),
            reinspection_covered=sn in covered,
        )
        for sn in _sampled_sns(session, batch_id, inspection_type)
    ]
    derived = derive_remediation_conclusion(sample)
    if derived.result != "pass":
        return RemediationOutcome(
            result=derived.result, reason=derived.reason, skipped="not_discharged"
        )

    # FR-4.2：复检重跑同一规则版本的同一抽样方案 —— n/ac/re 与检验类型一律
    # 沿用第 1 轮结论，不重新查表、不换版本。
    SampleRepository().save_conclusion(
        session,
        batch_id,
        inspection_type,
        round_one.rule_version_id,
        sample_round=REMEDIATION_ROUND,
        n=round_one.n,
        ac=round_one.ac,
        re=round_one.re,
        result="pass",
    )
    return RemediationOutcome(result="pass", written=True, sample_round=REMEDIATION_ROUND)


__all__ = ["REMEDIATION_ROUND", "RemediationOutcome", "materialize_remediation_conclusion"]
