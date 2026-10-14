# 规则版本管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `QualityRuleVersion` 补齐写路径与变更管控——创建 draft / 发布 / 停用 + 全生命周期审计留痕，停用后新批次不可再选用该版本。

**Architecture:** 状态机走 `app/domain/rule_version.py` 纯函数（对齐 `state_machine.py` 的表驱动风格，无 DB import）；审计事件落新表 `rule_version_events`（迁移 003）+ 新模型 `RuleVersionEvent`；API 扩 `app/api/rule_versions.py` 四端点（沿用该模块现有 `get_session` 依赖注入模式，非 Phase A `AppDeps` 闭包）；`batches.py::create_batch` 加 2 行 rule.status 校验。AI 推荐仅留 `source`/`recommendation_ref` 字段位，不实现。

**Tech Stack:** FastAPI + Pydantic v2 + SQLAlchemy 2.0 + PostgreSQL 15 + pytest + uv（ruff/black 门禁）。

**Spec:** `docs/superpowers/specs/2026-10-14-rule-version-management-design.md`（实现时两文档同读）

## Global Constraints

- AGENT.md §4.2：能用声明式约束表达的不写触发器；规则版本不可变已由 `001_init.sql` 的 `fn_rule_version_guard` 触发器落地，本计划**不新增触发器**（published 只允许 status 单列变更已由它保证）
- AGENT.md §8 红线：禁止修改已发布规则版本的字段/内容——本计划只写 `created/published/disabled` 事件 + status 单列变更，不碰其他列
- 冻结层（AGENT.md §6.1）：`app/infra/models.py` 只**纯新增** `RuleVersionEvent` 模型 + `RULE_VERSION_ACTION` tuple（不动既有类/列，dev-notes 标注）；`app/domain/rule_version.py` 为新文件（纯函数）
- 迁移按 `NNN_description.sql` 编号；本计划为 `003_rule_version_events.sql`（002 已有两个：`002_add_reinspection_scope.sql` + `002_intake_status_isolated.sql`）；仅 `001`/`002_reinspection_scope` 已 alembic 化，`003` 先落 SQL 文件（alembic revision 可选，跟随既有惯例——`migrations/*.sql` 为主，`alembic/versions/*.py` 按需补）
- `uv run` 跑一切 Python 命令；门禁 `uv run ruff check . && uv run black --check . && uv run pytest -x`
- 提交规范：conventional commits，dev-notes 过程留痕（`dev-notes/phase-<x>/` 或本功能新建 `dev-notes/rule-version-mgmt/`）

## Review Focus

1. **published 规则被误选用于新批次**（停用/发布后）——`create_batch` 现只查"存在"，本计划加 status 校验；期望 400 `rule version not published`。→ Task 5 测试钉住
2. **幂等 create 重复调用**（同 project+model+version_no）——期望 200 + `duplicate:True` + 已存在 ID，**不写新 event**、不 500。→ Task 4 测试钉住
3. **非法状态转移**（draft→disabled、disabled→published、published→published）——期望 409 + 明确 reason；DB 触发器兜底防字段改。→ Task 3/4 测试钉住
4. **停用后在途批次复算性**——已落 `rule_snapshot` 的批次判定不重读 status；期望 disable 不影响既有 `judge_batch`。→ Task 5 测试钉住（disable 一个 rule 后，用它判过的批次 re-judge 仍按快照通过）
5. **audit 事件时间序**（events 端点时间倒序、多事件不丢失）——期望 `GET /events` 倒序且全量。→ Task 4 测试钉住

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `migrations/003_rule_version_events.sql` | `rule_version_events` 表 + `rule_version_action` enum + 索引 + 授权 | 新增 |
| `app/infra/models.py` | `RuleVersionEvent` 模型 + `RULE_VERSION_ACTION` tuple + `__all__` 加 2 项 | 纯新增改动 |
| `app/domain/rule_version.py` | `RULE_LIFECYCLE` 表 + `can_transition` + `transition_rule_status` 纯函数 | 新增 |
| `app/api/rule_versions.py` | 扩 `create_rule_version` / `publish_rule_version` / `disable_rule_version` / `list_rule_version_events` 四端点 + Pydantic 请求/响应模型 | 扩展 |
| `app/api/batches.py` | `create_batch` 加 `rule.status == "published"` 校验（400） | 改（唯一改既有 API 逻辑处）|
| `tests/domain/test_rule_version.py` | 状态机全组合 | 新增 |
| `tests/api/test_rule_version_mgmt.py` | create/publish/disable/events + 停用选用边界 + AI 留口 | 新增 |
| `tests/api/test_batches_rule_status.py` | create_batch rule status 校验 | 新增 |

