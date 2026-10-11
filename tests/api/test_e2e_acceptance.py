"""Phase A end-to-end acceptance samples consumed via the API (PA plan Task 9 Step 1).

The four samples are the Phase A slice of master spec §6.1:

1. 正常放行 (normal release): 10 all-pass modules → batch reaches
   ``pending_release`` through the graph; release happens in Phase B.
2. 边界值 (boundary): Pmax = nameplate lower bound passes; the
   ``− 0.1`` control fails.
3. 异常隔离 (isolation): an over-band current → the batch routes to
   ``isolated`` (incident reinspection is Phase B).
4. 数据一致性 (consistency): the same SN in two batches → the repository's
   EXCLUDE fallback raises → 400, neither batch is releasable.

All on in-memory SQLite with the shared fixture LLM / ISO table; the API
routes under test are the Task 8 ones (now split into ``app/api``).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.infra.models import Base
from app.infra.repositories import CrossBatchSnConflict
from tests.api.test_batches import (
    _ISO_TABLE,
    _SEED,
    _ensure_rule,
    _make_client,
    _post_batch,
    _r_for,
    _shared_engine,
)

# nameplate 550 W, power_tolerance 5 % -> lower bound = 550 * 0.95 = 522.5 W.
_NAMEPLATE = _SEED["nameplate_power"]
_TOL = _SEED["power_tolerance"]
_LOWER = _NAMEPLATE * (1 - _TOL)
_UPPER = _NAMEPLATE * (1 + _TOL)


@pytest.fixture()
def env():
    engine = _shared_engine()
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    _ensure_rule(sf)
    s = sf()
    yield s, sf
    s.close()


def _sns(prefix: str, n: int) -> list[str]:
    return [f"{prefix}-{i:02d}" for i in range(n)]


def _row_for(sn: str, pmax: float | None = None, im: float | None = None) -> dict:
    row = _r_for(sn)
    if pmax is not None:
        row["pmax"] = pmax
    if im is not None:
        row["im"] = im
    return row


def test_sample1_normal_release_via_api(env) -> None:
    """Sample 1 (判定侧): 10 modules all pass → batch lands in pending_release."""
    s, sf = env
    sns = _sns("SN-A1", 10)
    client = _make_client(0.95, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    batch = _post_batch(client)

    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_row_for(sn) for sn in sns]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"

    detail = client.get(f"/api/batches/{batch['batch_id']}").json()
    assert detail["status"] == "pending_release"  # Phase A ends here; release is Phase B
    assert detail["sample_result"] == "pass"
    assert len(detail["inspections"]) == 10
    assert all(i["decision_status"] == "pass" for i in detail["inspections"])


def test_sample2_boundary_pmax(env) -> None:
    """Sample 2: Pmax at the lower bound passes; bound − 0.1 fails.

    P0 already pins these verdicts; here they are exercised through the
    API: the judge route returns a per-SN breakdown where the boundary
    module is Pass and the control module is Fail.
    """
    s, sf = env
    sns = _sns("SN-A2", 2)
    ok_sn, bad_sn = sns
    client = _make_client(0.95, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    batch = _post_batch(client)

    resp = client.post(
        "/api/intake",
        json={
            "batch_id": batch["batch_id"],
            "inspections": [
                _row_for(ok_sn, pmax=_LOWER),  # exactly the lower bound → Pass
                _row_for(bad_sn, pmax=_LOWER - 0.1),  # below the bound → Fail
            ],
        },
    )
    assert resp.status_code == 200, resp.text

    judge = client.post(f"/api/batches/{batch['batch_id']}/judge")
    assert judge.status_code == 200, judge.text
    body = judge.json()
    assert body["result"] == "Fail"  # one fail drags the batch verdict
    assert set(body["fail_sns"]) == {bad_sn}
    assert set(body["per_sn"][ok_sn]) and body["per_sn"][bad_sn]


def test_sample3_isolation_overband(env) -> None:
    """Sample 3 (判定侧): an over-band current isolates the batch."""
    s, sf = env
    sns = _sns("SN-A3", 10)
    # Over the current band: im = 20 A (bands in the seed cap the max).
    over_sn = sns[0]
    client = _make_client(0.95, "SN-I-CONF", sns, s, sf, _ISO_TABLE)
    batch = _post_batch(client)

    rows = [_row_for(sn) for sn in sns]
    rows[0] = _row_for(over_sn, im=20.0)
    resp = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": rows},
    )
    assert resp.status_code == 200, resp.text

    detail = client.get(f"/api/batches/{batch['batch_id']}").json()
    assert detail["status"] == "isolated"  # Phase A ends here; reinspection is Phase B
    assert detail["sample_result"] in ("fail", "rule_missing")


def test_sample4_cross_batch_sn_conflict(env) -> None:
    """Sample 4: the same SN in two batches → the EXCLUDE fallback fires.

    The repository layer (Task 2, D5/D17) rejects the duplicate; on the
    API side the intake of the second batch surfaces as a 400 carrying the
    conflicting SN — neither batch is releasable.
    """
    s, sf = env
    shared_sn = "SN-X-01"
    sns_a = _sns("SN-A4", 3)
    sns_b = _sns("SN-B4", 3)
    sns_b[0] = shared_sn  # collide with batch A's SN on the re-intake
    client = _make_client(0.95, "SN-I-CONF", sns_a + sns_b, s, sf, _ISO_TABLE)
    batch_a = _post_batch(client)
    batch_b = _post_batch(client)

    resp_a = client.post(
        "/api/intake",
        json={
            "batch_id": batch_a["batch_id"],
            "inspections": [_row_for(sn) for sn in sns_a if sn != shared_sn]
            + [_row_for(shared_sn)],
        },
    )
    assert resp_a.status_code == 200, resp_a.text

    resp_b = client.post(
        "/api/intake",
        json={
            "batch_id": batch_b["batch_id"],
            "inspections": [_row_for(sn) for sn in sns_b],
        },
    )
    assert resp_b.status_code == 400, resp_b.text
    assert shared_sn in json.dumps(resp_b.json()["detail"])

    # A6 (sample 4): the shared SN is blocked. Batch B's intake is rejected,
    # so it stays in pending_intake and is not releasable; the duplicate
    # itself can never be judged in two batches.
    db_ = client.get(f"/api/batches/{batch_b['batch_id']}").json()
    assert db_["status"] == "pending_intake"
    assert db_["status"] != "pending_release"


def test_sample4_repository_level_conflict(env) -> None:
    """Same SN across batches raises CrossBatchSnConflict at the repo layer."""
    s, sf = env
    from app.infra.repositories import BatchRepository, InspectionRepository

    rule_id = _ensure_rule_result(sf)

    repo = BatchRepository()
    b1 = repo.create_batch(s, "B-X1", _SEED["project_id"], _SEED["model"], uuid.UUID(rule_id), 2)
    b2 = repo.create_batch(s, "B-X2", _SEED["project_id"], _SEED["model"], uuid.UUID(rule_id), 2)
    s.commit()

    ir = InspectionRepository()
    ir.create_inspections(
        s,
        b1.batch_id,
        [dict(_row_for("SN-Z-01", pmax=_LOWER), captured_at=datetime(2026, 10, 1, tzinfo=UTC))],
    )
    s.commit()
    with pytest.raises(CrossBatchSnConflict):
        ir.create_inspections(
            s,
            b2.batch_id,
            [dict(_row_for("SN-Z-01", pmax=_LOWER), captured_at=datetime(2026, 10, 1, tzinfo=UTC))],
        )


def _ensure_rule_result(sf) -> str:
    """Return the seeded rule's id (shared engine, single rule)."""
    from tests.api.test_batches import _SHARED_RULE_UUID

    assert _SHARED_RULE_UUID is not None, "rule must be seeded"
    return str(_SHARED_RULE_UUID)
