"""Extract Skill (PA plan Task 5 / PA spec §5.5, R4).

``extract_inspection_data`` is the orchestration semantics of one extraction:
build the LLM prompt (embedding the ``[SOURCE_SN:<sn>]`` marker per Task 3
B1), call the pluggable ``LLMClient`` *outside* any DB transaction
(AGENT.md §4.3), write the ``extraction_result`` evidence record through
the injected ``EvidenceStore``, and return a plain structured dict.

No business judgment here: the Skill does not grade, reject, or aggregate
— it returns per-SN ``fields`` / ``confidence`` / ``appearance_findings``.
Batch-level confidence aggregation (min, Q3) is the graph node's job
(Task 7).
"""

from __future__ import annotations

import json

from app.plugins.evidence_store import EvidenceStore
from app.plugins.llm_client import LLMClient


def extract_inspection_data(
    llm: LLMClient,
    source_type: str,
    payload: dict,
    evidence: EvidenceStore,
    object_ref: dict,
) -> dict:
    """Run one extraction for ``payload`` (must carry the SN under ``"sn"``).

    Returns ``{"fields": dict, "confidence": float, "appearance_findings": list}``
    and writes the raw structured LLM result as the ``extraction_result``
    evidence record (content = the result JSON, hash-keyed by the store).
    """
    sn = payload["sn"]
    prompt = (
        f"Extract inspection fields from {source_type} source: "
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)} "
        f"[SOURCE_SN:{sn}]"
    )
    schema = {"type": "extraction", "source_type": source_type}
    # Model call happens BEFORE any DB write (caller transaction stays clean).
    structured = llm.chat(prompt, schema)

    content = json.dumps(structured, ensure_ascii=False).encode("utf-8")
    evidence.save(
        "extraction_result",
        object_ref,
        content=content,
        meta={"source_sn": sn, "source_type": source_type},
    )

    return {
        "fields": structured.get("extracted_params", {}),
        "confidence": structured["confidence"],
        "appearance_findings": structured.get("appearance_findings", []),
    }