---

### Task 1: 迁移 003 + RuleVersionEvent 模型

**Files:**
- Create: `migrations/003_rule_version_events.sql`
- Modify: `app/infra/models.py`（纯新增：`RULE_VERSION_ACTION` tuple 加到 enum 值列表区；`RuleVersionEvent` 模型加到文件尾部 `Report` 之后；`__all__` 加 `"RuleVersionEvent"` + `"RULE_VERSION_ACTION"`）
- Test: `tests/infra/test_rule_version_event_model.py`（新增）

**Interfaces:**
- Consumes: 无（Task 1 自含）
- Produces:
  - `RuleVersionEvent` 模型：字段 `event_id: uuid.UUID`（PK）、`rule_version_id: uuid.UUID`（FK→quality_rule_versions）、`action: str`（`RULE_VERSION_ACTION` 枚举值）、`operator: str`、`detail: dict`（JSONB）、`created_at: datetime`
  - `RULE_VERSION_ACTION = ("created", "published", "disabled")`

- [ ] **Step 1: 写失败测试**

`tests/infra/test_rule_version_event_model.py`：

```python
"""RuleVersionEvent model + migration contract."""
from uuid import UUID
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.infra.models import Base, RuleVersionEvent, QualityRuleVersion, RULE_VERSION_ACTION


def test_rule_version_action_values() -> None:
    assert RULE_VERSION_ACTION == ("created", "published", "disabled")


def test_rule_version_event_roundtrip_sqlite() -> None:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    sm = sessionmaker(bind=eng, future=True)
    s = sm()
    rule = QualityRuleVersion(
        project_id="P1", model="JH-550W", name="v1",
        nameplate_power=550.0, power_tolerance=0.03,
        parameter_standards={}, current_bands={}, sample_plan={},
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="draft", version_no=1,
    )
    s.add(rule); s.flush()
    ev = RuleVersionEvent(
        rule_version_id=rule.rule_version_id,
        action="created", operator="qa",
        detail={"version_no": 1},
    )
    s.add(ev); s.commit()
    got = s.query(RuleVersionEvent).one()
    assert got.action == "created"
    assert got.operator == "qa"
    assert got.detail == {"version_no": 1}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/infra/test_rule_version_event_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'RuleVersionEvent'`

- [ ] **Step 3: 写迁移 003 SQL + models.py 改动**

`migrations/003_rule_version_events.sql`（注意：`CREATE TYPE` 须先于 `CREATE TABLE`，与 001 风格一致）：

```sql
-- ============================================================
-- ZHL 规则版本管理 · 审计事件表 DDL（spec 2026-10-14-rule-version-management）
-- 影响范围：新增 rule_version_events 表 + rule_version_action 枚举
-- 回滚方案：DROP TABLE rule_version_events; DROP TYPE rule_version_action;
-- ============================================================
BEGIN;

CREATE TYPE rule_version_action AS ENUM ('created','published','disabled');

CREATE TABLE rule_version_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_version_id UUID NOT NULL REFERENCES quality_rule_versions(rule_version_id),
    action          rule_version_action NOT NULL,
    operator        text NOT NULL,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_rule_events_rule ON rule_version_events(rule_version_id, created_at);

-- 授权（对齐 001_init.sql:320 GRANT 块）
GRANT SELECT, INSERT ON rule_version_events TO zhl_app;

COMMIT;
```

