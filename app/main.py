"""App factory wiring batch / intake routes to the Task 7 main-chain graph.

``create_app`` returns a ready-to-serve FastAPI instance. Layering (spec
§3.2): FastAPI routes → graph orchestration → Skill / Plugin → domain pure
functions → SQLAlchemy. Routes never do business judgment themselves: they
marshal requests, run one request-scoped transaction (AGENT.md §4.3), and
translate skill/plugin exceptions into B3 responses (``{"detail": ...}``).

Dependency injection (spec §8): every external collaborator is injectable on
the factory so API tests run entirely on in-memory SQLite with a fixture-
driven MockLLMClient and a stub inspection repository; production defaults
wire the real repositories, the P0 fixture LLM client, and the full ISO
2859-1 table.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: F401 (reserved)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.state_machine import transition
from app.domain.types import IsoRow
from app.infra.models import (
    AgentTaskTrace,
    DeliveryBatch,
    ModuleInspection,
    QualityRuleVersion,
    SampleConclusion,
)
from app.infra.repositories import BatchRepository, InspectionRepository
from app.plugins.evidence_store import EvidenceStore, FileEvidenceStore
from app.plugins.llm_client import LLMClient, MockLLMClient
from app.skills.judge import UnconfirmedSnsError, judge_batch
from app.workflow.checkpointer import get_checkpointer
from app.workflow.main_chain import build_main_chain_graph

__all__ = ["create_app", "app"]

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_FIXTURES = _ROOT / "tests" / "fixtures"
_DEFAULT_EVIDENCE_ROOT = _ROOT / "data" / "evidence"
_DEFAULT_ISO_PATH = _DEFAULT_FIXTURES / "iso2859_1_aql.json"


# ---------------------------------------------------------------------------
# Request / response schemas (route-local; not P0-frozen)
# ---------------------------------------------------------------------------


class BatchCreate(BaseModel):
    """POST /api/batches body (plan Task 8 Interfaces)."""

    batch_no: str
    project_id: str
    model: str
    rule_version_id: str
    planned_quantity: int = Field(ge=1)


class IntakeRequestIn(BaseModel):
    """POST /api/intake body: JSON inline (A2).

    The ``inspections`` rows are the same dicts the Task 7 ``intake_node``
    persists (sn + six electrical params + appearance + source_type).
    ``fields`` carries optional human overrides applied on auto-confirm
    (§5.6 Q3, symmetric with the confirm endpoint).
    """

    batch_id: str
    source_type: str = "IV_CSV"
    inspections: list[dict] = Field(default_factory=list)
    fields: dict | None = None


class IntakeConfirmIn(BaseModel):
    """POST /api/intake/{task_id}/confirm body ({action, fields?}, §5.6 Q3)."""

    action: str
    fields: dict | None = None


# ---------------------------------------------------------------------------
# Per-request dependency container (1 request == 1 transaction, §4.3)
# ---------------------------------------------------------------------------


class AppDeps:
    """One request's environment: session + evidence + repos + llm + graph deps."""

    def __init__(
        self,
        session: Session,
        evidence: EvidenceStore,
        inspect_repo: object,
        llm: LLMClient,
        iso_table: list[IsoRow],
        checkpointer: object,
    ) -> None:
        self.session = session
        self.evidence = evidence
        self.inspect_repo = inspect_repo
        self.llm = llm
        self.iso_table = iso_table
        self.checkpointer = checkpointer


class _App:
    """Mutable holder shared by the route factories (DI container)."""

    def __init__(self) -> None:
        self.d: AppDeps | None = None

    def get(self) -> AppDeps:
        assert self.d is not None
        return self.d


def _default_iso_table() -> list[IsoRow]:
    rows = json.loads(_DEFAULT_ISO_PATH.read_text(encoding="utf-8"))["rows"]
    return [IsoRow(**row) for row in rows]


