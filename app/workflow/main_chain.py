"""main_chain_graph: six-node intake→judgment chain (PA spec §5.2–5.3, R1/R3).

Nodes (deterministic wrappers — no business judgment in the node body, R3):

    intake → extract → human_confirm [interrupt] → judge → sample → batch_transition

- ``intake``        writes ``module_inspections`` rows via ``InspectRepo`` +
                    a ``source_file`` evidence record.
- ``extract``       one ``extract_inspection_data`` (Task 5 Skill) call per
                    SN — batch-level confidence is the *graph node's* min
                    aggregation (Q3, spec §5.2); the Skill stays per-SN.
- ``human_confirm`` the single interrupt point (R3): ``interrupt()`` fires on
                    every run; the API layer owns the confirmation record —
                    auto-confirm (``confidence >= 0.8``, Review Focus #1
                    boundary) is decided by the intake route, which writes the
                    DB ``intake_status`` + ``human_confirmation`` evidence and
                    resumes immediately (spec §5.6, Q3); low confidence
                    defers to the confirm endpoint. The node only consumes the
                    resume payload ``{action: confirm|reject}`` and routes —
                    it never writes DB state (spec §2.3).
- ``judge``         ``judge_batch`` Skill (Task 6), single transaction.
- ``sample``        ``run_sampling`` Plugin (Task 6): hit → freeze +
                    conclusion; missing combo → ``rule_missing`` (D9, no
                    silent default release).
- ``batch_transition`` domain ``transition()`` (P0 frozen) advances the
                    batch state, writes ONE ``agent_task_traces`` row at
                    graph end (B2) + a ``task_trace`` evidence record.

Routing (spec §5.3):
    human_confirm → rejected        → END (incident mount point, R1)
    sample        → all pass        → batch_transition → pending_release
    sample        → any fail/miss   → batch_transition → isolated
                                      (incident_subgraph mount point, R1)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.domain.state_machine import transition
from app.domain.types import IsoRow, TransitionResult
from app.infra.models import AgentTaskTrace, DeliveryBatch, QualityRuleVersion
from app.plugins.auto_incident import ensure_incident
from app.plugins.sampling import run_sampling
from app.skills.extract import extract_inspection_data
from app.skills.judge import RuleNotEffective, judge_batch
from app.workflow.state import BatchWorkflowState

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.plugins.evidence_store import EvidenceStore
    from app.plugins.llm_client import LLMClient

#: Low-confidence threshold: ``confidence < 0.8`` → interrupt (Review Focus #1).
CONFIDENCE_THRESHOLD = 0.8

#: 允许开始进场的批次状态。入场是"pending_intake → pending_judge"这一步，
#: 只有停在该状态的批次才该被喂入数据；其余状态（已判定/已隔离/已放行/
#: 已归档……）再入场要么重复写行，要么让图替批次改状态。
INTAKE_ALLOWED_STATUS = "pending_intake"


class IntakeNotAllowed(Exception):
    """批次不在可入场状态时拒绝执行整条主链（节点层的守卫）。

    API 端点在这一层之前已经用 409 挡过一次；这里是第二道防线，覆盖直接
    使用图的调用方（脚本 / 测试），避免"绕过端点就绕过状态机"。
    """

    def __init__(self, status: str, batch_id: str) -> None:
        self.status = status
        self.batch_id = batch_id
        super().__init__(
            f"batch {batch_id} is in status {status!r}, expected {INTAKE_ALLOWED_STATUS!r}; "
            "refusing to run intake"
        )


def advance_batch(
    session: Session,
    batch_id: uuid.UUID,
    target: str,
    preconditions: dict,
) -> TransitionResult:
    """按状态机推进批次状态，**拒绝时不写库**，返回状态机的判定结果。

    这是 ``batch_transition`` 节点唯一被允许改批次状态的入口。此前的实现
    调用 ``transition()`` 后不检查 ``accepted``，直接 ``batch.status =
    target``：两次转移都被拒也照样落库，等于把状态机降级成"记录一下说法"。
    同时它写的一直是"请求的目标"而不是"实际被接受的边"—— ``target`` 被拒、
    回退到 ``isolated`` 成功时，落库的会是 ``pending_release``。
    """
    batch = session.get(DeliveryBatch, batch_id)
    if batch is None:
        return TransitionResult(accepted=False, reason=f"unknown batch: {batch_id}")

    res = transition(batch.status, target, preconditions, kind="batch")
    if res.accepted:
        batch.status = target
        session.flush()
        return res

    if target != "isolated":
        # 任何"不能照常放行"的判定都退回隔离（R1 挂载点），但同样必须真的
        # 被状态机接受才落库。
        fallback = transition(batch.status, "isolated", {"any_fail": True}, kind="batch")
        if fallback.accepted:
            batch.status = "isolated"
            session.flush()
            return fallback

    return res


class MainChainDeps(TypedDict, total=False):
    """Dependency container injected into ``build_main_chain_graph``.

    Required: ``session`` / ``inspect_repo`` / ``llm`` / ``evidence`` /
    ``iso_table`` / ``rule_version_id`` / ``inspection_type``. Optional
    overrides: ``aql`` / ``inspection_level`` / ``lot_size`` — when absent,
    the graph reads them from the batch's rule sample plan / planned
    quantity (P0-frozen rule data, AGENT.md §3.4).
    """

    session: Session
    inspect_repo: object
    llm: LLMClient
    evidence: EvidenceStore
    iso_table: list[IsoRow]
    rule_version_id: uuid.UUID
    inspection_type: str
    aql: float
    inspection_level: str
    lot_size: int


def _aql(deps: MainChainDeps) -> float:
    """AQL: deps override (tests / demos) else the rule sample plan (P0 data)."""
    override = deps.get("aql")
    if override is not None:
        return float(override)
    rule_row = deps["session"].get(QualityRuleVersion, deps["rule_version_id"])
    plan = (getattr(rule_row, "sample_plan", None) or {}) if rule_row else {}
    return float(plan.get("aql", 1.0))


def _lot_size(deps: MainChainDeps, state: BatchWorkflowState, sns: list[str]) -> int:
    """Lot size: deps override, else the batch's planned quantity, else len(sns)."""
    override = deps.get("lot_size")
    if override is not None:
        return int(override)
    row = deps["session"].get(DeliveryBatch, uuid.UUID(state["batch_id"]))
    if row is not None and row.planned_quantity:
        return int(row.planned_quantity)
    return len(sns)


