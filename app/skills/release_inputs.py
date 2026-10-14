"""Release-assertion input assembly (shared by the release API and reports).

The seven ``assert_release`` inputs must be assembled from the DB in exactly
one place: the release endpoint computes them to decide a release, and the
release report must QUOTE that same computation (设计文档 §9.9 —— 报告是数据
引用，不是第二套口径). A single module also keeps ``app/domain`` read-free
(P0 purity): this is the only layer that resolves ISO values, required-SN
sets and evidence sets from the ORM.

Everything here is DB → domain-view marshalling; no business judgement.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.domain.release import assert_release
from app.domain.state_machine import TERMINAL_INCIDENT_STATUSES
from app.domain.types import (
    AssertionResult,
    BatchView,
    CapaView,
    EvidenceView,
    IncidentView,
    IsoRow,
    ReinspectionScopeIn,
    SampleView,
)
from app.infra.models import (
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

#: 一个能驱动 ISO 查表的抽样方案至少要有这三个键（规则 sample_plan 的约定，
#: 见 001_init.sql 种子与主链 ``main_chain._aql``）。
_REQUIRED_PLAN_KEYS = ("inspection_type", "inspection_level", "aql")

#: The fixed seven assertion checks a release report carries (PB Task 2/8).
#: 单一事实来源：放行端点、报告层、测试都用这份名单。
RELEASE_ASSERTION_NAMES = (
    "state",
    "sample_passed",
    "no_open_critical_incident",
    "no_isolated_sn",
    "all_evidence_present",
    "all_capa_verified",
    "reinspection_scope_covered",
)


def _rule_sample_plan(session: Any, batch: DeliveryBatch) -> tuple[dict, str]:
    """批次所选规则版本的抽样方案 → ``(plan, error)``。

    ``error`` 非空表示方案缺项、无法解析 ISO 组合。调用方必须据此判定失败
    —— 旧实现用硬编码 ``"full"`` + ``iso_rows[0].aql`` 兜底，把"规则缺失"
    掩盖成了"查表命中/未命中"，检查因此失去意义（D9：规则缺失即异常）。
    """
    rule = session.get(QualityRuleVersion, batch.rule_version_id) if batch.rule_version_id else None
    plan = dict(getattr(rule, "sample_plan", None) or {})
    missing = [k for k in _REQUIRED_PLAN_KEYS if plan.get(k) in (None, "")]
    if missing:
        return plan, (
            f"rule sample_plan missing {missing} in rule_version "
            f"{getattr(rule, 'rule_version_id', None)} — cannot resolve the ISO sampling plan"
        )
    return plan, ""


def _latest_sample(session: Any, batch_id: uuid.UUID, inspection_type: str) -> SampleView:
    """该批次**指定检验类型**的最新抽样结论。

    ``inspection_type`` 必传：跨类型取"最新"正是
    ``SampleRepository.get_latest_conclusion`` 明确禁止的行为（会拿到别类型
    的合格结论）。查不到结论时返回空 ``result`` —— 由断言②判失败，不默认通过。
    """
    if not inspection_type:
        return SampleView(result="", inspection_type="")
    row = (
        session.query(SampleConclusion)
        .filter(
            SampleConclusion.batch_id == batch_id,
            SampleConclusion.inspection_type == inspection_type,
        )
        .order_by(SampleConclusion.sample_round.desc())
        .first()
    )
    if row is None:
        return SampleView(result="", inspection_type=inspection_type)
    return SampleView(
        result=row.result,
        inspection_type=row.inspection_type,
        sample_round=row.sample_round,
    )


def _incident_views(session: Any, batch_id: uuid.UUID) -> list[IncidentView]:
    rows = session.query(QualityIncident).filter(QualityIncident.batch_id == batch_id).all()
    return [
        IncidentView(
            incident_id=str(r.incident_id),
            batch_id=str(r.batch_id),
            trigger_type=r.trigger_type,
            severity=r.severity,
            status=r.status,
            owner=r.owner or "",
            confirmed_root_cause=r.confirmed_root_cause or "",
            corrective_action=r.corrective_action or "",
        )
        for r in rows
    ]


def _isolated_sns(session: Any, batch_id: uuid.UUID) -> list[str]:
    rows = (
        session.query(ModuleInspection.sn)
        .filter(
            ModuleInspection.batch_id == batch_id,
            ModuleInspection.intake_status == "isolated",
        )
        .all()
    )
    return [r[0] for r in rows]


def _required_evidence(
    session: Any, batch_id: uuid.UUID, incident_ids: list[str], batch: DeliveryBatch
) -> list[EvidenceView]:
    """The release gate's evidence set: every piece of evidence the batch's
    incidents + sample conclusions reference."""
    evidence_ids: set[str] = set()
    for iid in incident_ids:
        inc = session.get(QualityIncident, uuid.UUID(iid))
        if inc:
            evidence_ids.update(str(e) for e in (inc.evidence_ids or []))
    concls = session.query(SampleConclusion).filter(SampleConclusion.batch_id == batch_id).all()
    for c in concls:
        if c.evidence_id:
            evidence_ids.add(str(c.evidence_id))
    # Decision snapshots anchor the release gate: one is written per judging
    # run of THIS batch (graph judge/sampling node; object_ref.id == the
    # batch uuid). Without them the all-pass path (no open incident, no
    # sample-conclusion evidence link) would leave the required set empty and
    # all_evidence_present would block the release forever.
    # Cross-DB scoping: query decision snapshots by type, then filter to
    # this batch in Python. object_ref may be a JSON string (SQLite) or a
    # dict (jsonb-dialect ORM load); normalize before matching.
    for ev in session.query(Evidence).filter(Evidence.evidence_type == "decision_snapshot").all():
        ref = ev.object_ref
        if isinstance(ref, (str, bytes)):
            try:
                ref = json.loads(ref)
            except Exception:
                ref = {}
        ref_id = str((ref or {}).get("id") or "")
        if ref_id in (str(batch.batch_id), batch.batch_no or ""):
            evidence_ids.add(str(ev.evidence_id))
    # Resolve to EvidenceView rows (only those that actually exist).
    views: list[EvidenceView] = []
    for eid in sorted(evidence_ids):
        try:
            ev = session.get(Evidence, uuid.UUID(eid))
        except (ValueError, TypeError):
            ev = None
        if ev is not None:
            views.append(
                EvidenceView(
                    evidence_id=str(ev.evidence_id),
                    evidence_type=ev.evidence_type,
                    object_ref=ev.object_ref or {},
                )
            )
    return views


def _capa_views(session: Any, incident_views: list[IncidentView]) -> list[CapaView]:
    out: list[CapaView] = []
    for iv in incident_views:
        if iv.status in TERMINAL_INCIDENT_STATUSES:
            continue  # closed incidents don't need a CAPA check
        out.append(
            CapaView(
                incident_id=iv.incident_id,
                containment_action=iv.confirmed_root_cause,
                confirmed_root_cause=iv.confirmed_root_cause,
                corrective_action=iv.corrective_action,
                verified=_incident_capa_verified(session, iv.incident_id),
            )
        )
    return out


def _incident_capa_verified(session: Any, incident_id: str) -> bool:
    inc = session.get(QualityIncident, uuid.UUID(incident_id))
    return bool(inc.capa_verified) if inc else False


def _incident_required_sns(session: Any, incident_id: str) -> tuple[str, ...]:
    """The incident's full affected-SN set (the minimal reinspection scope)."""
    links = (
        session.query(IncidentSnLink.inspection_id)
        .filter(IncidentSnLink.incident_id == uuid.UUID(incident_id))
        .all()
    )
    inspection_ids = [r[0] for r in links]
    if not inspection_ids:
        return ()
    rows = (
        session.query(ModuleInspection.sn)
        .filter(ModuleInspection.inspection_id.in_(inspection_ids))
        .all()
    )
    return tuple(r[0] for r in rows)


