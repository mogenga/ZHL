# Phase 0 · 共享契约 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 每个任务完成后停止，等待人工核验（AGENT.md 要求）。

**Goal:** 建立 Phase 0 共享契约：一份可独立执行的 DDL（12 表 + 触发器 + 约束）、domain 纯函数（判定引擎 / 放行断言 / 状态机）、SQLAlchemy 数据模型、证据库 Protocol 骨架、测试夹具（四组验收样例 + 种子），全绿单测（domain ≥ 90% 覆盖）。

**Architecture:** domain 层是纯函数（不 import infra/langgraph/sqlalchemy），是 Phase A/B 的判定与状态转移唯一依据；infra/models.py 与 001_init.sql 双向对齐（模型 create_all 自检）；夹具数据直接喂给 domain 纯函数验证四组验收样例的判定结果。

**Tech Stack:** Python 3.11+ / PostgreSQL 15+ / SQLAlchemy 2.0 / Pydantic v2 / pytest + pytest-cov / ruff + black

**Spec:** `docs/superpowers/specs/2026-10-10-phase0-shared-contract-design.md`（下称 P0 spec）

## Global Constraints

（全部来自 P0 spec §3 / §4，逐条引用）

- Python 3.11+，black 格式化，ruff lint（AGENT.md §3.1）
- domain 层 100% 类型注解，纯函数不 import `sqlalchemy` / `app.infra.*` / `langgraph` / 模型服务，不读库、不取时间、不调随机、不调模型（AGENT.md §8 / P0 spec §3.2）
- 技术栈锁定：FastAPI / PostgreSQL 15+ / SQLAlchemy 2.0 / Pydantic v2 / pytest + httpx；禁止引入 spec 外新库（P0 spec §3.1）
- 判定顺序固定：① 基础校验 → ② 功率范围 → ③ 参数容差 → ④ 电流分档 → ⑤ 外观；容差闭区间，等于上下限均 Pass（P0 spec §3.3）
- 放行断言逐条返回 6 项 `AssertionCheck`，不返回单一布尔（P0 spec §3.5）
- 批次 10 态 + 异常 5 态转移表写死在 domain，模型不可改写（P0 spec §3.4）
- DDL 一份 `migrations/001_init.sql` 建齐 12 表，Phase A/B 零 DDL 变更；可独立执行（D1/D16/D18）
- SN 跨批次 EXCLUDE 不降级（D17）：`btree_gist + sn_uniqueness int8range`
- 规则版本 published 不可改（D6 触发器）；原始检验值列级 NO UPDATE（D7）；证据只追加（D8 权限回收）
- alembic 仅初始化配置，不生成 revision；触发器/EXCLUDE/权限手写进 001_init.sql（D19）
- domain 单测 ≥ 90% 覆盖率，纯内存跑（AGENT.md §3.3）
- dev-notes 按 `dev-notes/agnes-20261010.md` 逐节点留痕（AGENT.md §1）
- 每个任务完成后 `uv run ruff check . && uv run black --check . && uv run pytest -x` 全绿才可提交（AGENT.md §3.4 / §10）
- 演示止于 DDL + 单测，不起 FastAPI/uvicorn（D18）
- 所有 Python 工具命令（pytest / ruff / black / uvicorn）一律 `uv run` 前缀；依赖管理走 uv（`uv add` / `uv lock` / `uv sync`），`uv.lock` 必须提交（AGENT.md §10）

## Review Focus

（spec 暗示但任务测试未覆盖、最可能伤人的 5 类输入/失败模式）

1. **浮点边界**：Pmax = 533.5（恰好下限）判 Pass、533.4 判 Fail —— 由 Task 4 的 `acceptance_boundary.json` 夹具消费用例钉死
2. **非法状态转移**：`pending_judge → released` 之类跳步必须被 `transition()` 拒绝并返回缺失前置条件 —— 由 Task 6 状态机非法路径用例钉死
3. **SN 跨批次冲突**：同 SN 第二批次插入应被 EXCLUDE 拒绝 —— 由 Task 5 DDL 验证步骤钉死
4. **断言阻断**：存在未关闭 critical 异常 / 隔离 SN 时 `assert_release` 逐条 Fail，不能笼统 False —— 由 Task 6 断言用例钉死
5. **published 规则 UPDATE**：触发器应拦截字段值变更、放行 status 单列变更 —— 由 Task 5 DDL 验证步骤钉死

