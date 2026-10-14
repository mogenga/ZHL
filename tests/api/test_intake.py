"""API tests for the intake routes (PA plan Task 8, §5.6 / Q1–Q3)."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from app.infra.models import Base
from tests.api.test_batches import _ISO_TABLE, _make_client


def test_confirm_unknown_task_returns_404() -> None:
    """B3: confirming an unknown task_id -> 404, not 500."""
    from tests.api.test_batches import _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-CONF", [], s, s, _ISO_TABLE)

    r = client.post(
        "/api/intake/00000000-0000-0000-0000-000000000000/confirm",
        json={"action": "confirm"},
    )
    assert r.status_code == 404


def test_intake_unknown_batch_returns_404() -> None:
    """B3: intake into an unknown batch -> 404."""
    from tests.api.test_batches import _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-CONF", [], s, s, _ISO_TABLE)

    r = client.post(
        "/api/intake",
        json={
            "batch_id": "00000000-0000-0000-0000-000000000000",
            "inspections": [],
        },
    )
    assert r.status_code == 404


def test_intake_partial_inspection_row_returns_400() -> None:
    """B3: a legacy partial row ({sn, source_type} only) must surface as a
    structured 400 naming the missing fields, not a KeyError -> 500 inside the
    graph (the original intake.js bug)."""
    from tests.api.test_batches import _r_for, _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-CONF", ["SN-N-001"], s, s, _ISO_TABLE)
    from tests.api.test_batches import _post_batch

    batch = _post_batch(client, planned_quantity=1)

    r = client.post(
        "/api/intake",
        json={
            "batch_id": batch["batch_id"],
            "inspections": [
                {"sn": "SN-N-001", "source_type": "IV_CSV"},  # legacy partial row
            ],
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["reason"] == "incomplete_inspection"
    # The full 9 columns the repo requires are named.
    for k in ("captured_at", "pmax", "temp", "isc", "voc", "vm", "im", "ff", "appearance_result"):
        assert k in detail["missing_fields"], k

    # A complete row (the fixed intake.js shape) succeeds and auto-confirms.
    ok = client.post(
        "/api/intake",
        json={
            "batch_id": batch["batch_id"],
            "inspections": [_r_for("SN-N-001")],
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "confirmed"


def test_intake_duplicate_sn_same_batch_returns_400() -> None:
    """A1: 同一批次里重复 SN 必须是结构化 400，而不是 DDL 唯一约束
    (batch_id, sn) 抛 IntegrityError -> 500。

    注：正常入口下"重复入场"现在会先被 B2 的状态守卫挡成 409（批次已离开
    ``pending_intake``，见 ``test_intake_past_intake_stage_returns_409``）。
    这条守的是更深一层：批次仍停在待入场、但库里已有该 SN 的行（例如上一次
    入场只写了一半就被回滚/中断留下的残留）。
    """
    import uuid as _uuid
    from datetime import UTC, datetime

    from sqlalchemy.orm import sessionmaker

    from app.infra.models import Base, DeliveryBatch, ModuleInspection
    from tests.api.test_batches import _ISO_TABLE, _make_client, _post_batch, _r_for, _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-CONF", ["SN-DUP-001"], s, s, _ISO_TABLE)

    batch = _post_batch(client, planned_quantity=2)
    row = _r_for("SN-DUP-001")  # dedicated prefix: no cross-case engine collision

    # 制造"批次仍在待入场、但该 SN 已在库"的残留状态。
    session = s()
    session.add(
        ModuleInspection(
            batch_id=_uuid.UUID(batch["batch_id"]),
            sn="SN-DUP-001",
            source_type="IV_CSV",
            captured_at=datetime(2026, 10, 1, tzinfo=UTC),
            pmax=550.0,
            temp=25.0,
            isc=10.5,
            voc=48.0,
            vm=40.0,
            im=13.0,
            ff=0.8,
            appearance_result="pass",
        )
    )
    session.commit()
    assert session.get(DeliveryBatch, _uuid.UUID(batch["batch_id"])).status == "pending_intake"
    session.close()

    r = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [row]},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "sn_already_intaked"
    assert "SN-DUP-001" in detail["duplicate_sns"]


# ---------------------------------------------------------------------------
# B2 回归：进场必须受状态机约束，且批次状态真的会被推进
# ---------------------------------------------------------------------------


def test_rejected_intake_keeps_batch_in_pending_intake() -> None:
    """拒绝入场后批次**必须**留在 ``pending_intake``，且不得进入判定。

    "rejected never leaves intake"：留在待入场，操作员才能改数据重提。同时
    这也是 B2 状态机的语义要求 —— 只有被接受（确认）的进场才允许走
    ``pending_intake -> pending_judge`` 这条边。
    """
    import uuid as _uuid

    from sqlalchemy.orm import sessionmaker

    from app.infra.models import Base, DeliveryBatch
    from tests.api.test_batches import _ISO_TABLE, _make_client, _post_batch, _r_for, _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    # 0.7 < 0.8：停在人工确认，由下面的 /confirm 决定去留
    client = _make_client(0.7, "SN-I-ADV", [], s, s, _ISO_TABLE)
    batch = _post_batch(client, planned_quantity=1)
    r = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for("SN-I-ADV")]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"

    rej = client.post(f"/api/intake/{batch['batch_id']}/confirm", json={"action": "reject"})
    assert rej.status_code == 200, rej.text

    session = s()
    assert session.get(DeliveryBatch, _uuid.UUID(batch["batch_id"])).status == "pending_intake"
    session.close()


def test_intake_past_intake_stage_returns_409() -> None:
    """已进过场的批次再 POST /api/intake 必须 409，且不写入任何新行。

    原实现只查「批次是否存在」：第二次调用会把图重跑一遍（同一 thread_id，
    图已完成 → 从 START 重跑，boundary state 还会继承上一轮的
    ``batch_status``），既可能重复写行，也可能用状态机拒绝的结果去强改
    批次状态。
    """
    from sqlalchemy.orm import sessionmaker

    from app.infra.models import Base, DeliveryBatch, ModuleInspection
    from tests.api.test_batches import _ISO_TABLE, _make_client, _post_batch, _r_for, _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-409", [], s, s, _ISO_TABLE)
    batch = _post_batch(client, planned_quantity=2)
    row = _r_for("SN-I-409")

    first = client.post("/api/intake", json={"batch_id": batch["batch_id"], "inspections": [row]})
    assert first.status_code == 200, first.text

    session = s()
    before_status = session.get(DeliveryBatch, __import__("uuid").UUID(batch["batch_id"])).status
    before_rows = session.query(ModuleInspection).count()

    again = client.post("/api/intake", json={"batch_id": batch["batch_id"], "inspections": [row]})
    assert again.status_code == 409, again.text
    assert "pending_intake" in str(again.json()["detail"])

    session.expire_all()
    assert session.get(DeliveryBatch, __import__("uuid").UUID(batch["batch_id"])).status == (
        before_status
    )
    assert session.query(ModuleInspection).count() == before_rows
    session.close()


def test_intake_on_archived_batch_returns_409() -> None:
    """归档是终态：不得再入场（D8 守卫在 intake 端点同样要生效）。"""
    import uuid as _uuid

    from sqlalchemy.orm import sessionmaker

    from app.infra.models import Base, DeliveryBatch
    from tests.api.test_batches import _ISO_TABLE, _make_client, _post_batch, _r_for, _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.9, "SN-I-ARCH", [], s, s, _ISO_TABLE)
    batch = _post_batch(client, planned_quantity=1)

    session = s()
    session.get(DeliveryBatch, _uuid.UUID(batch["batch_id"])).status = "archived"
    session.commit()
    session.close()

    r = client.post(
        "/api/intake", json={"batch_id": batch["batch_id"], "inspections": [_r_for("SN-I-ARCH")]}
    )
    assert r.status_code == 409, r.text


def test_repeated_confirm_is_409_not_a_chain_rerun() -> None:
    """确认只能生效一次；再调必须 409。

    原实现没有状态守卫：第二次 /confirm 会在同一 ``thread_id`` 上重新
    ``stream``，此时该线程已无待确认的中断点，图会从 START 重跑整条链 ——
    重复写行、并让批次状态被重算一次。
    """
    import uuid as _uuid

    from sqlalchemy.orm import sessionmaker

    from app.infra.models import Base, DeliveryBatch, ModuleInspection
    from tests.api.test_batches import _ISO_TABLE, _make_client, _post_batch, _r_for, _shared_engine

    engine = _shared_engine()
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)
    client = _make_client(0.7, "SN-I-TWICE", [], s, s, _ISO_TABLE)
    batch = _post_batch(client, planned_quantity=1)
    first = client.post(
        "/api/intake",
        json={"batch_id": batch["batch_id"], "inspections": [_r_for("SN-I-TWICE")]},
    )
    assert first.status_code == 200 and first.json()["status"] == "pending"

    ok = client.post(f"/api/intake/{batch['batch_id']}/confirm", json={"action": "confirm"})
    assert ok.status_code == 200, ok.text

    session = s()
    status_after = session.get(DeliveryBatch, _uuid.UUID(batch["batch_id"])).status
    rows_after = session.query(ModuleInspection).count()

    again = client.post(f"/api/intake/{batch['batch_id']}/confirm", json={"action": "confirm"})
    assert again.status_code == 409, again.text

    session.expire_all()
    assert session.get(DeliveryBatch, _uuid.UUID(batch["batch_id"])).status == status_after
    assert session.query(ModuleInspection).count() == rows_after
    session.close()