`app/infra/models.py` 改动：
1. enum 值列表区（`RULE_STATUS` 附近）加：
```python
RULE_VERSION_ACTION = ("created", "published", "disabled")
```
2. 文件尾部 `Report` 类之后、`__all__` 之前加：
```python
# 5.1.13 rule_version_events（规则版本管理审计）
class RuleVersionEvent(Base):
    __tablename__ = "rule_version_events"
    event_id: Mapped[uuid.UUID] = _uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_rule_versions.rule_version_id"), nullable=False
    )
    action: Mapped[str] = _enum(RULE_VERSION_ACTION)
    operator: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB_T, nullable=False, default=dict)
    created_at: Mapped[datetime] = _ts()
```
3. `__all__` 列表加 `"RuleVersionEvent"` + `"RULE_VERSION_ACTION"`

- [ ] **Step 4: 跑测试确认通过 + 迁移可独立执行**

Run: `uv run pytest tests/infra/test_rule_version_event_model.py -v`
Expected: PASS

Run: `docker exec zhl-pg psql -U postgres -d zhl -c "\i /dev/stdin" < migrations/003_rule_version_events.sql` 或 `docker exec -i zhl-pg psql -U postgres -d zhl < migrations/003_rule_version_events.sql`
Expected: 无错误；`SELECT * FROM rule_version_events;` 返回空表（表已建）

- [ ] **Step 5: 全量门禁 + 提交**

Run: `uv run ruff check . && uv run black --check . && uv run pytest -x`
Expected: 全绿（既有测试不受纯新增影响）

```bash
git add migrations/003_rule_version_events.sql app/infra/models.py tests/infra/test_rule_version_event_model.py dev-notes/
git commit -m "feat(rules): migration 003 + RuleVersionEvent model (rule-version mgmt audit)

- 003_rule_version_events.sql: rule_version_action enum + 表 + 索引 + GRANT
- models.py: RuleVersionEvent 模型 + RULE_VERSION_ACTION（纯新增，不动既有类）
- 冻结层：models.py 纯新增，dev-notes 标注"
```

---

### Task 2: domain 状态机纯函数

**Files:**
- Create: `app/domain/rule_version.py`
- Test: `tests/domain/test_rule_version.py`

**Interfaces:**
- Consumes: 无（纯函数，仅 import 自包含）
- Produces:
  - `RULE_LIFECYCLE: dict[str, frozenset[str]]` — 状态转移表
  - `can_transition(from_status: str, to_status: str) -> bool`
  - `transition_rule_status(from_status: str, to_status: str) -> tuple[str | None, str | None]` — 返回 `(new_status, error_reason)`；合法返回 `(to_status, None)`，非法返回 `(None, "cannot transition from {from} to {to}")`

- [ ] **Step 1: 写失败测试**

`tests/domain/test_rule_version.py`：

```python
"""Rule version lifecycle state machine (pure)."""
from app.domain.rule_version import RULE_LIFECYCLE, can_transition, transition_rule_status


def test_lifecycle_table_shape() -> None:
    assert RULE_LIFECYCLE["draft"] == frozenset({"published"})
    assert RULE_LIFECYCLE["published"] == frozenset({"disabled"})
    assert RULE_LIFECYCLE["disabled"] == frozenset()


def test_legal_transitions() -> None:
    assert can_transition("draft", "published")
    assert can_transition("published", "disabled")
    assert not can_transition("draft", "disabled")
    assert not can_transition("disabled", "published")
    assert not can_transition("published", "published")


def test_transition_rule_status_legal() -> None:
    assert transition_rule_status("draft", "published") == ("published", None)
    assert transition_rule_status("published", "disabled") == ("disabled", None)


def test_transition_rule_status_illegal() -> None:
    new, reason = transition_rule_status("disabled", "published")
    assert new is None
    assert "cannot transition" in reason
    new2, reason2 = transition_rule_status("draft", "disabled")
    assert new2 is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/domain/test_rule_version.py -v`
Expected: FAIL — `ModuleNotFoundError: app.domain.rule_version`

- [ ] **Step 3: 实现 `app/domain/rule_version.py`**

签名与测试一致；对齐 `state_machine.py` 表驱动风格（数据表 + 纯函数，无 DB/infra import）：