---

## File Structure

| 文件 | 责任 |
| --- | --- |
| `pyproject.toml` | 依赖 + ruff/black/pytest/coverage 配置 |
| `alembic.ini` + `alembic/env.py` + `alembic/versions/.gitkeep` | 迁移工具骨架（D19：不生成 revision） |
| `app/__init__.py` / `app/domain/__init__.py` / `app/infra/__init__.py` / `app/plugins/__init__.py` | 包骨架 |
| `app/domain/types.py` | 所有纯函数入参/出参 dataclass（ModuleInspectionIn / RuleSnapshotIn / Decision / RuleCheck / BatchView / AssertionResult / IncidentView / ReinspectionView / TransitionResult / CloseResult / IsoRow 等） |
| `app/domain/judge.py` | 判定引擎纯函数（judge + 5 个 check_* + tolerance_interval） |
| `app/domain/state_machine.py` | 批次/异常转移表数据 + transition() + close_incident() |
| `app/domain/release.py` | assert_release() + 6 个 check_* 断言 |
| `app/domain/sampling.py` | iso_lookup() + freeze_sample() |
| `app/infra/models.py` | 12 表 SQLAlchemy 2.0 模型 |
| `app/infra/schemas.py` | Pydantic v2 请求/响应 schema（骨架） |
| `app/plugins/evidence_store.py` | EvidenceStore Protocol + FileEvidenceStore 骨架 |
| `migrations/001_init.sql` | 全部 DDL + 触发器 + 权限 + 种子导入 |
| `tests/fixtures/*.json` + `tests/fixtures/loader.py` | 六类夹具 + 加载器 |
| `tests/domain/test_judge.py` / `test_state_machine.py` / `test_release.py` / `test_sampling.py` / `test_acceptance.py` | domain 单测（含四组验收消费） |
| `tests/infra/test_models.py` | 模型 create_all 自检 |
| `tests/plugins/test_evidence_store.py` | 证据库骨架单测 |

## 任务总览

| Task | 交付 | 依赖 |
| --- | --- | --- |
| 1 | 项目脚手架（pyproject/目录/pytest/alembic 配置） | 无 |
| 2 | domain 类型定义（types.py） | Task 1 |
| 3 | 判定引擎（judge.py）+ 单测 | Task 2 |
| 4 | 测试夹具 + 加载器 + 边界/正常验收用例 | Task 3 |
| 5 | DDL 001_init.sql（12 表 + 触发器 + 约束 + 验证） | Task 4 |
| 6 | 状态机 + 放行断言（state_machine.py / release.py）+ 单测 | Task 2, 4 |
| 7 | 抽样 + 证据库骨架 + SQLAlchemy 模型 + 模型自检 | Task 4, 5 |
| 8 | 冻结 + 交付（tag / 覆盖率 / 全量验证 / dev-notes 收口） | Task 1–7 |

每个任务完成后**停止并等待人工核验**，再进入下一任务。

---

### Task 1: 项目脚手架

**Files:**
- Create: `pyproject.toml`
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/.gitkeep`
- Create: `app/__init__.py`, `app/domain/__init__.py`, `app/infra/__init__.py`, `app/plugins/__init__.py`, `tests/__init__.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: 可运行的 pytest 环境（`pytest -q` 能收集测试）；black/ruff 配置生效；alembic 配置就位（不生成 revision，D19）

- [ ] **Step 1: 写 `pyproject.toml`**

依赖（锁定，P0 spec §3.1）：`fastapi`, `sqlalchemy[asyncio]`, `alembic`, `pydantic`, `pytest`, `httpx`, `psycopg[binary]`, `langgraph-checkpoint-postgres`；dev：`pytest-cov`, `ruff`, `black`, `aiosqlite`。Python ≥ 3.11。pytest 配置：`testpaths=["tests"]`。ruff 与 black 配置段。

- [ ] **Step 2: 建目录骨架 + alembic 配置（D19：不生成 revision）**