def _reinspection_scope_rows(
    session: Any,
    batch: DeliveryBatch,
    incident_views: list[IncidentView],
    plan: dict,
    plan_error: str,
) -> tuple[list[ReinspectionScopeIn], dict[str, tuple[str, ...]]]:
    """One ReinspectionScopeIn per open incident: the incident's *reinspection
    coverage* (scope_sns from the Reinspection row) plus the ISO values
    resolved here from the batch's rule sample plan.

    The required-SN set for item 7 is what the incident *should* cover
    (its full incident_sn_links set); the scope_sns field is what a
    reinspection *actually* covered. A gap between the two is exactly the
    Review Focus #2 signal item 7 is meant to catch — so the row must
    carry the actual coverage, not re-declare the required set.

    The ISO values come from the rule's sample plan and the batch's planned
    quantity — the same scheme the main chain samples under. They are NOT
    hardcoded, and NOT taken from whichever ISO row happens to come back
    first: with a hardcoded ``"full"`` level the lookup could never resolve
    against the shipped table (which only carries ``normal``), and a
    positionally-picked AQL made the outcome depend on row order.

    ``plan_error`` (non-empty when the plan is incomplete) is carried onto
    every row so item 7 reports the rule gap instead of inventing a combo.
    """
    open_ids = [
        iv.incident_id for iv in incident_views if iv.status not in TERMINAL_INCIDENT_STATUSES
    ]
    if not open_ids:
        return [], {}
    inspection_type = str(plan.get("inspection_type") or "")
    inspection_level = str(plan.get("inspection_level") or "")
    aql = float(plan.get("aql") or 0.0)
    # lot_size = 批次批量（ISO 表的 lot 区间针对整批），不是"本次复检覆盖了几只"。
    lot_size = int(batch.planned_quantity or 0)
    out: list[ReinspectionScopeIn] = []
    required_sns: dict[str, tuple[str, ...]] = {}
    for iid in open_ids:
        actual_sns = _incident_reinspection_sns(session, iid)
        required_sns[iid] = _incident_required_sns(session, iid)
        out.append(
            ReinspectionScopeIn(
                incident_id=iid,
                scope_sns=actual_sns,
                lot_size=lot_size,
                inspection_type=inspection_type,
                inspection_level=inspection_level,
                aql=aql,
                plan_error=plan_error,
            )
        )
    return out, required_sns