```python
"""Rule version lifecycle state machine (pure, no DB import).

Transition table: draft -> published -> disabled (terminal). 不可逆，无回滚
（回到旧口径=发新 version_no）。对齐 AGENT.md §8 红线"修改已发布规则版本"。
"""
from __future__ import annotations

RULE_LIFECYCLE: dict[str, frozenset] = {
    "draft": frozenset({"published"}),
    "published": frozenset({"disabled"}),
    "disabled": frozenset(),  # 终态
}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in RULE_LIFECYCLE.get(from_status, frozenset())


def transition_rule_status(from_status: str, to_status: str) -> tuple[str | None, str | None]:
    if can_transition(from_status, to_status):
        return to_status, None
    return None, f"cannot transition from {from_status} to {to_status}"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/domain/test_rule_version.py -v`
Expected: 全 PASS

- [ ] **Step 5: 全量门禁 + 提交**

Run: `uv run ruff check . && uv run black --check . && uv run pytest -x`

```bash
git add app/domain/rule_version.py tests/domain/test_rule_version.py dev-notes/
git commit -m "feat(rules): domain rule version lifecycle state machine (pure functions)"
```

---

### Task 3: create_rule_version 端点（幂等）

**Files:**
- Modify: `app/api/rule_versions.py`（扩：`CreateRuleVersionRequest` / `CreateRuleVersionResponse` Pydantic 模型 + `POST ""` 端点 + `_write_event` 辅助）
- Test: `tests/api/test_rule_version_mgmt.py`（新建，先写 create 部分）

**Interfaces:**
- Consumes: `RuleVersionEvent`（Task 1）、现有 `get_session`/`SessionDep`（rule_versions.py 既有）、`QualityRuleVersion`（既有）
- Produces:
  - `CreateRuleVersionRequest`（Pydantic）：`project_id, model, name, nameplate_power: float, power_tolerance: float, parameter_standards: dict, current_bands: dict, sample_plan: dict, effective_at: datetime, version_no: int, source: Literal["manual","recommended"]="manual", recommendation_ref: dict|None=None`
  - `CreateRuleVersionResponse`：`rule_version_id: uuid.UUID, status: str, version_no: int, duplicate: bool, event_id: uuid.UUID|None`
  - `POST /api/quality-rule-versions` → 200/201：新建=201 + `duplicate:False` + 新 event_id；幂等命中=200 + `duplicate:True` + 已存在 ID + `event_id:None`

- [ ] **Step 1: 写失败测试（create 部分）**

`tests/api/test_rule_version_mgmt.py` 头部 + create 用例（沿用 `test_main_smoke.py` 的 SQLite 注入模式 + rule_versions router 的 `get_session` override）：

```python
"""Rule version management API tests (create/publish/disable/events)."""
from __future__ import annotations
from datetime import UTC, datetime
from uuid import UUID, uuid4
from typing import Any
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.infra.models import Base, QualityRuleVersion, RuleVersionEvent


def _make_app(session: Any) -> TestClient:
    from app.api import rule_versions
    app = create_app(session_factory=lambda: session, mount_static=False)
    app.dependency_overrides[rule_versions.get_session] = lambda: session
    return TestClient(app)


def _seed_published_rule(session: Any) -> QualityRuleVersion:
    r = QualityRuleVersion(
        project_id="P1", model="M1", name="n", nameplate_power=550.0,
        power_tolerance=0.03, parameter_standards={}, current_bands={},
        sample_plan={}, effective_at=datetime(2026,1,1,tzinfo=UTC),
        status="draft", version_no=1,
    )
    session.add(r); session.commit()
    return r


def test_create_rule_version_new() -> None:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, future=True)()
    client = _make_app(session)
    body = {
        "project_id": "P2", "model": "M2", "name": "v1",
        "nameplate_power": 400.0, "power_tolerance": 0.05,
        "parameter_standards": {}, "current_bands": {}, "sample_plan": {},
        "effective_at": "2026-02-01T00:00:00Z", "version_no": 1,
    }
    resp = client.post("/api/quality-rule-versions", json=body)
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert data["duplicate"] is False
    assert data["status"] == "draft"
    assert data["event_id"] is not None
    # event 已落库
    ev = session.query(RuleVersionEvent).filter_by(action="created").one()
    assert ev.rule_version_id == UUID(data["rule_version_id"])
    session.close()


def test_create_rule_version_idempotent_duplicate() -> None:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, future=True)()
    existing = _seed_published_rule(session)
    client = _make_app(session)
    body = {
        "project_id": "P1", "model": "M1", "name": "n",
        "nameplate_power": 550.0, "power_tolerance": 0.03,
        "parameter_standards": {}, "current_bands": {}, "sample_plan": {},
        "effective_at": "2026-01-01T00:00:00Z", "version_no": 1,
    }
    resp = client.post("/api/quality-rule-versions", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is True
    assert data["rule_version_id"] == str(existing.rule_version_id)
    assert data["event_id"] is None
    # 幂等重复不写新 event
    assert session.query(RuleVersionEvent).count() == 0
    session.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/api/test_rule_version_mgmt.py -v`
