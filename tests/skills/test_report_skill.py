"""C8 regression: ReportSkill content_hash must be a REAL sha256 over the
merged report payload (markdown + canonical JSON), not a random uuid fragment.

Recomputes the digest from the Report row's stored content and asserts the
report_snapshot evidence's content_hash matches — the "evidence chain" core
promise that the hash is verifiable, not decorative.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import reports as report_api
from app.infra.models import (
    Base,
    DeliveryBatch,
    Evidence,
    ModuleInspection,
    QualityRuleVersion,
    Report,
)

_TS = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_BATCH = UUID("cccccc01-1111-2222-3333-444444444401")
_RULE = UUID("cccccc01-1111-2222-3333-444444444402")


def _seed(session: Any) -> None:
    rule = QualityRuleVersion(
        rule_version_id=_RULE,
        project_id="P-1",
        model="JH-550W",
        name="rv-1",
        nameplate_power=550.0,
        power_tolerance=0.5,
        parameter_standards={},
        current_bands={},
        sample_plan={},
        effective_at=_TS,
        version_no=1,
    )
    batch = DeliveryBatch(
        batch_id=_BATCH,
        batch_no="B-C8-001",
        project_id="P-1",
        model="JH-550W",
        planned_quantity=100,
        rule_version_id=_RULE,
        status="released",
    )
    ins = ModuleInspection(
        inspection_id=UUID("cccccc01-1111-2222-3333-444444444403"),
        batch_id=_BATCH,
        sn="SN-A",
        source_type="IV_CSV",
        captured_at=_TS,
        pmax=550.0,
        temp=25.0,
        isc=10.5,
        voc=0.5,
        vm=48.0,
        im=11.0,
        ff=0.75,
        appearance_result="pass",
    )
    session.add_all([rule, batch, ins])
    session.commit()


@pytest.fixture()
def api_client() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(report_api.router)
    app.dependency_overrides[report_api.get_session] = lambda: session
    client = TestClient(app)
    yield client, session, engine
    session.close()
    engine.dispose()


def test_report_content_hash_is_real_sha256(api_client: Any) -> None:
    client, session, _ = api_client
    _seed(session)
    resp = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    assert resp.status_code == 201, resp.text
    report_id = resp.json()["report_id"]
    report = session.get(Report, UUID(report_id))
    assert report is not None
    ev = session.get(Evidence, report.source_snapshot_id)
    assert ev is not None and ev.evidence_type == "report_snapshot"

    # Recompute the digest exactly as ReportSkill.generate builds the payload:
    # markdown + b"\n---JSON---\n" + canonical JSON of content_json.
    payload = (
        report.content_md.encode("utf-8")
        + b"\n---JSON---\n"
        + json.dumps(report.content_json, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert ev.content_hash == expected, (
        f"content_hash not a verifiable sha256 of the report payload: "
        f"{ev.content_hash!r} != {expected!r}"
    )
    # Full 64-hex digest, not a truncated uuid fragment.
    assert len(ev.content_hash.split(":", 1)[1]) == 64


def test_report_hash_stable_across_recompute(api_client: Any) -> None:
    """Same stored content -> identical recomputed digest (deterministic)."""
    client, session, _ = api_client
    _seed(session)
    resp = client.post(f"/api/batches/{_BATCH}/report", json={"report_type": "release"})
    report = session.get(Report, UUID(resp.json()["report_id"]))
    ev = session.get(Evidence, report.source_snapshot_id)

    def _digest() -> str:
        payload = (
            report.content_md.encode("utf-8")
            + b"\n---JSON---\n"
            + json.dumps(report.content_json, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        return hashlib.sha256(payload).hexdigest()

    assert _digest() == _digest()
    assert ev.content_hash == "sha256:" + _digest()
