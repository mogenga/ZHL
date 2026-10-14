"""§8.1 异常事件自动创建（P0 缺口 #2，设计文档 §9.8）。

判定 Fail / 抽样 Fail / 规则缺失 / 跨批次 SN 冲突等触发条件满足时，由
调用方（主链 ``batch_transition`` 节点、intake 冲突路径）在同事务内调用
``ensure_incident`` 自动创建 ``QualityIncident``。

设计约束：
- **幂等**：同批次同触发原因存在未关闭异常（new/isolated/assigned/
  pending_recheck）时返回既有异常，不重复创建（Review Focus #3）。
- **不产生孤儿异常**：affected_sns 逐个映射到 ``module_inspections``；
  一个都映射不上时不创建异常（返回 None），由调用方的轨迹行留下
  validation_result=fail 的痕迹。
- **不重复校验证据存在性**：调用方传入的 evidence_ids 来自
  ``evidence.save`` 的返回值，构造上真实（人工创建端点的 400 校验
  不适用于自动路径）。
- 严重度默认 ``major``（规则缺失/判定失败未经业务确认的分级，不做
  自动升 critical；跨批次冲突由调用方显式传 critical）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.domain.state_machine import OPEN_INCIDENT_STATUSES
from app.infra.models import DeliveryBatch, IncidentSnLink, ModuleInspection, QualityIncident
from app.plugins.trace_store import TraceStore


def mark_sns_isolated(session: Session, batch_id: str | uuid.UUID, sns: list[str]) -> int:
    """§8.2 组件级隔离：将批内 confirmed 的 SN 标记为 isolated（§9.8 #3）。

    仅当批次处于 ``isolated`` 态时标记（跨批次冲突路径的占用批次不是
    隔离态，不标记）；返回标记的组件数。使放行断言④「无隔离组件」
    真实生效——此前该断言恒真（审查 I-1 关联缺口）。
    """
    batch_uuid = uuid.UUID(str(batch_id))
    batch = session.get(DeliveryBatch, batch_uuid)
    if batch is None or batch.status != "isolated" or not sns:
        return 0
    rows = (
        session.query(ModuleInspection)
        .filter(
            ModuleInspection.batch_id == batch_uuid,
            ModuleInspection.sn.in_(list(sns)),
            ModuleInspection.intake_status == "confirmed",
        )
        .all()
    )
    for row in rows:
        row.intake_status = "isolated"
    return len(rows)


def restore_sns_after_close(session: Session, incident: QualityIncident) -> int:
    """异常关闭后恢复被隔离的 SN（§8.2 闭环：复检通过解除隔离）。

    逐 link 检查：仅当该 SN 不再被任何**未关闭**异常关联、且当前值为
    ``isolated`` 时恢复为 ``confirmed``（多异常重叠 SN 只在全部关闭后
    解除）。返回恢复的组件数。
    """
    restored = 0
    links = (
        session.query(IncidentSnLink)
        .filter(IncidentSnLink.incident_id == incident.incident_id)
        .all()
    )
    for link in links:
        insp = session.get(ModuleInspection, link.inspection_id)
        if insp is None or insp.intake_status != "isolated":
            continue
        open_others = (
            session.query(IncidentSnLink)
            .join(QualityIncident, QualityIncident.incident_id == IncidentSnLink.incident_id)
            .filter(
                IncidentSnLink.inspection_id == link.inspection_id,
                IncidentSnLink.incident_id != incident.incident_id,
                QualityIncident.status.in_(OPEN_INCIDENT_STATUSES),
            )
            .count()
        )
        if open_others == 0:
            insp.intake_status = "confirmed"
            restored += 1
    return restored


def ensure_incident(
    session: Session,
    *,
    batch_id: str | uuid.UUID,
    trigger_type: str,
    severity: str,
    affected_sns: list[str] | tuple[str, ...],
    evidence_ids: list[str] | tuple[str, ...] = (),
) -> QualityIncident | None:
    """为批次自动创建一只异常事件（幂等），返回创建/复用的异常行。

    返回 ``None`` 表示未创建——affected_sns 全部无法映射到批内组件
    （自动路径不产生孤儿异常，轨迹行会记录 validation_result=fail）。
    """
    batch_uuid = uuid.UUID(str(batch_id))
    # 并发幂等（审查 I-3）：对批次行加锁串行化同批次的并发自动创建——
    # 后到事务在本行上等待，拿到锁后重查幂等，能看见前者已提交的异常。
    # 人工创建端点不做此约束（业务允许多只同触发异常），故不加 DDL 唯一索引。
    session.query(DeliveryBatch).filter(
        DeliveryBatch.batch_id == batch_uuid
    ).with_for_update().first()
    existing = (
        session.query(QualityIncident)
        .filter(
            QualityIncident.batch_id == batch_uuid,
            QualityIncident.trigger_type == trigger_type,
            QualityIncident.status.in_(OPEN_INCIDENT_STATUSES),
        )
        .first()
    )
    if existing is not None:
        return existing

    affected = list(affected_sns)
    sn_rows: list[Any] = []
    if affected:
        sn_rows = (
            session.query(ModuleInspection)
            .filter(
                ModuleInspection.batch_id == batch_uuid,
                ModuleInspection.sn.in_(affected),
            )
            .all()
        )
    if affected and not sn_rows:
        # 无法映射任何 SN：不创建（异常必须关联 SN，架构不变量）。
        TraceStore().record(
            session,
            batch_id=batch_uuid,
            input_ref={"batch_id": str(batch_uuid)},
            skill_name="incident_auto",
            plugins_invoked=[],
            params_summary={
                "trigger_type": trigger_type,
                "affected_sns": affected,
                "mapped_sns": [],
                "dropped_sns": affected,
            },
            structured_output={"created": False, "reason": "sn_not_found"},
            validation_result="fail",
            failure_reason="affected_sns 无法映射到批内组件，未创建异常",
            human_action={"action": "auto"},
            started_at=datetime.now(UTC),
        )
        return None

    incident = QualityIncident(
        incident_id=uuid.uuid4(),
        batch_id=batch_uuid,
        trigger_type=trigger_type,
        severity=severity,
        evidence_ids=[str(e) for e in evidence_ids],
        status="new",
    )
    session.add(incident)
    session.flush()  # incident_id 可用于关联行
    for row in sn_rows:
        session.add(
            IncidentSnLink(incident_id=incident.incident_id, inspection_id=row.inspection_id)
        )
    # §8.2 组件级隔离：隔离态批次的受影响 SN 标记 isolated（占用批次非
    # 隔离态，mark_sns_isolated 内部会跳过——不误标已放行批次的组件）。
    mark_sns_isolated(session, batch_uuid, [r.sn for r in sn_rows])
    TraceStore().record(
        session,
        batch_id=batch_uuid,
        input_ref={"batch_id": str(batch_uuid)},
        skill_name="incident_auto",
        plugins_invoked=[],
        params_summary={
            "trigger_type": trigger_type,
            "affected_sns": affected,
            "mapped_sns": [r.sn for r in sn_rows],
            "dropped_sns": [s for s in affected if s not in {r.sn for r in sn_rows}],
        },
        structured_output={"created": True, "incident_id": str(incident.incident_id)},
        validation_result="pass",
        human_action={"action": "auto"},
        started_at=datetime.now(UTC),
    )
    return incident