`app/`（domain/infra/plugins 各放空 `__init__.py`）、`tests/`、`migrations/`（.gitkeep）、`data/evidence/.gitkeep`、`alembic/`（env.py 指向 `app.infra.models`，target_metadata 留 TODO 注释待 Task 7 填，D19 只初始化）。

- [ ] **Step 3: 验证**

Run: `uv sync`（首次建 .venv + 按 uv.lock 同步；确认 lock 与 pyproject 一致）
Run: `uv run pytest -q`（此时无测试，Expected: `no tests ran` 且无 import 错误）
Run: `uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 4: Commit**

```
git add pyproject.toml alembic.ini alembic app tests migrations data .gitignore
git commit -m "chore: project scaffold — deps, dirs, pytest/ruff/black/alembic config"
```

**人工核验点**：pytest 可运行、ruff/black 全绿、目录结构与 P0 spec §3.3 一致。

---

### Task 2: domain 类型定义（types.py）

**Files:**
- Create: `app/domain/types.py`

**Interfaces:**
- Consumes: Task 1 脚手架
- Produces: 全部 dataclass，Task 3/4/6/7 依赖：
  - `ModuleInspectionIn`（sn, project, grade, captured_date, pmax, temp, isc, voc, vm, im, ff, appearance_result）—— 含基础校验所需六项电参数 + SN + 等级 + 日期（P0 spec §3.3）
  - `RuleSnapshotIn`（nameplate_power, power_tolerance, parameter_standards: dict, current_bands: list, sample_plan）
  - `ParamStandard`（std_value, tolerance, enabled: bool）
  - `CurrentBand`（lower, upper, grade）
  - `RuleCheck`（rule: str, passed: bool, detail: dict）—— 逐项证据（四元组：input/formula/computed/result）
  - `Decision`（result: "Pass"|"Fail", checks: list, rule_snapshot, batch_id, sn）—— 遵循 AGENT.md §6.2 接口契约
  - `BatchView` / `SampleView` / `IncidentView` / `EvidenceView` / `CapaView` / `ReinspectionView`（assert_release 入参）
  - `AssertionCheck`（name, passed, reason）/ `AssertionResult`（passed, checks）
  - `TransitionResult`（accepted: bool, reason: str）/ `CloseResult`（can_close: bool, missing: list）
  - `IsoRow` / `IsoResult`（n, ac, re）/ `SampleEntry`（sn, selected）
  - `IncidentTrigger`（trigger_type, batch_id, affected_sns, evidence_ids）/ `ReinspectionResult`（reinspection_id, incident_id, sn, passed, evidence_ids）—— AGENT.md §6.2

- [ ] **Step 1: 写 `app/domain/types.py`**

全部用 `@dataclass(frozen=True)`，100% 类型注解。`RuleCheck.detail` 的四元组结构按总 spec §6.3 示例（`{rule, input, formula, computed, result}`）。

- [ ] **Step 2: 验证 import 与纯度**

Run: `python -c "from app.domain.types import Decision, ModuleInspectionIn, AssertionResult"` → 无错
Run: `uv run ruff check app/domain/ && uv run black --check app/domain/` 全绿

- [ ] **Step 3: Commit**

```
git add app/domain/types.py
git commit -m "feat(domain): types — dataclasses for judge/release/state-machine/sampling"
```

**人工核验点**：types 与 P0 spec §5.2 签名清单一一对应，无 infra import。

---

### Task 3: 判定引擎（judge.py）+ 单测

**Files:**
- Create: `app/domain/judge.py`
- Test: `tests/domain/test_judge.py`

**Interfaces:**
- Consumes: `app.domain.types`（Task 2）
- Produces:
  - `judge(inspection: ModuleInspectionIn, rule_snapshot: RuleSnapshotIn) -> Decision`
  - `check_required_fields(inspection) -> RuleCheck`
  - `check_power_range(inspection, rule_snapshot) -> RuleCheck`
  - `check_parameter_tolerances(inspection, rule_snapshot) -> RuleCheck`
  - `check_current_band(inspection, rule_snapshot) -> RuleCheck`
  - `check_appearance(inspection) -> RuleCheck`
  - `tolerance_interval(std: float, tol: float) -> tuple[float, float]`

- [ ] **Step 1: 写失败单测 `tests/domain/test_judge.py`**

必含用例（Review Focus #1 钉死）：
- `test_tolerance_interval_exact`：`tolerance_interval(550, 0.03) == (533.5, 566.5)`
- `test_power_at_lower_boundary_pass`：pmax=533.5 → check_power_range.passed is True（闭区间）
- `test_power_below_boundary_fail`：pmax=533.4 → passed is False
- `test_power_at_upper_boundary_pass`：pmax=566.5 → True
- `test_required_fields_missing_sn`：sn 缺失 → check_required_fields passed False
- `test_parameter_tolerance_disabled_skipped`：parameter_standards 中某项 enabled=False → 该项不参与判定
- `test_current_band_in_range` / `test_current_band_out_of_range`
- `test_appearance_fail`
- `test_judge_all_pass`：全合格 → Decision.result == "Pass"，checks 5 项全 True
- `test_judge_one_fail`：一项 Fail → result == "Fail"
- 每个 check_* 的 `detail` 含四元组（input/formula/computed/result）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/domain/test_judge.py -v` → Expected: 全部 FAIL（judge 未实现）