Expected: FAIL — 404（端点不存在）

- [ ] **Step 3: 实现 create 端点**

`app/api/rule_versions.py` 加：

```python
from datetime import datetime
from uuid import UUID, uuid4
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy.exc import IntegrityError
from app.infra.models import RuleVersionEvent


class CreateRuleVersionRequest(BaseModel):
    project_id: str
    model: str
    name: str
    nameplate_power: float
    power_tolerance: float
    parameter_standards: dict
    current_bands: dict
    sample_plan: dict
    effective_at: datetime
    version_no: int
    source: Literal["manual", "recommended"] = "manual"
    recommendation_ref: dict | None = None


class CreateRuleVersionResponse(BaseModel):
    rule_version_id: uuid.UUID
    status: str
    version_no: int
    duplicate: bool
    event_id: uuid.UUID | None


@router.post("", response_model=CreateRuleVersionResponse, status_code=201)
def create_rule_version(body: CreateRuleVersionRequest, session: SessionDep) -> CreateRuleVersionResponse:
    """建 draft 规则版本（幂等：同 project+model+version_no 已存在 → 200 duplicate）。"""
    from sqlalchemy import select
    # 幂等查重
    existing = session.scalar(
        select(QualityRuleVersion).where(
            QualityRuleVersion.project_id == body.project_id,
            QualityRuleVersion.model == body.model,
            QualityRuleVersion.version_no == body.version_no,
        )
    )
    if existing is not None:
        session.commit()
        return CreateRuleVersionResponse(
            rule_version_id=existing.rule_version_id,
            status=existing.status,
            version_no=existing.version_no,
            duplicate=True,
            event_id=None,
        )
    rule = QualityRuleVersion(
        rule_version_id=uuid4(),
        project_id=body.project_id, model=body.model, name=body.name,
        nameplate_power=body.nameplate_power, power_tolerance=body.power_tolerance,
        parameter_standards=body.parameter_standards, current_bands=body.current_bands,
        sample_plan=body.sample_plan, effective_at=body.effective_at,
        status="draft", version_no=body.version_no,
    )
    session.add(rule)
    try:
        session.flush()  # 触发 UNIQUE 约束（竞态兜底）
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(QualityRuleVersion).where(
                QualityRuleVersion.project_id == body.project_id,
                QualityRuleVersion.model == body.model,
                QualityRuleVersion.version_no == body.version_no,
            )
        )
        return CreateRuleVersionResponse(
            rule_version_id=existing.rule_version_id, status=existing.status,
            version_no=existing.version_no, duplicate=True, event_id=None,
        )
    ev = RuleVersionEvent(
        event_id=uuid4(),
        rule_version_id=rule.rule_version_id,
        action="created",
        operator=body.source,  # 创建无独立 operator 字段；用 source 标记来源（manual/recommended）
        detail={"version_no": body.version_no, "source": body.source,
                "recommendation_ref": body.recommendation_ref},
    )
    session.add(ev)
    session.commit()
    session.refresh(rule, ev)
    return CreateRuleVersionResponse(
        rule_version_id=rule.rule_version_id, status=rule.status,
        version_no=rule.version_no, duplicate=False, event_id=ev.event_id,
    )
```