def build_main_chain_graph(
    deps: MainChainDeps,
    checkpointer: object | None = None,
) -> object:
    """Build and compile the six-node main-chain graph.

    ``checkpointer`` is required for ``interrupt()`` to work (R2); pass
    ``InMemorySaver()`` in tests/demos or ``get_checkpointer("pg")`` in
    production. The compiled graph is returned ready for ``stream`` /
    ``invoke`` with ``config={'configurable': {'thread_id': batch_id}}``.
    """
    session = deps["session"]
    inspect_repo = deps["inspect_repo"]
    llm = deps["llm"]
    evidence = deps["evidence"]
    iso_table = deps["iso_table"]
    rule_version_id = deps["rule_version_id"]

    graph = StateGraph(BatchWorkflowState)

    # ------------------------------------------------------------------ nodes

    def intake_node(state: BatchWorkflowState) -> dict:
        """Write the intake rows (module_inspections) + source_file evidence.

        Deterministic: the inspection rows are the ones carried in state
        (upstream of the graph they were parsed by the API layer, Q1); the
        node persists them exactly once.

        Entry guard (B2): the batch must actually be in ``pending_intake``.
        The batch status itself is advanced one node later, in
        ``human_confirm`` — only a *confirmed* intake may leave
        ``pending_intake`` (a rejected intake must stay there so the operator
        can correct the data and re-submit).
        """
        batch_id = uuid.UUID(state["batch_id"])
        batch = session.get(DeliveryBatch, batch_id)
        if batch is None:
            raise IntakeNotAllowed("<missing>", state["batch_id"])
        if batch.status != INTAKE_ALLOWED_STATUS:
            raise IntakeNotAllowed(batch.status, state["batch_id"])

        rows = state.get("inspections") or []
        if not rows:
            return {"batch_status": state.get("batch_status") or INTAKE_ALLOWED_STATUS}

        inspect_repo.create_inspections(session, batch_id, rows)
        session.flush()
        ev = evidence.save(
            "source_file",
            {"kind": "batch", "id": state["batch_id"]},
            content=str(len(rows)).encode("utf-8"),
            meta={"row_count": len(rows)},
        )
        return {
            "inspections": rows,
            "batch_status": "pending_judge",
            "evidence_ids": [str(getattr(ev, "evidence_id", ev))],
        }

    def extract_node(state: BatchWorkflowState) -> dict:
        """One extract Skill call per SN; batch confidence = min (Q3).

        The Skill writes the ``extraction_result`` evidence itself (Task 5);
        the node only aggregates confidence and collects low-conf SNs.

        Raw-source entry (FR-1.1/1.2): the parse Skill already extracted and
        archived every row before the graph ran, so ``state["pre_extracted"]``
        carries per-SN results and this node reuses them — extracting again
        would double the model calls and let the two answers disagree.
        """
        rows = state.get("inspections") or []
        pre_extracted: dict = state.get("pre_extracted") or {}
        confidences: dict[str, float] = {}
        extraction: dict[str, dict] = {}
        evidence_ids = list(state.get("evidence_ids") or [])
        for row in rows:
            sn = row["sn"]
            if sn in pre_extracted:
                result = pre_extracted[sn]
            else:
                payload = {
                    k: row[k]
                    for k in (
                        "sn",
                        "pmax",
                        "temp",
                        "isc",
                        "voc",
                        "vm",
                        "im",
                        "ff",
                        "appearance_result",
                    )
                    if k in row
                }
                result = extract_inspection_data(
                    llm=llm,
                    source_type=str(row.get("source_type", "")),
                    payload=payload,
                    evidence=evidence,
                    object_ref={"kind": "batch", "id": state["batch_id"]},
                )
            extraction[sn] = result
            confidences[sn] = float(result["confidence"])
            evidence_ids.append(str(result.get("evidence_id", "")))

        low_conf_sns = [sn for sn, c in confidences.items() if c < CONFIDENCE_THRESHOLD]
        min_confidence = min(confidences.values()) if confidences else 0.0
        return {
            "extraction": extraction,
            "confidence": min_confidence,
            "low_conf_sns": low_conf_sns,
            "evidence_ids": [e for e in evidence_ids if e],
        }

    def human_confirm_node(state: BatchWorkflowState) -> dict:
        """The single interrupt point (R3) / Q1 / Q3.

        ``interrupt()`` fires on **every** run (low- or high-confidence):
        the confirmation record (DB ``intake_status`` + ``human_confirmation``
        evidence) is owned by the API layer — on auto-confirm the intake
        route writes it and resumes immediately (spec §5.6, Q3); on low
        confidence the caller confirms/rejects later via the confirm
        endpoint. This ordering guarantees the judge node's A4 guard
        (every SN confirmed) is satisfied on every path, because the judge
        only ever runs *after* a resume whose payload the API endpoint has
        already persisted. The resume payload is
        ``{"action": "confirm"|"reject", "fields"?: dict}``.

        DB write (B2): on ``confirm`` this node runs the ``pending_intake ->
        pending_judge`` state-machine edge and persists it. The batch's status
        lives in the DB, not in workflow state, and the chain is about to judge
        against it — ``pending_judge`` used to be recorded in workflow state
        only, so the batch never actually occupied it (and the DB kept saying
        ``pending_intake`` while the chain called itself ``pending_judge``,
        which is what let ``batch_transition`` write a state the state machine
        had refused). On ``reject`` nothing is written: the batch stays
        ``pending_intake`` so the data can be corrected and re-submitted —
        "rejected never leaves intake".
        """
        confidence = float(state.get("confidence", 0.0))
        payload = interrupt(
            {
                "action_required": "confirm_or_reject",
                "low_conf_sns": state.get("low_conf_sns") or [],
                "confidence": confidence,
                "auto_confirm": confidence >= CONFIDENCE_THRESHOLD,
            }
        )
        action = (payload or {}).get("action", "reject")
        if action != "confirm":
            return {"intake_status": "rejected"}

        entered = advance_batch(session, uuid.UUID(state["batch_id"]), "pending_judge", {})
        if not entered.accepted:
            # The confirmation record is written by the API layer *before*
            # this resume, so a refusal here means the batch moved out from
            # under the chain. Refuse rather than judge a batch whose state
            # we cannot vouch for.
            raise IntakeNotAllowed(
                f"state changed during confirmation: {entered.reason}", state["batch_id"]
            )
        return {"intake_status": "confirmed", "batch_status": "pending_judge"}

    def route_after_human_confirm(state: BatchWorkflowState) -> str:
        return "judge" if state.get("intake_status") == "confirmed" else "end"

    def judge_node(state: BatchWorkflowState) -> dict:
        """Run the judge Skill (Task 6) for the whole batch, one tx.

        The A4 guard (every SN confirmed) is enforced by the API layer: the
        confirm endpoint writes the DB-side ``intake_status`` (spec §5.6, Q3)
        *before* resuming, and the intake route records the auto-confirm
        after the graph run. The Skill keeps its guard as a second line —
        an unexpected unconfirmed state is a loud 400, never a silent pass.
        """
        try:
            result = judge_batch(session, evidence, uuid.UUID(state["batch_id"]), rule_version_id)
        except RuleNotEffective as exc:
            # R1: the chosen rule version was not yet in force when the
            # inspections happened. Not fatal to the graph (the confirmation
            # itself is still valid); record the batch verdict as
            # ``rule_missing`` so downstream sampling / release asserts
            # (D9: no silent default) can see the gap. The independent
            # ``/judge`` endpoint still maps the raw exception to a 400 so a
            # standalone re-judge fails loudly.
            result = {
                "result": "rule_missing",
                "reason": "rule not effective yet",
                "counts": {"Pass": 0, "Fail": 0},
                "fail_sns": [],
                "per_sn": {},
                "rule_version_id": str(rule_version_id),
                "rule_snapshot": None,
            }
            session.flush()
            return {
                "decision": result,
                "evidence_ids": [str(e) for e in (state.get("evidence_ids") or [])],
                "rule_not_effective": exc.earliest_captured_at.isoformat(),
            }
        session.flush()
        return {
            "decision": result,
            "evidence_ids": [
                *(str(e) for e in (state.get("evidence_ids") or [])),
                *([result["decision_evidence_id"]] if result.get("decision_evidence_id") else []),
            ],
        }

    def sample_node(state: BatchWorkflowState) -> dict:
        """Run the sampling Plugin (Task 6).

        - combo hit → freeze + conclusion (``result`` in pass/fail/rule_missing)
        - combo missing → ``rule_missing`` (D9: no silent default release)
        """
        sns = inspect_repo.get_batch_sns(session, uuid.UUID(state["batch_id"]))
        result = run_sampling(
            session,
            iso_table,
            uuid.UUID(state["batch_id"]),
            inspection_type=str(deps.get("inspection_type", "iv")),
            aql=_aql(deps),
            lot_size=_lot_size(deps, state, sns),
            sns=sns,
            seed=state["batch_id"],
            inspection_level=str(deps.get("inspection_level", "normal")),
        )
        session.flush()
        return {"sample": result}

    def batch_transition_node(state: BatchWorkflowState) -> dict:
        """Advance the batch state machine + write the trace (B2).

        - all pass + sample pass → ``pending_release``
        - any fail / rule_missing → ``isolated`` (R1 mount point)
        """
        decision = state.get("decision") or {}
        sample = state.get("sample") or {}
        all_pass = decision.get("result") == "Pass"
        # all_pass + sample pass → release; any fail / rule_missing → isolated
        # (a missing sampling rule is a rule anomaly, not a clean pass — D9)
        release_path = all_pass and sample.get("result") == "pass"

        # The batch's current status is read from the DB inside advance_batch —
        # not from the carried-over workflow state. Workflow state can hold a
        # stale value (``state["batch_status"]`` survives a re-run of the same
        # thread_id), and the DB is the authority for a batch's state.
        target = "pending_release" if release_path else "isolated"
        preconditions = {
            "all_pass": all_pass,
            "sample_pass": release_path,
            "any_fail": (not all_pass) or sample.get("result") == "rule_missing",
        }
        batch = session.get(DeliveryBatch, uuid.UUID(state["batch_id"]))
        if batch is None:
            tr = TransitionResult(accepted=False, reason=f"unknown batch: {state['batch_id']}")
        else:
            tr = advance_batch(session, batch.batch_id, target, preconditions)
        # 状态机接受后批次实际落到的状态——可能是 target，也可能是被接受
        # 的 isolated 回退边；未接受时批次状态原样未动。
        applied = batch.status if batch is not None else ""
        applied_target = applied if tr.accepted else ""

        # §8.1 异常事件自动创建（P0 缺口 #2）：批次**确实进入隔离**时同事务
        # 创建异常。触发优先级：规则缺失 > 判定 Fail（affected=fail_sns，精确
        # 到组件）> 抽样 Fail（affected=全批 SN；判定 Fail 时抽样必然同源
        # 失败，只建一只 rule_fail，不重复建）。
        if applied_target == "isolated" and batch is not None:
            sample_result = sample.get("result")
            fail_sns = list(decision.get("fail_sns") or [])
            if decision.get("result") == "rule_missing" or sample_result == "rule_missing":
                trigger = "rule_missing"
                affected: list[str] = list(inspect_repo.get_batch_sns(session, batch.batch_id))
            elif fail_sns:
                trigger = "rule_fail"
                affected = fail_sns
            elif sample_result == "fail":
                trigger = "sample_fail"
                affected = list(inspect_repo.get_batch_sns(session, batch.batch_id))
            else:
                trigger = "rule_fail"
                affected = list(inspect_repo.get_batch_sns(session, batch.batch_id))
            ensure_incident(
                session,
                batch_id=batch.batch_id,
                trigger_type=trigger,
                severity="major",
                affected_sns=affected,
                evidence_ids=list(state.get("evidence_ids") or []),
            )
        trace = AgentTaskTrace(
            batch_id=uuid.UUID(state["batch_id"]),
            input_ref={"batch_id": state["batch_id"]},
            skill_name="judge",
            plugins_invoked=["sampling"],
            params_summary={
                "target": target,
                "applied_target": applied_target,
                "accepted": tr.accepted,
            },
            model_version="mock",
            structured_output={
                "result": decision.get("result"),
                "sample_result": sample.get("result"),
            },
            # A denied transition is not "partial data" — nothing was advanced.
            # Record it as a failure with the state machine's reason so the
            # audit trail names why the batch did not move.
            validation_result="pass" if tr.accepted else "fail",
            failure_reason=None if tr.accepted else f"batch transition denied: {tr.reason}",
            human_action={"action": state.get("intake_status")},
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        session.add(trace)
        session.flush()

        ev = evidence.save(
            "task_trace",
            {"kind": "batch", "id": state["batch_id"]},
            content=str(applied_target or target).encode("utf-8"),
            meta={"target": target, "applied_target": applied_target, "accepted": tr.accepted},
        )
        return {
            # What actually happened, not what was requested: an empty string
            # means the state machine refused and the batch did not move.
            "batch_status": applied_target,
            "transition_result": {
                "accepted": tr.accepted,
                "reason": getattr(tr, "reason", "") or "",
                "target": target,
                "applied_target": applied_target,
            },
            "evidence_ids": [
                *list(state.get("evidence_ids") or []),
                str(getattr(ev, "evidence_id", ev)),
            ],
        }

    # ------------------------------------------------------------- graph build

    graph.add_node("intake", intake_node)
    graph.add_node("extract", extract_node)
    graph.add_node("human_confirm", human_confirm_node)
    graph.add_node("judge", judge_node)
    graph.add_node("sample", sample_node)
    graph.add_node("batch_transition", batch_transition_node)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "extract")
    graph.add_edge("extract", "human_confirm")
    graph.add_conditional_edges(
        "human_confirm",
        route_after_human_confirm,
        {"judge": "judge", "end": END},
    )
    graph.add_edge("judge", "sample")
    graph.add_edge("sample", "batch_transition")
    graph.add_edge("batch_transition", END)

    return graph.compile(checkpointer=checkpointer)


__all__ = ["MainChainDeps", "build_main_chain_graph", "CONFIDENCE_THRESHOLD"]