def _incident_reinspection_sns(session: Any, incident_id: str) -> tuple[str, ...]:
    """The SNs a reinspection actually covered for this incident.

    Read from the Reinspection row's ``scope_sns`` column (the D5 explicit
    scope). When no reinspection row exists, the coverage is empty — which
    makes item 7's "no scope row / missing SNs" branch fire (a required
    set with zero coverage is a gap).
    """
    rows = (
        session.query(Reinspection.scope_sns)
        .filter(Reinspection.incident_id == uuid.UUID(incident_id))
        .all()
    )
    covered: set[str] = set()
    for row in rows:
        covered.update(row[0] or [])
    return tuple(sorted(covered))


def _iso_table(session: Any) -> list[IsoRow]:
    """The preset ISO table, in a deterministic order.

    ``order_by`` is not decoration: without it the row order is whatever the
    engine happens to return, and any consumer that reads the table
    positionally (the old code took ``iso_rows[0].aql``) silently becomes
    non-reproducible across engines / plans.
    """
    rows = (
        session.query(IsoSamplingRow)
        .order_by(
            IsoSamplingRow.inspection_type,
            IsoSamplingRow.inspection_level,
            IsoSamplingRow.aql,
            IsoSamplingRow.lot_size_min,
        )
        .all()
    )
    return [
        IsoRow(
            inspection_type=r.inspection_type,
            inspection_level=r.inspection_level,
            aql=float(r.aql),
            lot_size_min=r.lot_size_min,
            lot_size_max=r.lot_size_max,
            n=r.n,
            ac=r.ac,
            re=r.re,
        )
        for r in rows
    ]


# --- shared entry points ----------------------------------------------------