> 注：create 请求体 spec §2.1 未含 `operator` 字段（创建即来源标记），故 event.operator 用 `source`。若需独立 operator，spec §2.1 `CreateRuleVersionRequest` 需加 `operator: str`——本计划按 spec 现状（无 operator）落地；如 builder 认为需 operator，改 spec §2.1 加字段后再实现。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/api/test_rule_version_mgmt.py -v`
Expected: create 两用例 PASS

- [ ] **Step 5: 全量门禁 + 提交**

Run: `uv run ruff check . && uv run black --check . && uv run pytest -x`

```bash
git add app/api/rule_versions.py tests/api/test_rule_version_mgmt.py dev-notes/
git commit -m "feat(rules): POST create rule version (idempotent draft + audit event)"
```

---

### Task 4: publish + disable + events 端点

**Files:**
- Modify: `app/api/rule_versions.py`（扩 `StatusChangeRequest/Response` + `publish`/`disable`/`events` 三端点）
- Test: `tests/api/test_rule_version_mgmt.py`（补 publish/disable/events 用例）

**Interfaces:**
- Consumes: `transition_rule_status`/`can_transition`（Task 2）、`RuleVersionEvent`（Task 1）、`create_rule_version` 模式（Task 3）
- Produces:
  - `POST /api/quality-rule-versions/{id}/publish` body `{operator, source?}` → `StatusChangeResponse{rule_version_id, status, event_id}`；非法 409
  - `POST /api/quality-rule-versions/{id}/disable` body `{operator, reason?}` → 同上；非法 409
  - `GET /api/quality-rule-versions/{id}/events` → `list[RuleVersionEventResponse]`（时间倒序）

- [ ] **Step 1: 写失败测试（publish/disable/events 部分）**

`tests/api/test_rule_version_mgmt.py` 追加：

```python
def test_publish_draft_to_published() -> None:
    # 建 draft → publish → 验证 status=published + event
    ...（建 draft 用 create 端点或直接 seed draft rule）
    resp = client.post(f"/api/quality-rule-versions/{rid}/publish", json={"operator": "qa"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"
    assert session.query(RuleVersionEvent).filter_by(action="published").count() == 1


def test_publish_from_disabled_409() -> None:
    # seed disabled rule → publish → 409
    ...
    assert resp.status_code == 409


def test_publish_from_published_409() -> None:
    # seed published → publish → 409（幂等不可重复发布）


def test_disable_published_to_disabled() -> None:
    ...
    assert resp.json()["status"] == "disabled"
    assert session.query(RuleVersionEvent).filter_by(action="disabled").count() == 1


def test_disable_from_draft_409() -> None:
    # draft 不能直接 disable（须先 publish）


def test_events_list_desc() -> None:
    # 一 rule 走 created→published→disabled 全生命周期 → GET events 倒序 3 条
    events = client.get(f"/api/quality-rule-versions/{rid}/events").json()
    assert [e["action"] for e in events] == ["disabled", "published", "created"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/api/test_rule_version_mgmt.py -v`
Expected: 新用例 FAIL（404）

- [ ] **Step 3: 实现 publish/disable/events**

`app/api/rule_versions.py` 加：

```python
class StatusChangeRequest(BaseModel):
    operator: str
    source: Literal["manual", "recommended"] = "manual"
    reason: str | None = None


class StatusChangeResponse(BaseModel):
    rule_version_id: uuid.UUID
    status: str
    event_id: uuid.UUID


class RuleVersionEventResponse(BaseModel):
    event_id: uuid.UUID
    action: str
    operator: str
    detail: dict
    created_at: datetime


def _load_rule_or_404(session, rule_version_id) -> QualityRuleVersion:
    r = session.get(QualityRuleVersion, rule_version_id)
    if r is None:
        raise HTTPException(status_code=404, detail="rule version not found")
    return r


@router.post("/{rule_version_id}/publish", response_model=StatusChangeResponse)
def publish_rule_version(rule_version_id: uuid.UUID, body: StatusChangeRequest, session: SessionDep) -> StatusChangeResponse:
    r = _load_rule_or_404(session, rule_version_id)
    new_status, err = transition_rule_status(r.status, "published")
    if new_status is None:
        raise HTTPException(status_code=409, detail={"error": "cannot_publish", "reason": err, "from": r.status})
    r.status = new_status
    ev = RuleVersionEvent(event_id=uuid4(), rule_version_id=r.rule_version_id,
                          action="published", operator=body.operator,
                          detail={"version_no": r.version_no, "effective_at": r.effective_at.isoformat(),
                                  "source": body.source})
    session.add(ev); session.commit(); session.refresh(r, ev)
    return StatusChangeResponse(rule_version_id=r.rule_version_id, status=r.status, event_id=ev.event_id)


@router.post("/{rule_version_id}/disable", response_model=StatusChangeResponse)
def disable_rule_version(rule_version_id: uuid.UUID, body: StatusChangeRequest, session: SessionDep) -> StatusChangeResponse:
    r = _load_rule_or_404(session, rule_version_id)
    new_status, err = transition_rule_status(r.status, "disabled")
    if new_status is None:
        raise HTTPException(status_code=409, detail={"error": "cannot_disable", "reason": err, "from": r.status})
    r.status = new_status
    ev = RuleVersionEvent(event_id=uuid4(), rule_version_id=r.rule_version_id,
                          action="disabled", operator=body.operator,
                          detail={"reason": body.reason, "source": body.source})
    session.add(ev); session.commit(); session.refresh(r, ev)
    return StatusChangeResponse(rule_version_id=r.rule_version_id, status=r.status, event_id=ev.event_id)


@router.get("/{rule_version_id}/events", response_model=list[RuleVersionEventResponse])
def list_rule_version_events(rule_version_id: uuid.UUID, session: SessionDep) -> list[RuleVersionEventResponse]:
    _load_rule_or_404(session, rule_version_id)
    rows = (session.query(RuleVersionEvent)
            .filter(RuleVersionEvent.rule_version_id == rule_version_id)
            .order_by(RuleVersionEvent.created_at.desc(), RuleVersionEvent.event_id.desc())
            .all())
    return [RuleVersionEventResponse(
        event_id=e.event_id, action=e.action, operator=e.operator,
        detail=e.detail or {}, created_at=e.created_at,
    ) for e in rows]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/api/test_rule_version_mgmt.py -v`
Expected: 全 PASS

- [ ] **Step 5: 全量门禁 + 提交**

Run: `uv run ruff check . && uv run black --check . && uv run pytest -x`

```bash
git add app/api/rule_versions.py tests/api/test_rule_version_mgmt.py dev-notes/
git commit -m "feat(rules): publish/disable/events endpoints + lifecycle audit"
```

---

### Task 5: create_batch 加 rule status 校验（停用选用边界）

**Files:**
- Modify: `app/api/batches.py:78-84`（`create_batch` 规则解析处加 2 行）
- Test: `tests/api/test_batches_rule_status.py`（新建）

**Interfaces:**
- Consumes: 既有 `create_batch` + `QualityRuleVersion.status`
- Produces: 行为变更——`create_batch` 选 `draft`/`disabled` 规则 → 400 `{"reason": "rule version not published", "status": <status>}`

- [ ] **Step 1: 写失败测试**

`tests/api/test_batches_rule_status.py`：

```python
"""create_batch must reject non-published rule versions."""
from ...  # 复用 test_batches.py 的 _make_app + seed 模式


def test_create_batch_rejects_draft_rule() -> None:
    # seed draft rule → create_batch 选它 → 400
    ...
    assert resp.status_code == 400
    assert "not published" in resp.json()["detail"]["reason"]


def test_create_batch_rejects_disabled_rule() -> None:
    # seed disabled rule → 400


def test_create_batch_accepts_published_rule() -> None:
    # seed published rule → 201（现状行为，回归）


def test_disabled_rule_does_not_affect_existing_batch_rejudge() -> None:
    # 关键：停用不影响在途快照复算
    # 复用 test_batches.py 的 judge seed 模式（L90-100 的 rule seed + inspection seed）：
    # seed published rule + 一个已判 pass 的批次（含 ModuleInspection + rule_snapshot）
    # → disable 该 rule → 对该批次再跑 judge_batch（POST /api/batches/{id}/judge）
    # → 仍 200 + pass（判定读 rule_snapshot，不重读 rule.status）
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/api/test_batches_rule_status.py -v`
Expected: draft/disabled 用例 FAIL（现返回 201）

- [ ] **Step 3: 改 batches.py create_batch**

`app/api/batches.py:78-84`，在 `if rule is None: ...` 之后、`BatchRepository().create_batch(...)` 之前插入：

```python
        if rule.status != "published":
            raise HTTPException(
                status_code=400,
                detail={
                    "rule_version_id": body.rule_version_id,
                    "reason": "rule version not published",
                    "status": rule.status,
                },
            )
```

- [ ] **Step 4: 跑测试确认通过 + 既有 batches 回归**

Run: `uv run pytest tests/api/test_batches_rule_status.py tests/api/test_batches.py -v`
Expected: 新用例 PASS + 既有 `test_batches.py` 全 PASS（其 seed rule 均为 `status="published"`，已确认 L90/100/159/169/417/427）

- [ ] **Step 5: 全量门禁 + 提交**

Run: `uv run ruff check . && uv run black --check . && uv run pytest -x`

```bash
git add app/api/batches.py tests/api/test_batches_rule_status.py dev-notes/
git commit -m "feat(rules): create_batch rejects non-published rule versions (400)"
```

---

### Task 6: 收尾——dev-notes + 设计文档 §9.6 更新 + 全量验证

**Files:**
- Modify: `dev-notes/`（新建 `dev-notes/rule-version-mgmt/` 记过程）
- Modify: `docs/质慧链-设计文档.md` §9.6（把"管理面属下一阶段"改为"已实现"）
- Modify: `docs/superpowers/specs/2026-10-14-rule-version-management-design.md`（标记落地状态）

**Interfaces:** 无代码

- [ ] **Step 1: 全量门禁 + e2e 冒烟**

Run: `uv run ruff check . && uv run black --check . && uv run pytest -x`
Expected: 全绿

Run: 起 uvicorn + 手动走 create→publish→disable→events 一遍（Postgres 容器），验证触发器 `fn_rule_version_guard` 兜底（试图 UPDATE published rule 字段 → 触发器 REJECT）

- [ ] **Step 2: 更新设计文档 §9.6 + spec 落地标记**

`docs/质慧链-设计文档.md` §9.6 改为：
> 管理面（创建、发布、停用）已实现（2026-10-14）：`POST /api/quality-rule-versions`（幂等建 draft）+ `/publish` + `/disable` + `/events`（全生命周期审计，`rule_version_events` 表）。`create_batch` 拒绝选用非 published 规则（400）。停用只挡新批次选用，不动在途快照。

`docs/superpowers/specs/2026-10-14-rule-version-management-design.md` 顶部加"已实现（commit X）"标记。

- [ ] **Step 3: dev-notes 收口节点 + 提交**

`dev-notes/rule-version-mgmt/` 记：范围决策（纯 API 无 AI/无认证/无回滚）、5 项 builder check 判断、冻结层改动标注、全量验证结果。

```bash
git add dev-notes/ docs/质慧链-设计文档.md docs/superpowers/specs/2026-10-14-rule-version-management-design.md
git commit -m "docs(rules): 规则版本管理收口 — dev-notes + 设计文档 §9.6 落地标记"
```

---

## Execution Notes

- **执行模式**：本计划 6 任务，Task 1→2→3→4→5 有接口依赖（后续 import 前序产物），Task 6 收尾。建议 **subagent-driven**（每任务独立可测，reviewer 把关冻结层改动）。若用 native 自执行，每任务后跑全量门禁再进下一任务。
- **冻结层**：Task 1 改 `models.py`（纯新增），Task 2 新增 `domain/rule_version.py`——提交时 dev-notes 标注"扩冻结层、纯新增/纯函数、零既有改动"。Task 5 改 `batches.py`（API 层非冻结）但须回归 `test_batches.py`。
- **迁移 003**：先落 `migrations/003_rule_version_events.sql`（可独立 psql 执行）；alembic revision 可选（跟随 002 惯例——`002_reinspection_scope.py` 已 alembic 化但 `002_intake_status_isolated` 未，本任务不强制 alembic，留 dev-notes 标注）。
- **AI 留口**：Task 3/4 的 `source`/`recommendation_ref` 字段位已落地（spec §7）；§12 实现时复用同一套端点，填 `source="recommended"` + `recommendation_ref`，不改表结构。