- [ ] **Step 3: 实现 `app/domain/judge.py`**

`tolerance_interval` 闭区间；`check_power_range` 用 `lower ≤ pmax ≤ upper`；`check_parameter_tolerances` 逐项遍历 enabled 项；`check_current_band` 遍历 current_bands 找落档；`judge` 按固定顺序组装 5 个 check，全 True 才 Pass。纯函数，无 IO。

- [ ] **Step 4: 跑测试确认全绿**

Run: `uv run pytest tests/domain/test_judge.py -v` → Expected: PASS
Run: `uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 5: Commit**

```
git add app/domain/judge.py tests/domain/test_judge.py
git commit -m "feat(domain): judge engine — 5-check pure function + boundary tests"
```

**人工核验点**：边界值 533.5 Pass / 533.4 Fail 用例通过；判定顺序固定；无 infra import。

---

### Task 4: 测试夹具 + 加载器 + 正常/边界验收消费

**Files:**
- Create: `tests/fixtures/rules_seed.json`, `iso2859_1_aql.json`, `acceptance_normal.json`, `acceptance_boundary.json`, `acceptance_incident.json`, `acceptance_consistency.json`, `mock_llm_outputs.json`, `tests/fixtures/loader.py`
- Test: `tests/domain/test_acceptance.py`

**Interfaces:**
- Consumes: `app.domain.judge`（Task 3）、`app.domain.types`（Task 2）
- Produces: `load_acceptance(name) -> dict`（提供 batch/inspections/rules/incidents 命名空间）；Task 5/6/7 复用夹具

- [ ] **Step 1: 写夹具 JSON（P0 spec §5.5 六类）**

- `rules_seed.json`：2 型号（JH-550W 默认 nameplate_power=550, power_tolerance=0.03 + 1 测试型号），幂等键 `project_id+model+version_no`（D12）
- `iso2859_1_aql.json`：3 检验类型 × 3 AQL(0.65/1.0/1.5) × 批量区间 ≈ 30 行，字段 `[inspection_type, inspection_level, aql, lot_size_min, lot_size_max] → [n, ac, re]`（D9）
- `acceptance_normal.json`：1 批次 10 组件全 Pass + 规则快照 → 消费后 `judge` 全 Pass（样例一）
- `acceptance_boundary.json`：Pmax=功率下限 Pass + 对照组 下限−0.1 Fail（样例二）
- `acceptance_incident.json`：Impp 超档 + 历史同类 + 复检数据（样例三，Task 6 状态机消费）
- `acceptance_consistency.json`：同 SN 跨批次（样例四，Task 5 DDL 消费）
- `mock_llm_outputs.json`：归因/抽取固定模型输出（MockLLMClient 用）

- [ ] **Step 2: 写 `tests/fixtures/loader.py`**

`load_acceptance(name: str) -> dict`：读 JSON，返回 `{"batch": {...}, "inspections": [...], "rules": [...], "incidents": [...], "reinspections": [...]}`，字段缺省为 None/[]。

- [ ] **Step 3: 写 `tests/domain/test_acceptance.py`（正常 + 边界两组消费）**

- `test_normal_all_pass`：`load_acceptance("normal")` 10 组件逐条 `judge` → 全 Pass
- `test_boundary_pass_and_fail`：`load_acceptance("boundary")` 主件 judge Pass、对照组 judge Fail
- 断言 `Decision.rule_snapshot` 与规则种子一致（可复算）

- [ ] **Step 4: 跑测试确认全绿**

Run: `uv run pytest tests/domain/test_acceptance.py -v` → Expected: PASS
Run: `uv run pytest tests/domain/ -q` → 全绿

- [ ] **Step 5: Commit**

```
git add tests/fixtures tests/domain/test_acceptance.py
git commit -m "test(fixtures): six fixture sets + loader + normal/boundary acceptance via judge"
```

**人工核验点**：六类夹具齐全、字段与 P0 spec §5.5 一致；正常/边界两组 judge 结果符合样例一/二预期。

---

### Task 5: DDL 001_init.sql（12 表 + 触发器 + 约束 + 验证）

**Files:**
- Create: `migrations/001_init.sql`

**Interfaces:**
- Consumes: P0 spec §5.1 全部 12 表字段清单（权威）
- Produces: 可独立 `psql -f` 执行的 DDL；Task 7 模型 create_all 自检与之对齐

- [ ] **Step 1: 写 `migrations/001_init.sql`（按 P0 spec §5.1 逐表）**

头部：`CREATE EXTENSION IF NOT EXISTS btree_gist; CREATE EXTENSION IF NOT EXISTS "uuid-ossp";`（gen_random_uuid）
按 spec §5.1.1–§5.1.12 顺序建 12 表（先 evidence / quality_rule_versions 等被引用表，再引用方），含：
- 全部列 + 类型 + 默认 + NOT NULL
- UNIQUE / PK / FK（ON DELETE 语义按 spec）
- 枚举用 Postgres `CREATE TYPE ... AS ENUM`
- 表级约束：`module_inspections` UNIQUE(batch_id,sn) + EXCLUDE USING gist(sn_uniqueness WITH &&)（D5/D17）+ BEFORE INSERT 触发器填 sn_num/`sn_uniqueness`（D5）+ 列级 `NO UPDATE`（D7 权限）
- `quality_rule_versions` BEFORE UPDATE/DELETE 触发器拦截 published（D6）
- `evidence` / `iso_sampling_table` 权限回收（D8/D9）
- 索引：高频查询（batch_id、sn、incident_id、batch+report_type）加 btree；JSONB 列加 GIN（rule_snapshot / decision_evidence / structured_output）
- 末尾：规则种子 + ISO 表种子 INSERT（幂等 ON CONFLICT DO NOTHING）

- [ ] **Step 2: 在 PostgreSQL 15+ 验证 DDL 独立可执行**

Run: `psql $ZHL_DB -f migrations/001_init.sql` → Expected: 无错，12 表 + 扩展 + 触发器 + 种子建成
手工验证（Review Focus #3/#5 钉死）：
- `INSERT` 同 SN 到第二批次 → EXCLUDE 报错（被拒）
- `UPDATE quality_rule_versions SET nameplate_power=... WHERE status='published'` → 触发器 REJECT
- `UPDATE module_inspections SET pmax=...`（应用角色）→ 列级权限拒
- `DELETE FROM evidence`（应用角色）→ 权限拒

- [ ] **Step 3: Commit**

```
git add migrations/001_init.sql
git commit -m "feat(db): 001_init.sql — 12 tables + EXCLUDE + triggers + seeds, independently executable"
```

**人工核验点**：`psql -f` 零错；EXCLUDE/触发器/权限回收手工验证通过（不降级，D17）。

---

### Task 6: 状态机 + 放行断言（state_machine.py / release.py）+ 单测

**Files:**
- Create: `app/domain/state_machine.py`, `app/domain/release.py`
- Test: `tests/domain/test_state_machine.py`, `tests/domain/test_release.py`

**Interfaces:**
- Consumes: `app.domain.types`（Task 2）、`tests/fixtures`（Task 4：acceptance_incident 消费）
- Produces:
  - `BATCH_TRANSITIONS` / `INCIDENT_TRANSITIONS`（转移表数据）
  - `transition(current: str, target: str, preconditions: dict, kind: str) -> TransitionResult`
  - `close_incident(incident: IncidentView, reinspection: ReinspectionView, capa_verified: bool) -> CloseResult`
  - `assert_release(batch, sample, incidents, isolated_sns, evidence, capa) -> AssertionResult`
  - 6 个 `check_*` 断言函数（P0 spec §5.2）

- [ ] **Step 1: 写失败单测 `tests/domain/test_state_machine.py`**

必含（Review Focus #2 钉死）：
- 合法路径：`pending_judge → pending_release`（全部 Pass 前置）accepted
- 非法跳步：`pending_judge → released` rejected，reason 给缺失前置
- `pending_judge → isolated`（任一 Fail）accepted
- `pending_recheck → rejected`（复检失败）accepted
- 异常关闭五条件（`close_incident`）：缺任一 → can_close False + missing 列表；全满足 → True（消费 `acceptance_incident`）

- [ ] **Step 2: 写失败单测 `tests/domain/test_release.py`**

必含（Review Focus #4 钉死）：
- 全满足 → AssertionResult.passed True，6 项 checks 全 True
- 存在未关闭 critical 异常 → `check_no_open_critical_incident` passed False，整体 passed False
- 存在隔离 SN → `check_no_isolated_sn` False
- 逐条返回（不笼统布尔）：`result.checks` 长度 6，每项有 name/reason

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/domain/test_state_machine.py tests/domain/test_release.py -v` → Expected: FAIL

