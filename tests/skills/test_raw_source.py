"""Raw-source intake tests (FR-1.1/1.2/1.4): raw text -> validated rows -> graph.

The parse path must (a) turn an untidy raw source into inspection rows,
(b) reject unit/format violations before they reach the DB, and (c) go
through the exact same graph (same confirmation semantics) as the
structured entry.
"""

from __future__ import annotations

import pytest

from app.plugins.evidence_store import FileEvidenceStore
from app.plugins.llm_client import MockLLMClient
from app.plugins.raw_source import parse_raw_source_text

# ---------------------------------------------------------------------------
# Deterministic plugin parser (the mock's [RAW_PARSE] fallback)
# ---------------------------------------------------------------------------


def test_raw_parser_reads_csv_rows_with_header_and_comments() -> None:
    text = (
        "# IV 测试导出 / 设备 UT-2200\n"
        "SN,pmax,temp,isc,voc,vm,im,ff,appearance\n"
        "SN-N-001,550.2,25.0,10.5,48.0,40.0,13.0,0.80,pass\n"
        "SN-N-002 , 548.0 , 25.1 ,10.6,48.1,40.2,13.1,0.79, 合格\n"
    )
    rows = parse_raw_source_text(text)
    assert [r["sn"] for r in rows] == ["SN-N-001", "SN-N-002"]
    assert rows[0]["pmax"] == 550.2
    # Synonym mapped, not passed through raw.
    assert rows[1]["appearance_result"] == "pass"


def test_raw_parser_skips_unparseable_lines_without_inventing() -> None:
    text = "SN-N-001,550.2,25.0,10.5,48.0,40.0,13.0,0.80,pass\nSN-BAD,abc,...\n"
    rows = parse_raw_source_text(text)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# validate_rows — the FR-1.4 gate between model output and DB
# ---------------------------------------------------------------------------


def _row(**over: object) -> dict:
    base = {
        "sn": "SN-N-001",
        "pmax": 550.0,
        "temp": 25.0,
        "isc": 10.5,
        "voc": 48.0,
        "vm": 40.0,
        "im": 13.0,
        "ff": 0.80,
        "appearance_result": "pass",
        "confidence": 0.9,
    }
    base.update(over)
    return base


def test_validate_rows_accepts_clean_rows() -> None:
    from app.skills.extract import validate_rows

    valid, issues = validate_rows([_row()])
    assert issues == []
    assert valid[0]["sn"] == "SN-N-001"
    assert valid[0]["pmax"] == 550.0


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"pmax": "十二点五"}, "pmax"),  # not numeric (the classic OCR/unit case)
        ({"pmax": 55000.0}, "pmax"),  # magnitude off by 100 -> wrong unit
        ({"ff": 80.0}, "ff"),  # percent instead of fraction
        ({"appearance_result": "良好"}, "appearance_result"),  # unmapped vocabulary
        ({"sn": "SN N 001"}, "sn"),  # whitespace in SN
        ({"sn": ""}, "sn"),
    ],
)
def test_validate_rows_rejects_bad_field_and_names_it(over: dict, field: str) -> None:
    from app.skills.extract import validate_rows

    valid, issues = validate_rows([_row(**over)])
    assert valid == []
    assert issues and issues[0]["field"] == field


def test_validate_rows_flags_in_submission_duplicates() -> None:
    from app.skills.extract import validate_rows

    valid, issues = validate_rows([_row(), _row()])
    assert len(valid) == 1
    assert any("重复" in i["problem"] for i in issues)


# ---------------------------------------------------------------------------
# parse_raw_source Skill — evidence + extraction contract
# ---------------------------------------------------------------------------


@pytest.fixture()
def evidence(tmp_path) -> FileEvidenceStore:
    return FileEvidenceStore(root=tmp_path, db=None)


def _raw_client() -> MockLLMClient:
    from pathlib import Path

    return MockLLMClient(Path(__file__).resolve().parents[2] / "tests" / "fixtures")


def test_parse_raw_source_end_to_end(evidence: FileEvidenceStore) -> None:
    from app.skills.extract import parse_raw_source

    raw = (
        "SN-R-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass\n"
        "SN-R-002,549.0,25.0,10.5,48.0,40.0,13.0,0.81,fail\n"
    )
    result = parse_raw_source(
        _raw_client(), "iv_csv", raw, evidence, {"kind": "batch", "id": "B-RAW"}
    )
    assert {r["sn"] for r in result["rows"]} == {"SN-R-001", "SN-R-002"}
    assert result["issues"] == []
    # The raw text itself is archived (FR-1.2 保留原始文件).
    stored = evidence.list_by_object({"kind": "batch", "id": "B-RAW"})
    types = [s.evidence_type for s in stored]
    assert "source_file" in types
    assert types.count("extraction_result") == 2
    assert result["extraction"]["SN-R-002"]["fields"]["appearance_result"] == "fail"


def test_parse_raw_source_reports_every_rejected_row(evidence: FileEvidenceStore) -> None:
    from app.skills.extract import parse_raw_source

    raw = (
        "SN-R-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass\n"
        "SN-R-002,99999.0,25.0,10.5,48.0,40.0,13.0,0.81,pass\n"
    )
    result = parse_raw_source(
        _raw_client(), "iv_csv", raw, evidence, {"kind": "batch", "id": "B-RAW2"}
    )
    assert [r["sn"] for r in result["rows"]] == ["SN-R-001"]
    assert result["issues"] and result["issues"][0]["sn"] == "SN-R-002"
    # min() over *valid* rows only — a rejected row must not drag confidence.
    assert result["confidence"] == 0.9
