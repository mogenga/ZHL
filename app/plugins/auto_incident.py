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

from app.infra.models import IncidentSnLink, ModuleInspection, QualityIncident
from app.plugins.trace_store import TraceStore

_OPEN_STATUSES = ("new", "isolated", "assigned", "pending_recheck")


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
    existing = (
        session.query(QualityIncident)
        .filter(
            QualityIncident.batch_id == batch_uuid,
            QualityIncident.trigger_type == trigger_type,
            QualityIncident.status.in_(_OPEN_STATUSES),
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
            params_summary={"trigger_type": trigger_type, "affected_sns": affected},
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
    TraceStore().record(
        session,
        batch_id=batch_uuid,
        input_ref={"batch_id": str(batch_uuid)},
        skill_name="incident_auto",
        plugins_invoked=[],
        params_summary={"trigger_type": trigger_type, "affected_sns": [r.sn for r in sn_rows]},
        structured_output={"created": True, "incident_id": str(incident.incident_id)},
        validation_result="pass",
        human_action={"action": "auto"},
        started_at=datetime.now(UTC),
    )
    return incident
