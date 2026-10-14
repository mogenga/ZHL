"""create_batch must reject non-published rule versions (Task 5, rule-version mgmt)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import rule_versions as _rv
from app.domain.types import IsoRow
from app.infra.models import Base, QualityRuleVersion
from app.infra.repositories import InspectionRepository
from app.main import create_app
from app.plugins.evidence_store import EvidenceRef

_ROOT = Path(__file__).resolve().parents[2]
_SEED = json.loads((_ROOT / "tests" / "fixtures" / "rules_seed.json").read_text(encoding="utf-8"))[
    "rules"
][0]
_ISO_TABLE = [
    IsoRow(**row)
    for row in json.loads(
        (_ROOT / "tests" / "fixtures" / "iso2859_1_aql.json").read_text(encoding="utf-8")
    )["rows"]
]
_LOT = 10


class StubEvidence:
    def __init__(self) -> None:
        self.rows: list[EvidenceRef] = []
        self._on_confirm: list = []

    def save(self, etype, object_ref, *, content=None, path=None, meta=None, now=None):
        ref = EvidenceRef(
            evidence_id=str(uuid.uuid4()),
            evidence_type=etype,
            object_ref=dict(object_ref),
            content_hash="",
            file_path="",
            meta=dict(meta or {}),
        )
        self.rows.append(ref)
        if etype == "human_confirmation" and (meta or {}).get("auto"):
            from sqlalchemy import select as _sel

            from app.infra.models import ModuleInspection as _MI

            for session in self._on_confirm:
                for row in session.scalars(
                    _sel(_MI).where(_MI.batch_id == uuid.UUID(object_ref["id"]))
                ).all():
                    row.intake_status = "confirmed"
                session.flush()
        return ref

    def get(self, evidence_id):
        raise KeyError("stub store keeps no payloads")

    def list_by_object(self, object_ref):
        return []


def _seed_rule(s, status: str, version_no: int = 1) -> QualityRuleVersion:
    rule = QualityRuleVersion(
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        name=f"status-{status}",
        nameplate_power=_SEED["nameplate_power"],
        power_tolerance=_SEED["power_tolerance"],
        parameter_standards=_SEED["parameter_standards"],
        current_bands=_SEED["current_bands"],
        sample_plan=_SEED["sample_plan"],
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=status,
        version_no=version_no,
    )
    s.add(rule)
    s.flush()
    s.commit()
    return rule


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    return engine, factory


def _client(factory, rule_version_id: str) -> TestClient:
    s = factory()
    evidence = StubEvidence()
    app = create_app(
        session_factory=lambda: factory(),
        llm=_PassLLM(),
        evidence=evidence,
        iso_table=_ISO_TABLE,
        inspect_repo=InspectionRepository(),
    )
    # Mount the rule-versions router so disable/publish endpoints are reachable.
    app.include_router(_rv.router)
    app.dependency_overrides[_rv.get_session] = lambda: factory()
    evidence._on_confirm = [factory()]
    client = TestClient(app)
    client._rule_version_id = rule_version_id
    s.close()
    return client


def _r_for(sn: str) -> dict:
    return {
        "sn": sn,
        "source_type": "IV_CSV",
        "captured_at": "2026-10-01T00:00:00Z",
        "pmax": 550.0,
        "temp": 25.0,
        "isc": 10.5,
        "voc": 48.0,
        "vm": 40.0,
        "im": 13.0,
        "ff": 0.8,
        "appearance_result": "pass",
    }


class _PassLLM:
    """Deterministic stub: high confidence, echoes the source SN."""

    def chat(self, prompt: str, schema: dict) -> dict:
        import re

        m = re.search(r"\[SOURCE_SN:([^\]]+)\]", prompt)
        sn = m.group(1) if m else "SN-X"
        return {
            "source_sn": sn,
            "extracted_params": {"pmax": 550.0, "im": 13.0, "voc": 48.0, "isc": 10.5, "ff": 0.8},
            "appearance_findings": [],
            "confidence": 0.95,
        }


def test_create_batch_rejects_draft_rule(env) -> None:
    _, factory = env
    s = factory()
    rule = _seed_rule(s, "draft", version_no=1)
    client = _client(factory, str(rule.rule_version_id))
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": "B-RS-1",
            "project_id": _SEED["project_id"],
            "model": _SEED["model"],
            "rule_version_id": str(rule.rule_version_id),
            "planned_quantity": _LOT,
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "not published" in detail["reason"]
    assert detail["status"] == "draft"
    s.close()


def test_create_batch_rejects_disabled_rule(env) -> None:
    _, factory = env
    s = factory()
    rule = _seed_rule(s, "disabled", version_no=2)
    client = _client(factory, str(rule.rule_version_id))
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": "B-RS-2",
            "project_id": _SEED["project_id"],
            "model": _SEED["model"],
            "rule_version_id": str(rule.rule_version_id),
            "planned_quantity": _LOT,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["status"] == "disabled"
    s.close()


def test_create_batch_accepts_published_rule(env) -> None:
    _, factory = env
    s = factory()
    rule = _seed_rule(s, "published", version_no=3)
    client = _client(factory, str(rule.rule_version_id))
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": "B-RS-3",
            "project_id": _SEED["project_id"],
            "model": _SEED["model"],
            "rule_version_id": str(rule.rule_version_id),
            "planned_quantity": _LOT,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending_intake"
    s.close()


def test_disabled_rule_does_not_affect_existing_batch_rejudge(env) -> None:
    """Key boundary: disabling a rule does NOT change in-flight snapshots."""
    _, factory = env
    s = factory()
    rule = _seed_rule(s, "published", version_no=4)
    client = _client(factory, str(rule.rule_version_id))
    batch = client.post(
        "/api/batches",
        json={
            "batch_no": "B-RS-4",
            "project_id": _SEED["project_id"],
            "model": _SEED["model"],
            "rule_version_id": str(rule.rule_version_id),
            "planned_quantity": _LOT,
        },
    ).json()
    sns = [f"SN-DIS-{i:02d}" for i in range(_LOT)]
    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for(sn) for sn in sns]},
    )
    task_id = resp.json()["task_id"]
    confirm = client.post(f"/api/intake/{task_id}/confirm", json={"action": "confirm"})
    assert confirm.status_code == 200
    # judge once before disable -> pass
    r1 = client.post(f"/api/batches/{batch['batch_id']}/judge")
    assert r1.status_code == 200
    assert r1.json()["result"] == "Pass"

    # disable the rule now via the rule-versions API
    disable = client.post(
        f"/api/quality-rule-versions/{rule.rule_version_id}/disable",
        json={"operator": "ops", "reason": "criterion obsolete"},
    )
    assert disable.status_code == 200

    # judge again (rejudge) — in-flight snapshot must still pass
    r2 = client.post(f"/api/batches/{batch['batch_id']}/judge")
    assert r2.status_code == 200, r2.text
    assert r2.json()["result"] == "Pass"
    s.close()