- [ ] **Step 4: 实现 `state_machine.py` + `release.py`**

转移表 dict 数据 + `transition` 查表 + 前置校验；`close_incident` 校验五条件；`assert_release` 组装 6 个 check，`passed = all(...)`。纯函数，无 IO。

- [ ] **Step 5: 跑测试确认全绿**

Run: `uv run pytest tests/domain/test_state_machine.py tests/domain/test_release.py -v` → PASS
Run: `uv run pytest tests/domain/ -q && uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 6: Commit**

```
git add app/domain/state_machine.py app/domain/release.py tests/domain/test_state_machine.py tests/domain/test_release.py
git commit -m "feat(domain): state machines + release assertions with 5-condition close"
```

**人工核验点**：非法转移被拒且给原因；断言逐条返回；异常关闭五条件缺一即拒。

---

### Task 7: 抽样 + 证据库骨架 + SQLAlchemy 模型 + 模型自检

**Files:**
- Create: `app/domain/sampling.py`, `app/plugins/evidence_store.py`, `app/infra/models.py`, `app/infra/schemas.py`
- Test: `tests/domain/test_sampling.py`, `tests/infra/test_models.py`, `tests/plugins/test_evidence_store.py`

**Interfaces:**
- Consumes: `app.domain.types`（Task 2）、`tests/fixtures`（Task 4：iso 表 + incident）、`migrations/001_init.sql`（Task 5）
- Produces:
  - `iso_lookup(inspection_type, level, aql, lot_size, table) -> IsoResult | None`（缺项返回 None → 判定规则异常，不放行，D9）
  - `freeze_sample(batch_id, lot_size, iso_row, sns, seed) -> list[SampleEntry]`（固定 seed 分层随机）
  - `EvidenceStore` Protocol + `FileEvidenceStore` 骨架（save/get/list_by_object，SHA-256，不接 DB 写，P0 spec §5.4）
  - `app/infra/models.py`：12 表 SQLAlchemy 2.0 映射（与 001_init.sql 对齐）
  - `app/infra/schemas.py`：Pydantic v2 请求/响应骨架

- [ ] **Step 1: 写失败单测 `tests/domain/test_sampling.py`**

- `test_iso_lookup_hit`：iso 表内组合返回 n/ac/re
- `test_iso_lookup_missing_returns_none`：查不到组合 → None（规则异常，禁止默认值放行）
- `test_freeze_sample_deterministic`：同 seed + 同 sns → 两次结果一致
- `test_freeze_sample_respects_n`：返回样本数 == iso_row.n

- [ ] **Step 2: 跑确认失败 → 实现 `sampling.py`**

Run: `uv run pytest tests/domain/test_sampling.py -v` → FAIL 后实现，再 → PASS

- [ ] **Step 3: 写 `app/plugins/evidence_store.py` 骨架 + 单测**

`EvidenceStore` Protocol（save/get/list_by_object，P0 spec §5.4）+ `FileEvidenceStore`（本地目录 + SHA-256 哈希，不接 DB 写）
`tests/plugins/test_evidence_store.py`：save 计算哈希、get 读回、list_by_object 按 object_ref 过滤

- [ ] **Step 4: 写 `app/infra/models.py` + `app/infra/schemas.py` + 模型自检**

12 表 SQLAlchemy 2.0 Declarative（枚举用 `sqlalchemy.Enum`，JSONB 列，与 001_init.sql 对齐）；schemas.py Pydantic v2 骨架
`tests/infra/test_models.py`：`Base.metadata.create_all` 在 aiosqlite 内存库不报错（schema 一致性自检）

- [ ] **Step 5: 跑全量确认全绿**

Run: `uv run pytest tests/ -q && uv run ruff check . && uv run black --check .` → 全绿
Run: 回填 `alembic/env.py` 的 target_metadata 指向 `app.infra.models`（D19，仍不生成 revision）

- [ ] **Step 6: Commit**

```
git add app/domain/sampling.py app/plugins/evidence_store.py app/infra/models.py app/infra/schemas.py tests/domain/test_sampling.py tests/infra/test_models.py tests/plugins/test_evidence_store.py alembic/env.py
git commit -m "feat: sampling + evidence store + SQLAlchemy models + self-check"
```

**人工核验点**：iso 缺项返回 None（不放行）；抽样可复现；模型 create_all 与 DDL 对齐。

---

### Task 8: 冻结 + 交付收口

**Files:**
- Modify: `dev-notes/agnes-20261010.md`（补「每个任务完成」+「finish」节点）
- Create: `.gitignore` 补 `data/evidence/`（如缺）

**Interfaces:**
- Consumes: Task 1–7 全部产物

- [ ] **Step 1: 覆盖率验证（domain ≥ 90%）**

Run: `uv run pytest tests/domain/ --cov=app/domain --cov-report=term-missing` → Expected: 总覆盖率 ≥ 90%
若不足，补边界/非法路径用例（不得为凑数写无意义测试）

- [ ] **Step 2: 全量验证（P0 spec §6 完成标准逐条）**

Run: `psql $ZHL_DB -f migrations/001_init.sql && uv run pytest tests/ -v` → 全绿（演示命令，D18）
Run: `uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 3: 冻结（D20）**

