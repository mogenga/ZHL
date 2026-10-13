"""D6a auto-archive shared helper tests (T-3 Task 5).

The helper ``auto_archive_if_terminal(session, batch) -> bool`` runs the
state-machine transition to ``archived`` in the SAME transaction as the
caller's business write when the batch is in a terminal disposition
(released / rejected). D6a invariant: the archived edge MUST keep an empty
precondition so auto-archive always succeeds; the helper returns False
(and leaves the batch untouched) when the batch is not in a terminal
disposition or is already archived.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api._archival import auto_archive_if_terminal
from app.infra.models import Base, DeliveryBatch, QualityRuleVersion

_TS = None  # set in _seed
_RULE = UUID("bbbbbbbb-1111-2222-3333-444444444420")


def _batch(session: Any, status: str, batch_no: str = "B-ARCH") -> DeliveryBatch:
    from datetime import UTC, datetime

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
        effective_at=datetime(2026, 6, 1, tzinfo=UTC),
        version_no=1,
    )
    b = DeliveryBatch(
        batch_id=uuid.uuid4(),
        batch_no=batch_no,
        project_id="P-1",
        model="JH-550W",
        planned_quantity=10,
        rule_version_id=_RULE,
        status=status,
    )
    session.add_all([rule, b])
    session.commit()
    return b


@pytest.fixture()
def session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def test_released_auto_archives(session: Any) -> None:
    b = _batch(session, "released")
    assert auto_archive_if_terminal(session, b) is True
    assert b.status == "archived"
    session.commit()


def test_pending_release_not_archived(session: Any) -> None:
    b = _batch(session, "pending_release")
    assert auto_archive_if_terminal(session, b) is False
    assert b.status == "pending_release"


def test_already_archived_noop(session: Any) -> None:
    b = _batch(session, "archived")
    assert auto_archive_if_terminal(session, b) is False
    assert b.status == "archived"


def test_rejected_auto_archives(session: Any) -> None:
    b = _batch(session, "rejected")
    assert auto_archive_if_terminal(session, b) is True
    assert b.status == "archived"
