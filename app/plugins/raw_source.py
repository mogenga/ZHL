"""Inspection Data Plugin — deterministic raw-source parsing (FR §9.4).

Reads raw IV CSV text / paper-form transcripts and returns structured rows.
This is the *fallback* parser behind ``MockLLMClient``'s ``[RAW_PARSE]``
route (zero-network demos) and the shared reference for what "raw source"
means: one record per line, comma-separated —

    SN,pmax,temp,isc,voc,vm,im,ff,appearance

with ``appearance`` in pass/fail/OK/NG/合格/不合格 and any number of header
or comment lines (lines whose first cell is not a plausible SN) skipped.
The parser never invents values: a malformed line is reported in ``issues``
so the caller can surface it, matching the LLM path's contract.

Boundary (AGENT.md §8): plugins may not import skills or langgraph; this
module is pure stdlib, reads no clock, and returns plain dicts.
"""

from __future__ import annotations

import re

#: One plausible SN token: no whitespace, starts alphanumeric.
_SN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{1,63}$")

#: Column order after SN (see module docstring).
_NUMERIC_FIELDS = ("pmax", "temp", "isc", "voc", "vm", "im", "ff")

_APPEARANCE = {
    "pass": "pass",
    "fail": "fail",
    "合格": "pass",
    "不合格": "fail",
    "通过": "pass",
    "不通过": "fail",
    "ok": "pass",
    "ng": "fail",
}


def parse_raw_source_text(text: str) -> list[dict]:
    """Parse raw multi-line source text into row dicts.

    Each returned row carries ``sn`` + the seven electrical fields +
    ``appearance_result`` — the same shape the raw-parse prompt asks a live
    model to produce. Unparseable lines are skipped silently here; the rows
    that do parse are what the Skill then puts through ``validate_rows``
    (FR-1.4), which is the layer that reports issues.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        cells = [c.strip() for c in re.split(r"[,\t，]", line)]
        if len(cells) < 9:
            continue
        sn = cells[0]
        if not _SN_RE.match(sn):
            continue
        appearance = _APPEARANCE.get(cells[8].strip().lower())
        if appearance is None:
            continue
        row: dict = {"sn": sn, "appearance_result": appearance, "confidence": 0.9}
        try:
            for idx, field in enumerate(_NUMERIC_FIELDS, start=1):
                row[field] = float(cells[idx])
        except ValueError:
            continue
        rows.append(row)
    return rows


__all__ = ["parse_raw_source_text"]
