"""Traceability plugin + attribution skill (PB Task 5, D2/D3).

``TraceabilityPlugin`` is the deterministic *evidence gate + rank* step that
sits between the LLM's hypothesis list and persistence. It enforces the two
semantics named by the Phase B spec:

- **D2 (evidence gate)**: a hypothesis whose ``evidence_ids`` list is empty
  is dropped — the gate is on the *evidence ID's presence*, and the
  referenced evidence must actually exist in the store (Review Focus #5:
  not merely non-empty, but real).
- **D3 (confidence is a sort signal only)**: the surviving hypotheses are
  re-numbered ``rank`` by descending ``confidence``. Confidence never
  *gates* — a low-confidence hypothesis with real evidence keeps its
  rank; it just orders later.

The plugin never reads the DB: cross-batch hit counts arrive as a
caller-supplied ``cross_batch_counts`` mapping (evidence_id -> count of
other-batch SNs matching that evidence's signature).

``AttributionSkill`` wires the pieces together with the transaction
discipline AGENT.md §4.3 demands: the LLM call happens *outside* any DB
transaction, and only the write of the validated hypotheses + trace row
runs in a single transaction on the caller's session.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.plugins.evidence_store import EvidenceStore
from app.plugins.trace_store import TraceStore

# The LLM output contract is the shape the mock fixture pins (PB L1a):
# a list of hypotheses, each {cause, confidence, evidence_ids[],
# recommended_action}. The skill passes this whole object to the client as
# the declarative ``schema`` (the client's job is to return something
# conforming; the mock just returns the fixture).
_LLM_SCHEMA = {
    "hypotheses": [
        {
            "cause": "string",
            "confidence": "float",
            "evidence_ids": ["string"],
            "recommended_action": "string",
        }
    ]
}

#: Keys a live model may use instead of ``hypotheses`` when it decides to
#: rearrange the requested shape. A real endpoint answered with
#: ``hypotheses_ranked`` plus extra keys (``key_observations``,
#: ``next_step_summary``) and the Skill's strict ``.get("hypotheses", [])``
#: silently produced zero hypotheses — the whole attribution showed up empty
#: in the UI while the model had in fact answered well. The prompt names the
#: contract; this list is the tolerance for models that rename it anyway.
_HYPOTHESIS_KEYS = ("hypotheses", "hypotheses_ranked", "root_causes", "causes")

#: Per-item key aliases inside a hypothesis.
_CAUSE_KEYS = ("cause", "hypothesis", "description", "root_cause")
_EVIDENCE_KEYS = ("evidence_ids", "evidence", "evidence_refs")
_ACTION_KEYS = ("recommended_action", "action", "recommendation", "suggestion")
_CONFIDENCE_KEYS = ("confidence", "score", "probability")


def _normalize_hypotheses(payload: dict) -> list[dict]:
    """Coerce a model reply into the canonical hypothesis shape.

    Tolerant by design: the model is being helpful, not adversarial — a
    renamed key or a string-instead-of-list evidence field should degrade to
    a usable hypothesis (which the D2 gate then judges on evidence), not to
    an empty result. Anything that cannot be read as a hypothesis at all is
    dropped rather than invented.
    """
    if not isinstance(payload, dict):
        return []
    raw = None
    for key in _HYPOTHESIS_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value:
            raw = value
            break
    if raw is None:
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                out.append({"cause": item.strip(), "confidence": 0.0, "evidence_ids": []})
            continue
        cause = next((str(item[k]) for k in _CAUSE_KEYS if item.get(k)), "")
        if not cause:
            continue
        evidence = next((item[k] for k in _EVIDENCE_KEYS if item.get(k) is not None), [])
        if isinstance(evidence, str):
            evidence = [evidence] if evidence.strip() else []
        elif not isinstance(evidence, list):
            evidence = []
        action = next((str(item[k]) for k in _ACTION_KEYS if item.get(k)), None)
        confidence = next((item[k] for k in _CONFIDENCE_KEYS if item.get(k) is not None), 0.0)
        try:
            confidence = min(max(float(confidence), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        out.append(
            {
                "cause": cause,
                "confidence": confidence,
                "evidence_ids": [str(e) for e in evidence if str(e).strip()],
                "recommended_action": action or "",
            }
        )
    return out


#: Prompt digest head length recorded in the trace params_summary — a
#: 64-char prefix is enough to correlate a trace row with a prompt without
#: persisting the full (potentially secret-bearing) prompt text.
_PROMPT_HEAD_CHARS = 64


def _model_version(llm: Any) -> str:
    """Trace-facing model identity (never claims a model that did not answer).

    ``ResilientLLMClient`` reports ``mock-degraded`` once it has fallen back;
    a bare mock reports ``mock``; a live client reports its configured model
    name.
    """
    reporter = getattr(llm, "model_version", None)
    if callable(reporter):
        try:
            return str(reporter())
        except Exception:  # noqa: BLE001 — identity must never break attribution
            return "unknown"
    if llm.__class__.__name__.startswith("Mock"):
        return "mock"
    # A bare OpenAI-compatible client carries its configured model name.
    configured = getattr(llm, "model", None)
    if configured:
        return str(configured)
    return "openai-compat"


class TraceabilityPlugin:
    """Deterministic validate + rank of LLM-produced hypothesis lists."""

    def validate_evidence(
        self,
        hypotheses: list[dict],
        evidence_store: EvidenceStore,
        cross_batch_counts: dict[str, int] | None = None,
    ) -> list[dict]:
        """Gate + rank + annotate. See module docstring for D2/D3 semantics."""
        counts = cross_batch_counts or {}
        # D2: keep only hypotheses with >=1 evidence id, where EVERY id
        # exists in the store (Review Focus #5 — "real" not "non-empty").
        surviving: list[dict] = []
        for h in hypotheses:
            ids: list[str] = h.get("evidence_ids") or []
            if not ids:
                continue
            if any(not self._evidence_exists(evidence_store, eid) for eid in ids):
                continue
            surviving.append(h)

        # D3: confidence only orders. Sort desc by confidence, then assign
        # 1-based rank.
        surviving.sort(key=lambda h: h.get("confidence", 0.0), reverse=True)
        out: list[dict] = []
        for rank, h in enumerate(surviving, start=1):
            annotated = dict(h)
            annotated["rank"] = rank
            ids = h.get("evidence_ids") or []
            annotated["impact_scope"] = {
                "affected_sns": len(ids),
                "cross_batch_hits": sum(counts.get(eid, 0) for eid in ids),
            }
            out.append(annotated)
        return out

    @staticmethod
    def _evidence_exists(store: EvidenceStore, evidence_id: str) -> bool:
        try:
            store.get(evidence_id)
            return True
        except KeyError:
            return False


class AttributionSkill:
    """End-to-end attribution: prompt -> LLM -> validate -> persist + trace.

    The skill does not own the transaction boundary; it writes through the
    caller's ``session`` and flushes. The LLM call happens *before* any
    session write, which is the AGENT.md §4.3 discipline (LLM outside the
    transaction; the DB write of hypotheses + trace is one unit).
    """

    def __init__(
        self,
        llm: Any,
        evidence_store: EvidenceStore,
        cross_batch_counts: dict[str, int] | None = None,
    ) -> None:
        self._llm = llm
        self._evidence_store = evidence_store
        self._cross_batch_counts = cross_batch_counts or {}

    def run(
        self,
        incident: Any,
        *,
        trace: TraceStore,
        session: Any,
        started_at: datetime | None = None,
        prompt_context: dict | None = None,
    ) -> dict:
        """Run one attribution pass for ``incident`` (a ``QualityIncident``).

        Returns the validated hypotheses list (the same object written to
        ``incident.root_cause_hypotheses``) so callers can branch on it.
        """
        incident_id = str(incident.incident_id)
        prompt = self._build_prompt(incident, prompt_context)

        # --- LLM call: OUTSIDE the transaction (AGENT.md §4.3) ------------
        llm_result: dict = self._llm.chat(prompt, _LLM_SCHEMA)
        hypotheses = _normalize_hypotheses(llm_result)
        plugin = TraceabilityPlugin()
        validated = plugin.validate_evidence(
            hypotheses,
            self._evidence_store,
            cross_batch_counts=self._cross_batch_counts,
        )

        # --- Business write + trace: one unit on the caller's session -----
        incident.root_cause_hypotheses = validated
        session.flush()

        trace.record(
            session,
            task_id=str(uuid.uuid4()),
            batch_id=getattr(incident, "batch_id", None),
            skill_name="attribution",
            input_ref={"kind": "incident", "id": incident_id},
            plugins_invoked=["llm_client", "traceability"],
            params_summary={
                # Digest only — never the full prompt / any secret.
                "prompt_len": len(prompt),
                "prompt_head": prompt[:_PROMPT_HEAD_CHARS],
                "model": _model_version(self._llm),
            },
            # Never hardcode "mock": with a real endpoint configured the old
            # constant mislabelled a live model's output as fixture data in
            # the very trace row a reviewer reads to judge provenance.
            model_version=_model_version(self._llm),
            structured_output={"hypotheses": validated},
            validation_result="pass" if validated else "partial",
            failure_reason=None if validated else "no hypothesis survived the evidence gate",
            human_action=None,
            started_at=started_at or datetime.now(UTC),
            finished_at=None,
        )
        return validated

    def _build_prompt(self, incident: Any, context: dict | None = None) -> str:
        """Assemble the prompt the LLM (mock) routes on.

        The mock client extracts ``incident_id`` from the prompt via regex,
        so the prompt MUST contain the incident's UUID string verbatim
        (``incident_id=<uuid>``). Cross-batch evidence references are
        listed as ``evidence_ids`` so the caller can see the evidence layer
        the LLM will ground in.

        ``context`` (optional, filled by the API layer) carries the *problem*
        — failing SNs, the batch/model, the actual rule verdicts. Without it
        a live model can only answer in generalities ("rule mismatch", "data
        drift"), which reads as noise next to a specific 470 W module; with
        it the hypotheses name the real failure.
        """
        evidence_ids = list(getattr(incident, "evidence_ids", []) or [])
        incident_uuid = str(incident.incident_id)
        lines = [
            "You are a photovoltaic module quality engineer doing root-cause "
            "analysis on a FAILED inspection batch. Base your hypotheses on "
            "the concrete values below; cite the evidence ids provided.",
            f"incident_id={incident_uuid}",
            f"severity={getattr(incident, 'severity', '')}",
            f"trigger_type={getattr(incident, 'trigger_type', '')}",
            f"evidence_ids={evidence_ids}",
        ]
        for key in ("batch_no", "model", "failing_sns", "rule_findings"):
            if context and context.get(key) is not None:
                lines.append(f"{key}={json.dumps(context[key], ensure_ascii=False, default=str)}")
        lines.append(
            "Requirements: give 2-4 ranked hypotheses; every hypothesis except "
            "at most one must cite at least one of the evidence_ids above "
            "(a hypothesis with no evidence will be discarded); state confidence "
            "as a number between 0 and 1. Return hypotheses with "
            "cause / confidence / evidence_ids / recommended_action."
        )
        return "\n".join(lines)
