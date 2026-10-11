"""Pydantic v2 request / response schemas (P0 spec §5.3, Task 7 skeleton).

Phase 0 provides the *shape* of the API payloads for Phase A/B: reusable
models that mirror the domain dataclasses (judge / release / state-machine /
sampling inputs-outputs) so API routes can validate request/response bodies
without re-inventing the contract. These are skeletons — no route wiring
(that is Phase A/B), and no DB coupling.

Cross-layer contract: schemas reuse ``app.domain.types`` dataclasses as their
field types wherever the domain already defines the shape, so the request
layer and the pure-function layer never drift apart.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.domain.types import (
    AssertionResult,
    Decision,
    IncidentTrigger,
    ReinspectionResult,
    TransitionResult,
)

# ---------------------------------------------------------------------------
# Ingest / intake
# ---------------------------------------------------------------------------


class IntakeRequest(BaseModel):
    """Body of ``POST /api/intake`` (Phase A fills the orchestration)."""

    batch_id: str = Field(..., description="Existing delivery batch to intake into")
    source_type: Literal["OCR_FORM", "IV_CSV", "MANUAL", "SIMULATED_DEVICE"]
    file_path: str | None = None
    inline_payload: str | None = Field(default=None, description="Inline content, if any")


class IntakeResponse(BaseModel):
    task_id: str
    status: Literal["pending", "confirmed", "rejected"]
    extracted_fields: dict = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


class JudgeRequest(BaseModel):
    """Body of ``POST /api/batches/{id}/judge``."""

    batch_id: str
    # The rule version the decision is computed against is resolved server-side
    # from the batch; the snapshot is returned in the response.
    operator: str = "quality-engineer"


class JudgeResponse(BaseModel):
    batch_id: str
    decision: Decision
    # 全部组件判定汇总
    counts: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Release assertion
# ---------------------------------------------------------------------------


class ReleaseRequest(BaseModel):
    batch_id: str
    # Six per-item checks are computed server-side; the request only carries
    # the operator identity for the trace.
    operator: str = "quality-engineer"


class ReleaseResponse(BaseModel):
    batch_id: str
    result: AssertionResult
    # Human-approval is a separate endpoint; this is the assertion outcome.
    released: bool = False


# ---------------------------------------------------------------------------
# Incident / reinspection
# ---------------------------------------------------------------------------


class IncidentCreateRequest(BaseModel):
    batch_id: str
    trigger: IncidentTrigger
    severity: Literal["critical", "major", "minor"]
    owner: str | None = None


class ReinspectionCreateRequest(BaseModel):
    incident_id: str
    inspection_id: str
    rule_version_id: str
    result: Literal["pass", "fail"]
    executor: str
    executed_at: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReinspectionResponse(BaseModel):
    result: ReinspectionResult


# ---------------------------------------------------------------------------
# State transition
# ---------------------------------------------------------------------------


class TransitionRequest(BaseModel):
    batch_id: str
    target_state: str
    kind: Literal["batch", "incident"] = "batch"
    preconditions: dict = Field(default_factory=dict)


class TransitionResponse(BaseModel):
    result: TransitionResult


# ---------------------------------------------------------------------------
# Rule / sample lookup
# ---------------------------------------------------------------------------


class RuleVersionResponse(BaseModel):
    rule_version_id: str
    model: str
    status: Literal["draft", "published", "disabled"]
    version_no: int
    nameplate_power: float
    power_tolerance: float


__all__ = [
    "IntakeRequest",
    "IntakeResponse",
    "JudgeRequest",
    "JudgeResponse",
    "ReleaseRequest",
    "ReleaseResponse",
    "IncidentCreateRequest",
    "ReinspectionCreateRequest",
    "ReinspectionResponse",
    "TransitionRequest",
    "TransitionResponse",
    "RuleVersionResponse",
]