git tag `phase0-frozen`（对 `app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/`）

- [ ] **Step 4: dev-notes 收口 + 交付清单**

`dev-notes/agnes-20261010.md` 追加：每个任务完成节点 + finish 节点（交付物清单 + 测试结果 + 已知遗留）
整理交付：功能演示命令（Step 2）+ 全量 pytest 结果 + dev-notes 路径 + 已知遗留

- [ ] **Step 5: Commit + tag**

```
git add dev-notes/agnes-20261010.md
git commit -m "chore: phase0 finish — dev-notes, coverage gate, freeze tag"
git tag phase0-frozen
```

**人工核验点**：domain 覆盖率 ≥ 90%；演示命令可复现；`phase0-frozen` tag 已打；dev-notes 完整。

---

## Self-Review

1. **Spec 覆盖**：P0 spec §5.1 12 表 → Task 5/7；§5.2 纯函数 → Task 3/6/7；§5.5 夹具 → Task 4；§5.4 证据库 → Task 7；§6 完成标准 → Task 8。无 gap。
2. **步骤粒度**：每步单一可检查结果。夹具/DDL 步骤给的是「按 spec §5.x 写」的指针（spec 是权威），不过度展开为代码。
3. **类型一致性**：`judge`/`transition`/`assert_release`/`iso_lookup` 签名与 P0 spec §5.2 一致；types 在 Task 2 统一定义，后续任务只 import。
4. **Review Focus 5 项**：#1 边界→Task 4；#2 非法转移→Task 6；#3 SN 冲突→Task 5；#4 断言阻断→Task 6；#5 published UPDATE→Task 5。每项已落到任务测试。
5. **篇幅**：plan 以任务 + 指针到 spec 为主，未复制 spec 全文；代码体只保留类型签名与测试断言骨架。
