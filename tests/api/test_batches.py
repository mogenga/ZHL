"""API tests for batch routes (PA plan Task 8, A12 / B3 / Q1 / Q2 / Q3).

``create_app`` wires the P0 repositories + Task 7 graph over an injected
SQLite (StaticPool, single in-memory DB) environment; the real
``MockLLMClient`` is driven with per-test fixtures so extraction confidence
is controlled. Error responses follow B3 (``{"detail": ...}``).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.domain.types import IsoRow
from app.infra.models import Base, DeliveryBatch, ModuleInspection, QualityRuleVersion
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
_INSPECTION_TYPE = _SEED["sample_plan"]["inspection_type"]
_LEVEL = _SEED["sample_plan"]["inspection_level"]
_AQL = float(_SEED["sample_plan"]["aql"])
_LOT = 10  # hit interval [9,15] at the seed plan's AQL

_INSPECT_KEYS = ("sn", "pmax", "temp", "isc", "voc", "vm", "im", "ff", "appearance_result")


class StubEvidence:
    """In-memory EvidenceStore stand-in; records rows for assertions.

    On ``human_confirmation`` (auto-confirm, PA spec 5.6 Q3) the stub marks
    every inspection row of the batch confirmed in the wired session -
    mirroring the production evidence store's DB half; graph nodes never
    write DB state (spec 2.3).
    """

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


def _seed_rule(s) -> None:
    rule = QualityRuleVersion(
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        name="JH-550W default",
        nameplate_power=_SEED["nameplate_power"],
        power_tolerance=_SEED["power_tolerance"],
        parameter_standards=_SEED["parameter_standards"],
        current_bands=_SEED["current_bands"],
        sample_plan=_SEED["sample_plan"],
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="published",
        version_no=_SEED["version_no"],
    )
    s.add(rule)
    s.flush()
    s.commit()


class _ConfidenceLLM:
    """Deterministic LLM stub: fixed confidence, echoes the SOURCE_SN marker.

    Mirrors the Task 7 CountingLLM pattern so the API graph tests exercise
    the R4 deterministic fixture contract without per-SN fixture authoring
    (PA spec 5.7 B1 marker contract).
    """

    def __init__(self, confidence: float, source_sn: str) -> None:
        self._confidence = confidence
        self._source_sn = source_sn

    def chat(self, prompt: str, schema: dict) -> dict:
        import re as _re

        m = _re.search(r"\[SOURCE_SN:([^\]]+)\]", prompt)
        sn = m.group(1) if m else self._source_sn
        return {
            "source_sn": sn,
            "extracted_params": {"pmax": 550.0, "im": 13.0, "voc": 48.0, "isc": 10.5, "ff": 0.8},
            "appearance_findings": [],
            "confidence": self._confidence,
        }


def _rule_version_id(session_factory) -> str:
    """Read the UUID back through a live session (avoid detached-attr access)."""
    s = session_factory()
    row = s.get(QualityRuleVersion, _SHARED_RULE_UUID)
    return str(row.rule_version_id)


def _shared_engine() -> object:
    global _SHARED_ENGINE
    if _SHARED_ENGINE is None:
        _SHARED_ENGINE = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
    return _SHARED_ENGINE


_SHARED_ENGINE = None
_SHARED_RULE_UUID: uuid.UUID | None = None


def _ensure_rule(session_factory) -> None:
    global _SHARED_RULE_UUID
    if _SHARED_RULE_UUID is None:
        s = session_factory()
        existing = s.get(QualityRuleVersion, _SEED_RULE_UUID)
        if existing is None:
            rule = QualityRuleVersion(
                project_id=_SEED["project_id"],
                model=_SEED["model"],
                name="JH-550W default",
                nameplate_power=_SEED["nameplate_power"],
                power_tolerance=_SEED["power_tolerance"],
                parameter_standards=_SEED["parameter_standards"],
                current_bands=_SEED["current_bands"],
                sample_plan=_SEED["sample_plan"],
                effective_at=datetime(2026, 1, 1, tzinfo=UTC),
                status="published",
                version_no=_SEED["version_no"],
            )
            s.add(rule)
            s.flush()
            _SHARED_RULE_UUID = rule.rule_version_id
            s.commit()
        else:
            _SHARED_RULE_UUID = existing.rule_version_id


_SEED_RULE_UUID = uuid.uuid5(uuid.NAMESPACE_URL, "zhl-phase-a-api-default-rule")


def _make_client(
    confidence: float,
    source_sn: str,
    sns: list[str],
    s: sessionmaker,
    session_factory,
    iso_table: list[IsoRow],
) -> TestClient:
    Base.metadata.create_all(_shared_engine())
    _ensure_rule(session_factory)
    rule_version_id = str(_SHARED_RULE_UUID)

    llm = _ConfidenceLLM(confidence, source_sn)
    evidence = StubEvidence()
    app = create_app(
        session_factory=session_factory,
        llm=llm,
        evidence=evidence,
        iso_table=iso_table,
        inspect_repo=InspectionRepository(),
    )
    # Auto-confirm writes happen on a wired session; StaticPool in-memory
    # SQLite shares one connection so the commit is visible to the graph.
    evidence._on_confirm = [session_factory()]
    client = TestClient(app)
    client._env = {
        "rule_version_id": rule_version_id,
        "evidence": evidence,
        "session_factory": session_factory,
    }
    return client


def _post_batch(client: TestClient, planned_quantity: int = _LOT) -> dict:
    env = client._env
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": f"B-API-{uuid.uuid4().hex[:6]}",
            "project_id": _SEED["project_id"],
            "model": _SEED["model"],
            "rule_version_id": str(env["rule_version_id"]),
            "planned_quantity": planned_quantity,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _confirm(client: TestClient, task_id: str, action: str = "confirm") -> None:
    resp = client.post(f"/api/intake/{task_id}/confirm", json={"action": action})
    assert resp.status_code == 200, resp.text


@pytest.fixture()
def sqlite_env():
    engine = _shared_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory, factory


def test_create_batch(sqlite_env) -> None:
    """POST /api/batches -> 201; GET shows pending_intake (R5)."""
    s, sf = sqlite_env
    client = _make_client(
        0.9, "SN-I-CONF", [f"SN-I-{i:02d}" for i in range(_LOT)], s, sf, _ISO_TABLE
    )
    body = _post_batch(client)
    assert body["status"] == "pending_intake"

    detail = client.get(f"/api/batches/{body['batch_id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["status"] == "pending_intake"
    assert payload["planned_quantity"] == _LOT


def test_intake_triggers_graph(sqlite_env):
    """Q1: high confidence -> confirmed without interrupt; low -> pending."""
    s, sf = sqlite_env
    sns = [f"SN-H-{i:02d}" for i in range(_LOT)]
    client = _make_client(0.9, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    batch = _post_batch(client)

    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for(sn) for sn in sns]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == batch["batch_id"]  # Q2
    assert body["status"] == "confirmed"
    assert body["confidence"] == 0.9

    # Low-confidence twin: graph pauses at human_confirm -> pending.
    sns_l = [f"SN-L-{i:02d}" for i in range(_LOT)]
    client_l = _make_client(0.79, "SN-I-CONF", sns_l, s, sf, _ISO_TABLE)
    batch_l = _post_batch(client_l)
    resp_l = client_l.post(
        "/api/intake",
        json={"batch_id": batch_l["batch_id"], "inspections": [_r_for(sn) for sn in sns_l]},
    )
    assert resp_l.status_code == 200, resp_l.text
    assert resp_l.json()["status"] == "pending"
    assert set(resp_l.json()["low_conf_sns"]) == set(sns_l)


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


def test_confirm_resumes_graph(sqlite_env):
    """Q1/Q3: confirm resumes to END; batch -> pending_release; human_confirmation evidence."""
    s, sf = sqlite_env
    sns = [f"SN-C-{i:02d}" for i in range(_LOT)]
    client = _make_client(0.79, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    batch = _post_batch(client)
    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for(sn) for sn in sns]},
    )
    task_id = resp.json()["task_id"]
    _confirm(client, task_id, "confirm")

    env = client._env
    s2 = env["session_factory"]()
    b = s2.get(DeliveryBatch, uuid.UUID(batch["batch_id"]))
    assert b.status == "pending_release"
    confirmed = s2.scalars(
        select(ModuleInspection).where(
            ModuleInspection.batch_id == b.batch_id,
            ModuleInspection.intake_status == "confirmed",
        )
    ).all()
    assert len(confirmed) == _LOT

    detail = client.get(f"/api/batches/{batch['batch_id']}").json()
    assert detail["inspections"]
    assert all(c["intake_status"] == "confirmed" for c in detail["inspections"])
    assert detail["sample_result"] == "pass"
    assert detail["status_timeline"]

    types = [r.evidence_type for r in env["evidence"].rows]
    assert "human_confirmation" in types
    assert "decision_snapshot" in types
    assert "task_trace" in types


def test_confirm_reject_routes_end(sqlite_env):
    """Q3 reject: whole batch rejected; graph ends; no judge/sample."""
    s, sf = sqlite_env
    sns = [f"SN-RJ-{i:02d}" for i in range(_LOT)]
    client = _make_client(0.79, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    batch = _post_batch(client)
    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for(sn) for sn in sns]},
    )
    _confirm(client, resp.json()["task_id"], "reject")

    s2 = client._env["session_factory"]()
    b = s2.get(DeliveryBatch, uuid.UUID(batch["batch_id"]))
    rejected = s2.scalars(
        select(ModuleInspection).where(
            ModuleInspection.batch_id == b.batch_id,
            ModuleInspection.intake_status == "rejected",
        )
    ).all()
    assert len(rejected) == _LOT
    assert b.status == "pending_intake"  # rejected never leaves intake (spec §5.3)
    types = [r.evidence_type for r in client._env["evidence"].rows]
    assert "human_confirmation" in types
    assert "decision_snapshot" not in types


def test_judge_rejects_unconfirmed(sqlite_env):
    """Review Focus #3: judge before confirmation -> 400 + unconfirmed SN list."""
    s, sf = sqlite_env
    sns = [f"SN-U-{i:02d}" for i in range(_LOT)]
    client = _make_client(0.79, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    batch = _post_batch(client)
    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for(sn) for sn in sns]},
    )
    assert resp.json()["status"] == "pending"

    r = client.post(f"/api/batches/{batch['batch_id']}/judge")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert set(detail["unconfirmed_sns"]) == set(sns)


