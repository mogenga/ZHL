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

``parse_raw_source`` (FR-1.1/1.2) is the *real* extraction entry: raw IV
CSV text or a paper-form OCR transcript goes in, structured rows come out,
with per-row confidence and a deterministic FR-1.4 validation pass
(``validate_rows``) between the model and persistence — the model proposes,
the validator disposes.
"""

from __future__ import annotations

import json
import re

from app.plugins.evidence_store import EvidenceStore
from app.plugins.llm_client import LLMClient

#: The raw-source prompt contract (the mock routes on ``<RAW_SOURCE>``).
RAW_PARSE_MARKER = "[RAW_PARSE]"
_RAW_SOURCE_RE = re.compile(r"<RAW_SOURCE>(.*?)</RAW_SOURCE>", re.DOTALL)

#: Numeric sanity ranges (FR-1.4 "校验类型、单位"). A value outside these is
#: a parse failure (unit mismatch, column shift, OCR garbage), not a quality
#: finding — it must not silently become an inspection row.
_NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "pmax": (1.0, 1000.0),
    "temp": (-60.0, 200.0),
    "isc": (0.1, 100.0),
    "voc": (1.0, 200.0),
    "vm": (1.0, 200.0),
    "im": (0.1, 100.0),
    "ff": (0.01, 1.2),
}

#: SN shape gate: printable, no whitespace/comma, sane length.
_SN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{1,63}$")

#: Appearance synonyms a paper form might use (mapped, not rejected).
_APPEARANCE_MAP = {
    "pass": "pass",
    "fail": "fail",
    "合格": "pass",
    "不合格": "fail",
    "通过": "pass",
    "不通过": "fail",
    "ok": "pass",
    "ng": "fail",
}


def validate_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split model-produced rows into ``(valid, issues)`` (FR-1.4, pure).

    Checks type / unit range / SN format / in-submission duplicates /
    appearance vocabulary. No judgment about quality (Pass/Fail) happens
    here — that is the rule engine's job, one layer down.
    """
    valid: list[dict] = []
    issues: list[dict] = []
    seen: set[str] = set()
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            issues.append({"sn": None, "field": "-", "problem": f"row {idx}: not an object"})
            continue
        sn = str(row.get("sn") or "").strip()
        if not sn:
            issues.append({"sn": None, "field": "sn", "problem": f"row {idx}: missing SN"})
            continue
        if not _SN_RE.match(sn):
            issues.append(
                {"sn": sn, "field": "sn", "problem": "SN 格式不合法（含空格/逗号或过长）"}
            )
            continue
        if sn in seen:
            issues.append({"sn": sn, "field": "sn", "problem": "本次提交内 SN 重复"})
            continue

        bad = False
        clean: dict = {"sn": sn}
        for field, (lo, hi) in _NUMERIC_RANGES.items():
            raw = row.get(field)
            try:
                value = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                issues.append({"sn": sn, "field": field, "problem": f"缺失或非数字: {raw!r}"})
                bad = True
                continue
            if not (lo <= value <= hi):
                issues.append(
                    {"sn": sn, "field": field, "problem": f"超出合理范围 [{lo}, {hi}]: {value}"}
                )
                bad = True
                continue
            clean[field] = value

        appearance = _APPEARANCE_MAP.get(str(row.get("appearance_result") or "").strip().lower())
        if appearance is None:
            issues.append(
                {
                    "sn": sn,
                    "field": "appearance_result",
                    "problem": f"外观结论须为 pass/fail: {row.get('appearance_result')!r}",
                }
            )
            bad = True
        else:
            clean["appearance_result"] = appearance

        if bad:
            continue
        seen.add(sn)
        try:
            confidence = min(max(float(row.get("confidence", 0.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        findings = row.get("appearance_findings")
        clean["confidence"] = confidence
        clean["appearance_findings"] = (
            [str(f) for f in findings] if isinstance(findings, list) else []
        )
        valid.append(clean)
    return valid, issues


def parse_raw_source(
    llm: LLMClient,
    source_kind: str,
    raw_text: str,
    evidence: EvidenceStore,
    object_ref: dict,
) -> dict:
    """Parse raw IV CSV / form text into structured rows (FR-1.1/1.2/1.4).

    Returns ``{"rows": [...], "issues": [...], "confidence": float,
    "extraction": {sn: {fields, confidence, appearance_findings}}}``.

    Evidence: the *raw text* is archived as ``source_file`` (FR-1.2 保留原始
    文件), one ``extraction_result`` per valid SN keeps the downstream
    contract identical to the structured entry path.
    """
    prompt = (
        "You are a photovoltaic module inspection data extractor.\n"
        f"source_kind={source_kind}\n"
        "Parse EVERY module record in the raw source below into structured rows.\n"
        f"{RAW_PARSE_MARKER}\n"
        f"<RAW_SOURCE>\n{raw_text}\n</RAW_SOURCE>\n"
        'Return ONLY a JSON object with exactly these keys: {"rows": [{"sn": str, '
        '"pmax": number, "temp": number, "isc": number, "voc": number, "vm": number, '
        '"im": number, "ff": number, "appearance_result": "pass"|"fail", '
        '"confidence": number between 0 and 1, "appearance_findings": [str]}], '
        '"issues": [{"sn": str|null, "field": str, "problem": str}]}. '
        "Numbers must be plain numbers, no units. Do not invent missing values — "
        "list the row in issues instead. No markdown fence, no prose."
    )
    schema = {"type": "raw_parse", "source_kind": source_kind}
    structured = llm.chat(prompt, schema)

    evidence.save(
        "source_file",
        object_ref,
        content=raw_text.encode("utf-8"),
        meta={
            "source_kind": source_kind,
            "model": _model_version(llm),
            "raw_len": len(raw_text),
        },
    )

    raw_rows = structured.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        # A client that does not speak the raw-parse contract (a test double,
        # an older model proxy) answered with something unusable. Fall back to
        # the deterministic Inspection Data Plugin rather than failing the
        # intake: the raw text is what carries the data, the model is an
        # accelerator. ``model_version`` in the evidence meta still records
        # which path answered.
        from app.plugins.raw_source import parse_raw_source_text

        raw_rows = parse_raw_source_text(raw_text)
        structured = {**structured, "rows": raw_rows}
    valid, issues = validate_rows(raw_rows if isinstance(raw_rows, list) else [])
    model_issues = structured.get("issues")
    if isinstance(model_issues, list):
        for issue in model_issues:
            if isinstance(issue, dict):
                issues.append(
                    {
                        "sn": issue.get("sn"),
                        "field": issue.get("field"),
                        "problem": f"model-reported: {issue.get('problem')}",
                    }
                )

    extraction: dict[str, dict] = {}
    for row in valid:
        fields = {
            k: row[k] for k in ("pmax", "temp", "isc", "voc", "vm", "im", "ff", "appearance_result")
        }
        extraction[row["sn"]] = {
            "fields": fields,
            "confidence": row["confidence"],
            "appearance_findings": row["appearance_findings"],
        }
        evidence.save(
            "extraction_result",
            # ``sn`` is part of the ref: without it every module in the batch
            # hashed to one evidence id and the chain kept a single record.
            {**object_ref, "sn": row["sn"]},
            content=json.dumps(
                {
                    "source_sn": row["sn"],
                    "extracted_params": fields,
                    "appearance_findings": row["appearance_findings"],
                    "confidence": row["confidence"],
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            meta={"source_sn": row["sn"], "source_type": source_kind, "model": _model_version(llm)},
        )

    confidence = min((r["confidence"] for r in valid), default=0.0)
    return {"rows": valid, "issues": issues, "confidence": confidence, "extraction": extraction}


def _model_version(llm: LLMClient) -> str:
    """Trace/evidence-facing model identity (never claims a live model it
    did not use — ``ResilientLLMClient`` reports ``mock-degraded``)."""
    reporter = getattr(llm, "model_version", None)
    if callable(reporter):
        try:
            return str(reporter())
        except Exception:  # noqa: BLE001 — identity must never break extraction
            return "unknown"
    return "mock" if llm.__class__.__name__.startswith("Mock") else "openai-compat"


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
    # The schema travels *inside* the prompt as well as to the client: the
    # fixture mock ignores it, but a real model has no other way to learn the
    # expected shape. Relying on response_format={"type":"json_object"} alone
    # only guarantees "an object", not "an object with these keys".
    schema = {"type": "extraction", "source_type": source_type}
    prompt = (
        f"Extract inspection fields from {source_type} source: "
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)} "
        f"[SOURCE_SN:{sn}]\n"
        f"Return ONLY a JSON object with exactly these keys: "
        f'{{"extracted_params": {{<param_name>: <number>}}, '
        f'"confidence": <float between 0 and 1>, '
        f'"appearance_findings": [<string>]}}. '
        f"No markdown fence, no prose."
    )
    # Model call happens BEFORE any DB write (caller transaction stays clean).
    structured = llm.chat(prompt, schema)

    content = json.dumps(structured, ensure_ascii=False).encode("utf-8")
    evidence.save(
        "extraction_result",
        # ``sn`` in the ref keeps one evidence row per module (FR-1.7);
        # without it a multi-SN batch collapsed to a single record.
        {**object_ref, "sn": sn},
        content=content,
        meta={"source_sn": sn, "source_type": source_type, "model": _model_version(llm)},
    )

    # A live model can omit or mistype keys even when asked politely. The
    # confidence gate below decides whether a human must confirm, so a missing
    # value must degrade to "no confidence" (forcing confirmation) rather than
    # raise — a hard ``structured["confidence"]`` would abort the whole
    # intake on one malformed model reply.
    raw_confidence = structured.get("confidence")
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    return {
        "fields": structured.get("extracted_params", {}),
        "confidence": confidence,
        "appearance_findings": structured.get("appearance_findings", []),
    }