def assert_batch_release(session: Any, batch: DeliveryBatch) -> AssertionResult:
    """Assemble all seven assertion inputs for ``batch`` and run the domain
    ``assert_release``. The single computation both the release endpoint and
    the report layer consume."""
    # 抽样/复检的 ISO 组合统一来自该批次规则的 sample_plan（与主链抽样同源），
    # 不硬编码、不取"表里第一行"。
    plan, plan_error = _rule_sample_plan(session, batch)
    incident_views = _incident_views(session, batch.batch_id)
    open_ids = [
        iv.incident_id for iv in incident_views if iv.status not in TERMINAL_INCIDENT_STATUSES
    ]
    sample = _latest_sample(session, batch.batch_id, str(plan.get("inspection_type") or ""))
    iso_table = _iso_table(session)
    reinspection_scopes, required_sns = _reinspection_scope_rows(
        session, batch, incident_views, plan, plan_error
    )
    return assert_release(
        batch=BatchView(batch_id=str(batch.batch_no), status=batch.status),
        sample=sample,
        incidents=incident_views,
        isolated_sns=_isolated_sns(session, batch.batch_id),
        evidence=_required_evidence(session, batch.batch_id, open_ids, batch),
        capa=_capa_views(session, incident_views),
        reinspection_scopes=reinspection_scopes,
        iso_table=iso_table,
        required_sns=required_sns,
    )


def recorded_release_assertions(session: Any, batch: DeliveryBatch) -> dict | None:
    """放行时刻随证据行落库的七项断言结果（没有则 ``None``）。

    报告是**数据引用**不是重新裁决（设计文档 §9.9）：断言在批次处于
    ``pending_release`` 时算过一次并随放行证据落库（``app/api/release.py``
    写 ``Evidence.content["assertions"]`` + ``batch.release_evidence_id``）。
    报告引用那份已签署的结果，而不是在 ``released`` 状态上重算 —— 重算的
    ``check_state`` 会因状态不再是 ``pending_release`` 而误报失败，且任何
    重算结果都无法与放行时刻签署过的内容对应。
    """
    if not batch.release_evidence_id:
        return None
    ev = session.get(Evidence, batch.release_evidence_id)
    if ev is None:
        return None
    content = ev.content
    if isinstance(content, (str, bytes)):
        try:
            content = json.loads(content)
        except Exception:
            return None
    if not isinstance(content, dict):
        return None
    assertions = content.get("assertions")
    if not isinstance(assertions, dict) or not assertions.get("checks"):
        return None
    return assertions


#: ``recorded_release_assertions`` 返回 ``None`` 时的兜底断言集：七项全部
#: 未过、逐条指名原因。绝不渲染"全部通过"的占位 —— 一个 released 批次若
#: 连放行证据都没有，报告必须如实说"没有可引用的放行断言记录"。
def missing_release_assertions() -> dict:
    reason = (
        "no recorded release evidence (release_evidence_id missing) — assertion result unavailable"
    )
    return {
        "passed": False,
        "checks": [
            {"name": name, "passed": False, "reason": reason} for name in RELEASE_ASSERTION_NAMES
        ],
    }


def judgment_stats(session: Any, batch_id: uuid.UUID) -> dict:
    """判定统计（设计文档 §9.9：Pass/Fail 计数 + 抽样接收拒收数）。

    报告 ``decisions`` 键的真实数据源：``module_inspections.decision_status``
    聚合 + ``sample_conclusions`` 结论行。数值一律来自结构化事实，不由渲染
    层编造（功能需求 §9.2）。
    """
    rows = (
        session.query(ModuleInspection.decision_status)
        .filter(ModuleInspection.batch_id == batch_id)
        .all()
    )
    counts: dict[str, int] = {}
    for (status,) in rows:
        counts[status] = counts.get(status, 0) + 1
    concls = (
        session.query(SampleConclusion)
        .filter(SampleConclusion.batch_id == batch_id)
        .order_by(SampleConclusion.inspection_type, SampleConclusion.sample_round.desc())
        .all()
    )
    samples = [
        {
            "inspection_type": c.inspection_type,
            "sample_round": c.sample_round,
            "n": c.n,
            "ac": c.ac,
            "re": c.re,
            "result": c.result,
        }
        for c in concls
    ]
    return {
        "total_inspections": len(rows),
        "decision_counts": counts,
        "sample_conclusions": samples,
    }


__all__ = [
    "assert_batch_release",
    "judgment_stats",
    "missing_release_assertions",
    "recorded_release_assertions",
]