def create_app(
    checkpointer_mode: str = "memory",
    session_factory: sessionmaker | None = None,
    llm: LLMClient | None = None,
    evidence: EvidenceStore | None = None,
    iso_table: list[IsoRow] | None = None,
    inspect_repo: object | None = None,
) -> FastAPI:
    """Build the app; deps are injectable (plan Task 8 Step 1–2).

    ``session_factory=None`` defers the real repository factory to request
    time (so importing ``app.main`` never requires a live database URL —
    the module-level ``app`` below is import-safe; production rebinds a
    concrete factory). Checkpointer is built eagerly (memory mode is
    side-effect-free; pg mode requires ``ZHL_DATABASE_URL``).
    """
    app = FastAPI(title="ZHL Phase A")

    checkpointer = get_checkpointer(checkpointer_mode)
    llm_client = llm or MockLLMClient(_DEFAULT_FIXTURES)
    table = iso_table if iso_table is not None else _default_iso_table()
    default_evidence = evidence
    default_repo = inspect_repo
    holder = _App()

    def get_deps() -> AppDeps:
        factory = session_factory
        if factory is None:
            from app.infra.repositories import get_session_factory

            factory = get_session_factory()
        session = factory()
        ev = (
            default_evidence
            if default_evidence is not None
            else FileEvidenceStore(_DEFAULT_EVIDENCE_ROOT, db=session)
        )
        repo = default_repo if default_repo is not None else InspectionRepository()
        holder.d = AppDeps(session, ev, repo, llm_client, table, checkpointer)
        return holder.d

    # ------------------------------------------------------------------
    # Batches
    # ------------------------------------------------------------------

    @app.post("/api/batches", status_code=201)
    def create_batch(body: BatchCreate, d: AppDeps = Depends(get_deps)) -> dict:  # noqa: B008
        """R5: insert the draft batch and advance it to ``pending_intake``."""
        s = d.session
        rule = s.get(QualityRuleVersion, uuid.UUID(body.rule_version_id))
        if rule is None:
            raise HTTPException(
                status_code=400,
                detail={"rule_version_id": body.rule_version_id, "reason": "unknown rule version"},
            )
        batch = BatchRepository().create_batch(
            s,
            body.batch_no,
            body.project_id,
            body.model,
            uuid.UUID(body.rule_version_id),
            body.planned_quantity,
        )
        tr = transition("draft", "pending_intake", {}, kind="batch")
        if not tr.accepted:
            raise HTTPException(status_code=400, detail={"reason": tr.reason})
        batch.status = "pending_intake"
        s.commit()
        return {"batch_id": str(batch.batch_id), "status": batch.status}

    @app.get("/api/batches/{batch_id}")
    def get_batch_detail(batch_id: str, d: AppDeps = Depends(get_deps)) -> dict:  # noqa: B008
        """A8/B2: batch + per-SN verdict detail + sample conclusion + timeline."""
        s = d.session
        batch = s.get(DeliveryBatch, uuid.UUID(batch_id))
        if batch is None:
            raise HTTPException(status_code=404, detail={"batch_id": batch_id})
        inspections = [
            {
                "sn": r.sn,
                "intake_status": r.intake_status,
                "decision_status": r.decision_status,
                "decision_evidence": r.decision_evidence,
            }
            for r in s.scalars(
                select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
            ).all()
        ]
        conclusion = s.scalars(
            select(SampleConclusion).where(SampleConclusion.batch_id == batch.batch_id)
        ).first()
        traces = s.scalars(
            select(AgentTaskTrace)
            .where(AgentTaskTrace.batch_id == batch.batch_id)
            .order_by(AgentTaskTrace.started_at)
        ).all()
        timeline = [
            {
                "skill_name": t.skill_name,
                "target": (t.params_summary or {}).get("target"),
                "accepted": (t.params_summary or {}).get("accepted"),
                "started_at": t.started_at.isoformat() if t.started_at else None,
            }
            for t in traces
        ]
        return {
            "batch_id": str(batch.batch_id),
            "batch_no": batch.batch_no,
            "status": batch.status,
            "planned_quantity": batch.planned_quantity,
            "rule_version_id": str(batch.rule_version_id),
            "inspections": inspections,
            "sample_result": conclusion.result if conclusion else None,
            "status_timeline": timeline,
        }

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------

    _CONFIRMABLE = frozenset({"pmax", "temp", "isc", "voc", "vm", "im", "ff", "appearance_result"})

    def _parse_iso_utc(value: str):
        """ISO-8601 (with or without Z suffix) → aware UTC datetime."""

        s = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    def _graph(d: AppDeps, batch: DeliveryBatch) -> object:
        return build_main_chain_graph(
            {
                "session": d.session,
                "inspect_repo": d.inspect_repo,
                "llm": d.llm,
                "evidence": d.evidence,
                "iso_table": d.iso_table,
                "rule_version_id": batch.rule_version_id,
                "inspection_type": "iv",
            },
            checkpointer=d.checkpointer,
        )

    @app.post("/api/intake")
    def intake(body: IntakeRequestIn, d: AppDeps = Depends(get_deps)) -> dict:  # noqa: B008
        """Q1: trigger the full graph from ``intake`` to the interrupt point.

        The graph pauses at ``human_confirm`` on every run (spec §5.6). On
        auto-confirm (``confidence >= 0.8``, Review Focus #1 boundary) the
        route writes the whole-batch confirmation record and resumes
        immediately, so the caller sees ``status=confirmed``; low
        confidence returns ``status=pending`` + ``low_conf_sns`` (Q3) and
        the batch waits for the confirm endpoint. ``task_id`` =
        ``str(batch_id)`` (Q2).
        """
        s = d.session
        batch = s.get(DeliveryBatch, uuid.UUID(body.batch_id))
        if batch is None:
            raise HTTPException(status_code=404, detail={"batch_id": body.batch_id})

        graph = _graph(d, batch)
        cfg = {"configurable": {"thread_id": body.batch_id}}
        rows = [
            {**row, "source_type": row.get("source_type", body.source_type)}
            for row in body.inspections
        ]
        # JSON payloads carry captured_at as an ISO-8601 string; the ORM
        # expects a datetime (B3: marshal at the boundary, not in domain).
        for row in rows:
            if isinstance(row.get("captured_at"), str):
                row["captured_at"] = _parse_iso_utc(row["captured_at"])
        initial = {"batch_id": body.batch_id, "inspections": rows}
        for _chunk in graph.stream(initial, config=cfg):
            pass
        s.commit()
        state = graph.get_state(cfg).values or {}
        pending = bool(graph.get_state(cfg).next)

        # §5.6 / Q3: the confirmation record belongs to the API layer. On
        # auto-confirm (confidence >= 0.8, boundary Review Focus #1) this
        # route is the confirmer: persist the whole-batch intake_status +
        # human_confirmation evidence, then resume immediately. Low
        # confidence defers to the confirm endpoint. The judge node's A4
        # guard therefore always sees a confirmed DB state (§2.3).
        if pending and float(state.get("confidence") or 0.0) >= 0.8:
            for row in s.scalars(
                select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
            ).all():
                row.intake_status = "confirmed"
                for key, value in (body.fields or {}).items():
                    if key in _CONFIRMABLE:
                        setattr(row, key, value)
            d.evidence.save(
                "human_confirmation",
                {"kind": "batch", "id": body.batch_id},
                content=body.batch_id.encode("utf-8"),
                meta={"action": "confirm", "auto": True, "confidence": state.get("confidence")},
            )
            s.commit()
            from langgraph.types import Command

            for _chunk in graph.stream(
                Command(resume={"action": "confirm", "fields": body.fields}), config=cfg
            ):
                pass
            s.commit()
            state = graph.get_state(cfg).values or {}
            pending = bool(graph.get_state(cfg).next)
        return {
            "task_id": body.batch_id,
            "status": "pending" if pending else "confirmed",
            "extracted_fields": state.get("extraction") or {},
            "confidence": float(state.get("confidence") or 0.0),
            "low_conf_sns": state.get("low_conf_sns") or [],
        }

    @app.post("/api/intake/{task_id}/confirm")
    def confirm_intake(
        task_id: str,
        body: IntakeConfirmIn,
        d: AppDeps = Depends(get_deps),  # noqa: B008
    ) -> dict:
        """§5.6: persist the whole-batch confirmation, then resume the graph."""
        s = d.session
        batch = s.get(DeliveryBatch, uuid.UUID(task_id))
        if batch is None:
            raise HTTPException(status_code=404, detail={"task_id": task_id})

        # §5.6 / Q3: whole-batch intake_status + human_confirmation evidence
        # BEFORE the resume (graph nodes must not write DB state, spec §2.3).
        new_status = "confirmed" if body.action == "confirm" else "rejected"
        for row in s.scalars(
            select(ModuleInspection).where(ModuleInspection.batch_id == batch.batch_id)
        ).all():
            row.intake_status = new_status
            if body.action == "confirm" and body.fields:
                for key, value in body.fields.items():
                    if key in _CONFIRMABLE:
                        setattr(row, key, value)
        d.evidence.save(
            "human_confirmation",
            {"kind": "batch", "id": task_id},
            content=str(task_id).encode("utf-8"),
            meta={"action": body.action, "fields": body.fields},
        )
        s.commit()

        from langgraph.types import Command

        graph = _graph(d, batch)
        cfg = {"configurable": {"thread_id": task_id}}
        for _chunk in graph.stream(
            Command(resume={"action": body.action, "fields": body.fields}), config=cfg
        ):
            pass
        s.commit()
        return {"task_id": task_id, "intake_status": new_status}

    # ------------------------------------------------------------------
    # Judge (standalone skill call, Q1 — no graph)
    # ------------------------------------------------------------------

    @app.post("/api/batches/{batch_id}/judge")
    def judge(batch_id: str, d: AppDeps = Depends(get_deps)) -> dict:  # noqa: B008
        """Q1: independent judge-skill call for standalone re-judging."""
        s = d.session
        batch = s.get(DeliveryBatch, uuid.UUID(batch_id))
        if batch is None:
            raise HTTPException(status_code=404, detail={"batch_id": batch_id})
        try:
            result = judge_batch(s, d.evidence, uuid.UUID(batch_id), batch.rule_version_id)
        except UnconfirmedSnsError as exc:
            # Review Focus #3 / A4: 400 + unconfirmed SN checklist (B3).
            raise HTTPException(
                status_code=400, detail={"unconfirmed_sns": exc.unconfirmed}
            ) from exc
        s.commit()
        return {
            "batch_id": batch_id,
            "result": result["result"],
            "counts": result["counts"],
            "fail_sns": result["fail_sns"],
            "per_sn": result["per_sn"],
        }

    return app


#: Module-level default app for ``uvicorn app.main:app`` (memory checkpointer).
app = create_app()