def test_judge_replays_skill(sqlite_env):
    """Q1: judge after full confirmation -> 200 JudgeResponse (skill, no graph)."""
    s, sf = sqlite_env
    sns = [f"SN-J-{i:02d}" for i in range(_LOT)]
    client = _make_client(0.9, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    batch = _post_batch(client)
    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for(sn) for sn in sns]},
    )
    _confirm(client, resp.json()["task_id"], "confirm")

    r = client.post(f"/api/batches/{batch['batch_id']}/judge")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "Pass"
    assert body["counts"]["Pass"] == _LOT
    assert body["fail_sns"] == []
    assert set(body["per_sn"]) == set(sns)


def test_judge_rule_not_effective_returns_400(sqlite_env):
    """R1: judge with a rule whose effective_at is LATER than the inspection
    captured_at -> stable 400 (not 500), reason 'rule not effective yet'."""
    s, sf = sqlite_env
    # Seed a second rule version whose effective_at is in the future (after the
    # inspection captured_at of 2026-10-01 in _r_for).
    s2 = sf()
    future_rule = QualityRuleVersion(
        project_id=_SEED["project_id"],
        model=_SEED["model"],
        name="JH-550W future-effective",
        nameplate_power=_SEED["nameplate_power"],
        power_tolerance=_SEED["power_tolerance"],
        parameter_standards=_SEED["parameter_standards"],
        current_bands=_SEED["current_bands"],
        sample_plan=_SEED["sample_plan"],
        effective_at=datetime(2027, 6, 1, tzinfo=UTC),  # after 2026-10-01
        status="published",
        version_no=99,
    )
    s2.add(future_rule)
    s2.flush()
    s2.commit()

    sns = [f"SN-NE-{i:02d}" for i in range(_LOT)]
    client = _make_client(0.9, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    client._env["rule_version_id"] = str(future_rule.rule_version_id)
    batch = _post_batch(client)
    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for(sn) for sn in sns]},
    )
    _confirm(client, resp.json()["task_id"], "confirm")

    r = client.post(f"/api/batches/{batch['batch_id']}/judge")
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "rule not effective yet"
    assert detail["rule_version_id"] == str(future_rule.rule_version_id)
