# Phase B · 异常闭环 + 报告归档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **每个任务完成后停止，等待人工核验，再进入下一任务（开发要求 + AGENT.md §1）。**

**Goal:** 批次从「已隔离」经异常 API → 归因 Skill（证据门槛 + 置信度排序 + 三选一采纳）→ 复检 API（范围显式）→ 放行 API（7 项断言）→ 报告 Skill → 自动归档，端到端可复现；三层留痕（轨迹 / 证据 / 报告）贯穿全链路。

**Architecture:** 契约扩展（migration 002 + assert_release 第 7 项，PR+review 改 Phase 0 冻结件）先行锁定；能力层 LLMClient / 轨迹 / 报告 / 归因 Plugin 各自独立单测；编排层 LangGraph `incident_subgraph`（interrupt 三选一）；接入层 FastAPI 路由；E2E 用 mock `Decision` + `MockLLMClient` 驱动样例三/四，不依赖 Phase A。

**Tech Stack:** Python 3.11+ / FastAPI / PostgreSQL 15+（Docker，zhl-pg:15432）/ SQLAlchemy 2.0 / Pydantic v2 / LangGraph（StateGraph + interrupt/Command + PostgresSaver）/ pytest + httpx

**Spec:** `docs/superpowers/specs/2026-10-11-phaseb-incident-report-design.md`（下称 PB spec）

## Global Constraints

（全部来自 PB spec §3 / §4，逐条引用）

- 依赖方向固定：接入层 → 编排层（LangGraph 节点）→ 能力层（Plugin/Skill）→ domain（纯函数）→ 数据层；domain 不 import infra/sqlalchemy/langgraph/模型服务（PB spec §3.2 / AGENT.md §8 红线）
- 三层留痕分工（D1）：`agent_task_traces` 记 Agent 每步；`evidence` 记关键原始产物（复检原始值、人工确认差异（CAPA 确认）、源文件哈希、报告快照）——判定快照可复算不进证据表；`reports` 做归档。三层不互相替代（PB spec §3.6）
- 归因假设准入门槛（D2）：无 `evidence_id` 的假设在 Skill 层丢弃，不进界面；置信度（D3）只排序不过滤
- 断言失败不改批次状态（D4）：`assert_release` 与 `transition()` 互不调用；`archived` 无出边（D8），后续 API 409
- 复检范围显式列（D5/D5a/L2）：`scope_type`（minimal/expanded/full_lot，默认 minimal）+ `scope_sns`（JSONB 数组）；扩大的动作写轨迹 + 报告
- 报告生成完成即自动归档（D6/D6a）：归档内嵌于 `POST /report` 单事务，不暴露独立 `/archive` 端点
- `rejected` 只挂处置报告（D7）；报告版本化幂等，同 `report_type` 不新增版本（D10）
- CAPA `verified` 人工显式声明（L3）：不复检 pass 不自动推导；`quality_incidents.capa_verified` 列持久化
- LLMClient 由 Phase B 创建（L1/L1a）：`app/plugins/llm_client.py`；冻结 `mock_llm_outputs.json` 零改动，新增 `mock_llm_attribution.json`
- 断言第 7 项 `check_reinspection_scope_covered`（D7-7 方案 A）：复检覆盖范围 ⊇ 未关闭异常影响范围 + 抽样方案 ISO 表命中；**走 PR + review 改 Phase 0 冻结的 `assert_release`**（AGENT.md §6.1）
- Phase B 不新增 domain 纯函数（D14）；缺则 PR + review
- 冻结项 `app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/` 只读；变更走契约扩展 PR（本计划 Task 1 / Task 2 即该 PR）
- 事务边界（AGENT.md §4.3）：异常指派 / CAPA / 复检 / 放行 / 报告 + 归档 各自单事务；LLM 调用在事务外
- 提交前门禁：`uv run ruff check . && uv run black --check . && uv run pytest -x` 全绿（AGENT.md §3.4 / §10）
- 依赖一律 `uv add` / `uv remove`，`uv.lock` 提交；库用法先查 Context7 再写，查完 dev-notes 留痕（AGENT.md §2）
- dev-notes 按 `dev-notes/phase-b/agnes-20261011.md` 逐节点留痕（D12）

## Review Focus

（spec 暗示但最可能伤人的 5 类输入/失败模式，各钉入所属任务）

1. **无证据假设泄漏**：LLM 输出某假设 `evidence_ids=[]` → 必须被 Skill 过滤，绝不出现在 `root_cause_hypotheses` —— 由 Task 5 归因 Skill 用例钉死
2. **复检范围不覆盖**：复检只测 1 个 SN 而异常挂 3 个 SN → 断言第 7 项必须 Fail 并指明缺口 —— 由 Task 2（domain 断言）+ Task 10（E2E）钉死
3. **断言失败误改状态**：`assert_release` 返回全 False 时批次必须仍在 `pending_release`，不允许出现 `rejected`/`isolated` 回退 —— 由 Task 8 放行 API 用例钉死
4. **archived 终态误入**：已归档批次再调任何写 API（异常/复检/放行/报告）→ 一律 409，不产生任何行 —— 由 Task 9/10 钉死
5. **证据断链**：复检/报告引用的 `evidence_id` 在 `evidence` 表不存在 → 前置校验 400，拒绝写入 —— 由 Task 6/7 钉死

---

## File Structure

| 文件 | 责任 |
| --- | --- |
| `migrations/002_add_reinspection_scope.sql` | 契约 DDL：`reinspections.scope_type`/`scope_sns` + `quality_incidents.capa_verified` + 枚举类型 + 存量回填（L2/L3） |
| `alembic/versions/002_reinspection_scope.py` | alembic revision（upgrade 执行 002 SQL，downgrade 回滚），供增量迁移跟踪 |
| `app/domain/types.py`（修改） | 契约扩展：`ReinspectionView` 增 `scope_sns`；新增 `ReinspectionScope` 入参；`assert_release` 第 7 项所需类型（Task 2） |
| `app/domain/release.py`（修改） | `check_reinspection_scope_covered()` + `assert_release` 第 7 项（Task 2） |
| `app/infra/models.py`（修改） | `Reinspection.scope_type/scope_sns` + `QualityIncident.capa_verified` 列（Task 1 随 DDL） |
| `tests/fixtures/mock_llm_attribution.json` | 多假设归因夹具（含一条 `evidence_ids=[]` 验证门槛）；零改 `mock_llm_outputs.json`（Task 5） |
| `app/plugins/llm_client.py` | `LLMClient` Protocol + `OpenAICompatClient` + `MockLLMClient`（按 `incident_id` 路由）（L1）（Task 3） |
| `app/plugins/trace_store.py` | 轨迹写入/查询（`agent_task_traces`）（Task 4） |
| `app/plugins/report_renderer.py` | 报告 Markdown 渲染 + JSON 结构化（模板驱动）（Task 4） |
| `app/plugins/attribution.py` | Traceability Plugin：假设校验（证据门槛）+ 影响范围 + 排序（Task 5） |
| `app/skills/attribution_skill.py` | 归因 Skill：LLMClient → Plugin → 写 `root_cause_hypotheses` + 轨迹（Task 5） |
| `app/skills/report_skill.py` | 报告 Skill：聚合批次/异常/复检/断言/三层留痕 → 渲染 → 写 `reports` + 快照证据 + （条件）归档（Task 7） |
| `app/workflow/incident_subgraph.py` | `incident_subgraph`（4 节点 + 路由）+ `BatchWorkflowState` + 检查点装配（Task 6） |
| `app/api/incidents.py` | 异常创建 / 指派 / CAPA / 查询（Task 6） |
| `app/api/reinspections.py` | 复检创建 / 查询（Task 7 并入） |
| `app/api/release.py` | 放行（7 项断言）（Task 8） |
| `app/api/reports.py` | 报告生成 / 查询 + 自动归档（Task 9） |
| `app/api/main.py` | FastAPI app 装配（路由注册 + 静态目录 + 409 终态中间件钩子）（Task 9） |
| `app/static/incident.html` / `assertion.html` / `report.html` / `trace.html` | 4 个静态页（Task 10） |
| `tests/plugins/test_llm_client.py` / `test_attribution.py` / `test_trace_store.py` / `test_report_renderer.py` | 能力层单测（各 ≥70%） |
| `tests/workflow/test_incident_subgraph.py` | 子图回放 + interrupt/resume 断言 |
| `tests/api/test_incidents.py` / `test_reinspections.py` / `test_release.py` / `test_reports.py` | API 集成测试（httpx ASGITransport + SQLite/PG） |
| `tests/e2e/test_sample3_incident_closure.py` / `test_sample4_consistency.py` | 样例三/四端到端 |

## 任务总览

| Task | 交付 | 依赖 |
| --- | --- | --- |
| 1 | 契约扩展 PR（上）：`002_add_reinspection_scope.sql` + alembic revision + `models.py` 两表加列 + DDL 容器验证 | Phase 0 冻结件 |
| 2 | 契约扩展 PR（下）：`assert_release` 第 7 项 `check_reinspection_scope_covered` + `types.py` 扩展 + domain 单测 | Task 1 |
| 3 | LLMClient Plugin（L1/L1a）+ `mock_llm_attribution.json` + 单测 | Task 1 |
| 4 | 轨迹 Plugin + 报告渲染 Plugin + 单测 | Task 1 |
| 5 | 归因 Skill + Traceability Plugin + 单测（门槛/排序/影响范围） | Task 3, 4 |
| 6 | LangGraph `incident_subgraph` + 异常 API（创建/指派/CAPA/查询）+ 子图回放测试 | Task 2, 4, 5 |
| 7 | 复检 API + 报告 Skill（含自动归档 D6a）+ 报告 API + 单测 | Task 4, 6 |
| 8 | 放行 API（7 项断言 + 断言失败不改状态 D4）+ 单测 | Task 2, 6 |
| 9 | FastAPI app 装配 + `archived` 终态 409 守卫（D8）+ 全 API 冒烟 | Task 7, 8 |
| 10 | 4 静态页 + 样例三/四 E2E + 演示命令 + dev-notes 收口 + 覆盖率 | Task 9 |

每个任务完成后**停止并等待人工核验**；每个任务同步追加 dev-notes 节点（AGENT.md §1.2）。Task 1 + Task 2 合并为一个「契约扩展 PR + review」人工核验点（AGENT.md §6.1 要求 PR 级 review）。

---

### Task 1: 契约扩展 PR（上）—— DDL 002 + models 加列

**Files:**
- Create: `migrations/002_add_reinspection_scope.sql`
- Create: `alembic/versions/002_reinspection_scope.py`
- Modify: `app/infra/models.py`（`Reinspection` 加 `scope_type`/`scope_sns`；`QualityIncident` 加 `capa_verified`）
- Test: `tests/infra/test_models_002.py`（create_all 自检含新列）

**Interfaces:**
- Consumes: Phase 0 冻结 `001_init.sql` / `models.py` 基线
- Produces: `SCOPE_TYPE = ("minimal", "expanded", "full_lot")` 枚举（models 常量 + DDL）；`Reinspection.scope_type: Mapped[str]`（默认 `minimal`）、`Reinspection.scope_sns: Mapped[list]`（JSONB，默认 `[]`）、`QualityIncident.capa_verified: Mapped[bool]`（默认 `False`）

- [ ] **Step 1: 写 `migrations/002_add_reinspection_scope.sql`**

内容（按 L2/L3 + D5a）：
1. `scope_type` 列走 `VARCHAR(40) + CHECK (scope_type IN ('minimal','expanded','full_lot'))`（对齐 `models.py` 的 `native_enum=False` 约定，DDL 里同样不用 native enum，与 001 的枚举写法保持一致，写之前先读 `001_init.sql` 确认其枚举实现方式）
2. `ALTER TABLE reinspections ADD COLUMN scope_type VARCHAR(40) NOT NULL DEFAULT 'minimal' CHECK (scope_type IN ('minimal','expanded','full_lot'));`
3. `ALTER TABLE reinspections ADD COLUMN scope_sns JSONB NOT NULL DEFAULT '[]'::jsonb;`
4. `ALTER TABLE quality_incidents ADD COLUMN capa_verified BOOLEAN NOT NULL DEFAULT FALSE;`
5. 存量行回填：`scope_sns` 保持 `[]`（应用层创建复检时必传，回填仅保证 NOT NULL 可过）；注释说明回填语义
6. 头注：影响范围（仅加 3 列 + 1 枚举值域）、回滚方案（`DROP COLUMN` × 3）

- [ ] **Step 2: 写 alembic revision `alembic/versions/002_reinspection_scope.py`**

`upgrade()` 执行 002 SQL 的等价 SQLAlchemy 语句（`op.add_column` × 3 + 枚举值域 CHECK）；`downgrade()` drop 3 列；`down_revision = None`——**已勘察确认**：`alembic/versions/` 仅有 `.gitkeep`（Phase 0 D19 未生成 revision），本 revision 即 alembic 链首，注释写明 001 走 `psql -f` 直执、alembic 从 002 起步。

- [ ] **Step 3: 修改 `app/infra/models.py`**

`Reinspection` 加两列（`scope_type` 默认 `"minimal"`、`scope_sns` JSONB 默认 `[]`，用模块内已有的 `JSONB_T`）；`QualityIncident` 加 `capa_verified`（`Boolean`，默认 `False`）。仅加列，不动既有列/约束。

- [ ] **Step 4: DDL 容器验证（`docker zhl-pg`，端口 15432）**

Run: `docker start zhl-pg`（或 `.\scripts\start-pg.ps1`）
Run: `docker cp migrations/002_add_reinspection_scope.sql zhl-pg:/tmp/ && docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl -f /tmp/002_add_reinspection_scope.sql` → 无错
Run: `docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl -c "\d reinspections" -c "\d quality_incidents"` → 新列在
Run: 负向：重复执行 002 应报「column already exists」（幂等性说明写进 SQL 头注：002 不幂等，alembic 版本号保证只跑一次）

- [ ] **Step 5: 模型自检 + 全绿**

Create `tests/infra/test_models_002.py`：SQLite 内存库 `Base.metadata.create_all` + 断言 `Reinspection.__table__.c` 含 `scope_type`/`scope_sns`、`QualityIncident.__table__.c` 含 `capa_verified`；默认值插入自检（`scope_type` 缺省 = `minimal`）。
Run: `uv run pytest tests/infra/ -v` → PASS
Run: `uv run ruff check . && uv run black --check . && uv run pytest -x` → 全绿

- [ ] **Step 6: dev-notes 节点 + 提交（契约扩展 PR 上半，与 Task 2 合并 review）**

dev-notes 追加「任务完成 · Task 1 契约 DDL」节点（含 Context7 留痕：查过 alembic op.add_column 用法）。
```
git add migrations/002_add_reinspection_scope.sql alembic/versions/002_reinspection_scope.py app/infra/models.py tests/infra/test_models_002.py dev-notes/phase-b/
git commit -m "feat(contract): DDL 002 — reinspection scope columns + capa_verified (PB L2/L3)"
```

**人工核验点**：DDL 容器验证通过；models 自检绿；**不单独放行，与 Task 2 合并为契约扩展 PR 一次性 review（AGENT.md §6.1）**。

---

### Task 2: 契约扩展 PR（下）—— assert_release 第 7 项（D7-7 方案 A）

**Files:**
- Modify: `app/domain/types.py`（`ReinspectionView` 增 `scope_sns: tuple = ()`；新增 `ReinspectionScopeIn` 或复用——以最小改动为准：给 `ReinspectionView` 加 `scope_sns: tuple`，`assert_release` 新增入参 `reinspections: list[ReinspectionView]` 与 `iso_table: list[IsoRow]`）
- Modify: `app/domain/release.py`（`check_reinspection_scope_covered()` + 第 7 项进 `assert_release`）
- Modify: `tests/domain/test_release.py`（既有用例补新入参 + 新增第 7 项用例）

**Interfaces:**
- Consumes: Task 1 的 `scope_sns` 语义；Phase 0 `app.domain.sampling.iso_lookup(inspection_type, level, aql, lot_size, table)`
- Produces:
  - `check_reinspection_scope_covered(reinspections: list[ReinspectionView], open_incidents: list[IncidentView], iso_table: list[IsoRow]) -> AssertionCheck`（`name="reinspection_scope_covered"`）
  - `assert_release(batch, sample, incidents, isolated_sns, evidence, capa, reinspection_scopes: list[ReinspectionScopeIn], iso_table: list[IsoRow]) -> AssertionResult`（7 项；`ReinspectionScopeIn(incident_id, scope_sns: tuple[str], lot_size: int, inspection_type, inspection_level, aql)` 为 `types.py` 新增 frozen dataclass；上述 `lot_size` / `inspection_type` / `inspection_level` / `aql` 四值不在 `reinspections` / `quality_incidents` 表中，由 Task 8 放行 API 从 `module_inspections` 与 `samples` 关联取，domain 层不读库，只消费调用方传入值）
  - 判定语义：① 每个未关闭异常（status ∉ closed/rejected/scrapped）的 `incident_id` 必须有对应 scope 行，且其 `scope_sns` ⊇ 该异常关联 SN（由调用方传入，domain 不读库）；② `iso_lookup(type, level, aql, lot_size, iso_table)` 必须命中（None → Fail，理由「ISO 表无此组合」）；全过 → passed=True

- [ ] **Step 1: 改 `app/domain/types.py`（最小契约扩展）**

`ReinspectionView` 增 `scope_sns: tuple = field(default_factory=tuple)`；新增 `ReinspectionScopeIn`（frozen dataclass：`incident_id, scope_sns: tuple[str], lot_size: int, inspection_type: str, inspection_level: str, aql: float`）。不删不改既有字段（既有测试不破）。

- [ ] **Step 2: 写失败单测 `tests/domain/test_release.py` 追加**

必含用例：
- `test_scope_covered_pass`：scope 覆盖全部未关闭异常 SN + ISO 命中 → 第 7 项 passed
- `test_scope_missing_incident`：未关闭异常 INC-1 无对应 scope 行 → 第 7 项 Fail，reason 含 `INC-1`
- `test_scope_sn_subset`：scope_sns 缺少异常关联的某 SN → Fail，reason 列缺口 SN
- `test_scope_iso_missing`：`iso_lookup` 返回 None → Fail，reason 含「ISO」
- `test_closed_incidents_not_required`：已 closed 的异常无 scope 行也 Pass
- 既有 6 项用例全部补上新入参（空列表/空表），断言 `len(r.checks) == 7`

- [ ] **Step 3: 实现 `check_reinspection_scope_covered` + 改 `assert_release`**

纯函数：不读库不取时间；`iso_lookup` 来自 Phase 0 `app/domain/sampling.py`（import 同包，合规——domain 内互调允许，红线只禁 infra/langgraph/模型）。顺序加在 6 项之后为第 7 项。

- [ ] **Step 4: 全绿 + 覆盖率**

Run: `uv run pytest tests/domain/ -v` → PASS
Run: `uv run pytest tests/ --cov=app/domain --cov-report=term` → domain ≥ 90%
Run: `uv run ruff check . && uv run black --check . && uv run pytest -x` → 全绿

- [ ] **Step 5: dev-notes 节点 + 提交（契约扩展 PR 下半）**

dev-notes 追加「任务完成 · Task 2 断言第 7 项」节点。
```
git add app/domain/ tests/domain/ dev-notes/phase-b/
git commit -m "feat(contract): assert_release item 7 — reinspection scope covered (PB D7-7)"
```

**人工核验点（契约扩展 PR 整体 review）**：Task 1 + Task 2 合并作为一个 PR 级 review：冻结件改动最小化（仅加列 + 加 1 断言项 + 扩展 1 个 dataclass）、既有测试零回归、DDL 可回滚。review 通过后打 dev-notes「code review 结论」节点。

---

### Task 3: LLMClient Plugin + 归因夹具（L1/L1a）

**Files:**
- Create: `app/plugins/llm_client.py`
- Create: `tests/fixtures/mock_llm_attribution.json`
- Test: `tests/plugins/test_llm_client.py`

**Interfaces:**
- Consumes: 总 spec §9.1（`LLMClient` Protocol：`chat(prompt, schema) -> dict`；`ZHL_LLM_API_KEY` / `ZHL_LLM_BASE_URL` / `ZHL_LLM_MODEL` 环境变量）
- Produces:
  - `LLMClient(Protocol)`：`chat(prompt: str, schema: dict) -> dict`
  - `MockLLMClient(fixture_path: str | Path)`：从 prompt 中提取 `incident_id`（prompt 约定含 `"incident_id": "<id>"` 子串，用 `re` 提取），路由到 `mock_llm_attribution.json[incident_id]`；未命中 → `KeyError`
  - `OpenAICompatClient(api_key, base_url, model)`：httpx 调 OpenAI-compatible `/chat/completions`，`response_format={"type":"json_object"}`；测试不实例化（只测构造 + 参数校验）
  - 夹具结构（`mock_llm_attribution.json`）：`{ "<incident_id>": {"hypotheses": [ {cause, confidence, evidence_ids[], recommended_action}, ... ]} }`；至少 2 个 incident_id，其中一条假设 `evidence_ids=[]`（钉死 Review Focus #1 的门槛过滤输入）

- [ ] **Step 1: 写 `tests/fixtures/mock_llm_attribution.json`**

按上述结构；引用 `acceptance_incident.json` 的 `INC-001` + 一个样例四用的 incident id；数值与既有夹具一致（confidence 0.87/0.62/0.40 风格）。

- [ ] **Step 2: 写失败单测 `tests/plugins/test_llm_client.py`**

- `test_mock_routes_by_incident_id`：prompt 含 `"incident_id": "INC-001"` → 返回夹具 `INC-001` 的 dict
- `test_mock_unknown_incident_raises`：未知 id → `KeyError`
- `test_mock_returns_hypotheses_shape`：返回值含 `hypotheses` 列表，每条含 4 字段
- `test_protocol_conformance`：`MockLLMClient` 与 `LLMClient` runtime_checkable Protocol 兼容（`isinstance`）
- `test_openai_compat_client_requires_key`：空 key 构造 → `ValueError`

- [ ] **Step 3: 实现 `app/plugins/llm_client.py`**

Protocol + 两个实现；OpenAICompatClient 用 httpx（已在 lock 中）；Context7 先查 httpx 同步/异步用法（本项目 API 层同步即可，LLM 调用在事务外——AGENT.md §4.3；dev-notes 留痕）。

- [ ] **Step 4: 全绿 + 提交**

Run: `uv run pytest tests/plugins/test_llm_client.py -v` → PASS
Run: `uv run ruff check . && uv run black --check . && uv run pytest -x` → 全绿
dev-notes 节点 + `git commit -m "feat(plugins): LLMClient protocol + Mock/OpenAI-compat impl + attribution fixture (PB L1/L1a)"`

**人工核验点**：Mock 路由与门槛过滤输入（空 evidence_ids 假设）在位；冻结 `mock_llm_outputs.json` 未被改动（`git status` 验证）。

---

### Task 4: 轨迹 + 报告渲染 Plugin

**Files:**
- Create: `app/plugins/trace_store.py`
- Create: `app/plugins/report_renderer.py`
- Test: `tests/plugins/test_trace_store.py` / `tests/plugins/test_report_renderer.py`

**Interfaces:**
- Consumes: Phase 0 `app.infra.models`（`AgentTaskTrace`）；`EvidenceStore`（报告快照走 Task 7 的 Skill，renderer 不碰证据）
- Produces:
  - `TraceStore`：`record(session, *, task_id, batch_id, skill_name, input_ref, plugins_invoked, params_summary, model_version, structured_output, validation_result, failure_reason, human_action, started_at, finished_at=None) -> AgentTaskTrace`；`list_for_batch(session, batch_id) -> list[AgentTaskTrace]`（按 started_at 升序 = 轨迹时间线）
  - `ReportRenderer`：`render_release_report(context: dict) -> tuple[str, dict]`（Markdown, 结构化 JSON）；`render_incident_report(context: dict) -> tuple[str, dict]`；context 键固定（`batch` / `decisions` / `incidents` / `reinspections` / `assertions` / `refusal_reason` / `trace_refs` / `evidence_refs`），模板为函数内字符串模板（不引 Jinja2——spec 未锁定模板引擎，Markdown 由 f-string/`str.format` 拼，JSON 由 `json.dumps` 生成；此为我的默认拍板，评审可推翻）

- [ ] **Step 1: 写失败单测**

`test_trace_store.py`：
- `record` 落 SQLite 会话后 `list_for_batch` 按时间升序返回
- `validation_result` 默认 `partial`
`test_report_renderer.py`：
- `render_release_report` 输出 Markdown 含批次号 + 断言 7 项逐条 + 三层留痕引用；JSON 可 `json.loads` 且含 `assertions.checks` 7 条
- `render_incident_report` 含拒因章节（`refusal_reason` 非空时）
- 确定性：同一 context 两次渲染字节级相同（不取时间——`generated_at` 由调用方传入，renderer 不读时钟）

- [ ] **Step 2: 实现两个 Plugin + 全绿 + 提交**

dev-notes 节点 + `git commit -m "feat(plugins): trace store + report renderer (PB D1 三层留痕)"`

**人工核验点**：renderer 无时钟读取（确定性）；trace 顺序语义 = 轨迹时间线（Review Focus #3 的状态留痕基础）。

---

### Task 5: 归因 Skill + Traceability Plugin

**Files:**
- Create: `app/plugins/attribution.py`
- Create: `app/skills/attribution_skill.py`
- Test: `tests/plugins/test_attribution.py` / `tests/plugins/test_attribution_skill.py`

**Interfaces:**
- Consumes: Task 3 `LLMClient.chat`；Task 4 `TraceStore.record`；`EvidenceStore.get`（证据存在性校验，Phase 0）
- Produces:
  - `TraceabilityPlugin.validate_evidence(hypotheses: list[dict], evidence_store: EvidenceStore) -> list[dict]`：丢 `evidence_ids` 为空的假设（D2）；剩余按 `confidence` 降序重编号 `rank`（D3）；每假设增 `impact_scope`（`{"affected_sns": n, "cross_batch_hits": m}`，跨批次命中数由调用方经 `module_inspections` 查同 SN 传入——Plugin 不读库，参数 `cross_batch_counts: dict[str, int]`）
  - `AttributionSkill.run(incident, llm, plugin, trace, session) -> dict`：组 prompt（含 `incident_id` + 跨批次证据引用）→ `llm.chat` → `plugin.validate_evidence` → 写 `quality_incidents.root_cause_hypotheses`（JSONB）→ `trace.record`；LLM 调用在事务外，写库单事务（AGENT.md §4.3）

- [ ] **Step 1: 写失败单测（钉死 Review Focus #1）**

`test_attribution.py`：
- `test_evidence_gate_drops_empty`：三假设（0.87 带证据 / 0.62 带证据 / 0.40 空证据）→ 输出 2 条，rank 1/2，0.40 条不存在
- `test_confidence_only_orders`：confidence 乱序输入 → 输出 rank 按 confidence 降序
- `test_evidence_missing_in_store`：假设引用不存在的 evidence_id → 该假设丢弃（证据必须真存在，不只是非空——Review Focus #5）
`test_attribution_skill.py`（MockLLMClient + FileEvidenceStore + SQLite）：
- `test_skill_writes_hypotheses_and_trace`：运行后 `root_cause_hypotheses` 非空 + 轨迹行存在（`skill_name="attribution"`）
- `test_skill_llm_outside_transaction`：轨迹行 `params_summary` 记录 prompt 摘要（不记全文密钥信息）

- [ ] **Step 2: 实现 + 全绿 + 提交**

dev-notes 节点 + `git commit -m "feat(skills): attribution skill + traceability plugin — evidence gate + confidence rank (PB D2/D3)"`

**人工核验点**：门槛 + 排序两条语义各有钉死用例；冻结夹具 `mock_llm_outputs.json` 未动。

---

### Task 6: LangGraph 子图 + 异常 API

**Files:**
- Create: `app/workflow/incident_subgraph.py`
- Create: `app/api/incidents.py`
- Modify: `pyproject.toml`（新增 `langgraph` 依赖）
- Test: `tests/workflow/test_incident_subgraph.py` / `tests/api/test_incidents.py`

**Interfaces:**
- Consumes: Task 5 `AttributionSkill`；Task 2 状态机/断言；Phase 0 `incident_sn_links` / `QualityIncident` 模型；`langgraph`（主包，`StateGraph`/`interrupt`/`Command`）+ `langgraph-checkpoint-postgres`（`PostgresSaver`）+ `MemorySaver`（测试）
- Produces:
  - `IncidentSubgraphState`（TypedDict）：`incident_id, batch_id, hypotheses, human_decision, reinspection_result, reinspection_payload`
  - 节点：`generate_hypotheses`（调 Skill，写状态）→ `validate_evidence`（确定性过滤，已在 Skill 内完成，节点做状态搬运 + 写轨迹）→ `human_adopt`（`interrupt()` 三选一，payload 为假设列表）→ `run_reinspection`（从状态取 `reinspection_payload`；复检能力接口预留，本任务用 stub callable 占位，Task 7 接线替换）；`route_reinspection_result`：pass → END（异常 `closed` 由 API 层驱动，子图只出结果），fail → `rejected_route` 节点（写状态标记，API 层转 `rejected`）
  - `build_incident_subgraph(checkpointer) -> CompiledGraph`；`main_chain` 挂载点常量 `INCIDENT_ENTRY = "incident_subgraph"`（`main_chain_graph` 由集成阶段建，本任务只保证子图可独立编译 + 回放）
  - 异常 API（`app/api/incidents.py`，FastAPI Router）：
    - `POST /api/incidents`：创建（`affected_sns` 非空 + 经 `incident_sn_links` 写关联 + `evidence_ids` 存在性校验 400 + 批次必须 `isolated` 否则 409；异常状态 `new`）
    - `POST /api/incidents/{id}/assign`：`{owner, due_at, containment_action}` → `transition("isolated"→"assigned")`（前置 owner_assigned）+ 批次 `isolated→disposing`（前置 assigned_owner/due_at_set）双转移单事务 + 轨迹
    - `POST /api/incidents/{id}/capa`：`{confirmed_root_cause, corrective_action, verified}`（L3：`verified` 仅当复检记录存在且 result=pass 时可置 True，否则 400；写 `capa_verified` 列）→ 异常 `assigned→pending_recheck` + 批次 `disposing→pending_recheck` 单事务 + 轨迹
    - `GET /api/incidents` / `GET /api/incidents/{id}`

- [ ] **Step 0: 执行 `uv add langgraph`，确认 `uv.lock` 更新，dev-notes 留痕（AGENT.md §2 依赖一律 `uv add`）**

说明：`StateGraph` / `interrupt` / `Command` 均来自 `langgraph` 主包，当前 `uv.lock` 仅有 `langgraph-checkpoint` 系列，主包缺失会直接导致 Task 6 import 失败。`uv add langgraph` 后确认 `uv.lock` 更新，提交（AGENT.md §10 依赖变更随 lock 一并提交）。

- [ ] **Step 1: Context7 先查 LangGraph（StateGraph 子图挂载 / interrupt payload 约束 / PostgresSaver 用法），dev-notes 留痕**

- [ ] **Step 2: 写失败单测**

`test_incident_subgraph.py`（MemorySaver + 固定 seed）：
- `test_subgraph_interrups_at_adopt`：跑到 `human_adopt` 暂停，`graph.get_state(config)` 显示 `next` 含该节点，interrupt payload = 假设列表
- `test_resume_adopt_closes_path`：`Command(resume={"decision":"adopt","cause":...})` 恢复 → 状态含采纳结果 → 到 END
- `test_recheck_pass_routes_closed` / `test_recheck_fail_routes_rejected`
`test_incidents.py`（httpx `ASGITransport` + SQLite 会话工厂）：
- 创建 201 + `incident_sn_links` 行数 = `len(affected_sns)`
- `evidence_ids` 含不存在 id → 400
- 批次非 `isolated` → 409
- assign：异常 + 批次双转移成功；重复 assign → 409
- capa `verified=true` 但无复检记录 → 400；有 pass 复检 → 200 + `capa_verified` 落列
- GET 详情含 `root_cause_hypotheses`

- [ ] **Step 3: 实现 + 全绿 + 提交**

dev-notes 节点 + `git commit -m "feat(workflow+api): incident subgraph (interrupt) + incident/capa endpoints (PB D9)"`

**人工核验点**：interrupt/resume 回放测试通过；异常五态 + 批次状态双驱动事务边界正确（单事务双转移）；LLM 未进事务。

---

### Task 7: 复检 API + 报告 Skill/API（含自动归档）

**Files:**
- Create: `app/api/reinspections.py`
- Create: `app/skills/report_skill.py`
- Create: `app/api/reports.py`
- Test: `tests/api/test_reinspections.py` / `tests/api/test_reports.py`

**Interfaces:**
- Consumes: Task 1 新列；Task 2 断言；Task 4 `TraceStore` / `ReportRenderer`；Phase 0 `EvidenceStore`
- Produces:
  - `POST /api/incidents/{id}/reinspections`：请求体含 `scope_type`/`scope_sns`（缺省 = `minimal` + 异常 `incident_sn_links` 全部 SN，服务端计算）；`expanded`/`full_lot` 必须传 `scope_sns` 且 ⊇ minimal 集（否则 400 列缺口）；扩大时 `params_summary.scope_expansion=true` 写轨迹；独立 `Reinspection` 行（不改 `module_inspections`）；回写 `quality_incidents.verification_id`；result=fail → 异常 `pending_recheck→rejected` + 批次 `pending_recheck→rejected`（状态机）单事务 + 轨迹；**响应体** `{reinspection_id, incident_id, sn, passed, scope_applied}`，其中 `sn` 由 `inspection_id` 关联 `module_inspections.sn` 查出，非 `reinspections` 表直连字段
  - `GET /api/incidents/{id}/reinspections`
  - `ReportSkill.generate(session, batch_id, report_type) -> Report`：聚合 context（批次 + 判定统计 + 异常 + 复检 + 断言 + 三层留痕引用）→ `ReportRenderer` 渲染 → 写 `reports` 行（version = 该 batch+type 最大 version + 1；幂等：同 type 已有版本且批次未变 → 返回现有最新版不新增，D10）→ 写 `evidence`（`report_snapshot`）→ 写轨迹
  - `POST /api/batches/{id}/report`：`{report_type}`；`rejected` 批次只允许 `incident` 型（409 否则）；`archived` 批次 409（D8）；写报告后**同事务**检查自动归档（D6a）：批次状态 ∈ {released, rejected} 且目标 type 版本 ≥ 1 → `transition(batch, "archived")`（状态机 `("rejected","archived")` / `("released","archived")` 前置为空，Phase 0 已有边）+ 轨迹
  - `GET /api/batches/{id}/reports` / `GET /api/batches/{id}/reports/{report_id}`

- [ ] **Step 1: 写失败单测**

`test_reinspections.py`：
- 缺省 minimal：`scope_sns` 服务端填 = 异常全部 SN，`scope_type="minimal"`
- `expanded` 不传 `scope_sns` → 400；传了但不覆盖 minimal → 400 列缺口
- 创建后 `reinspections` 行独立 + `verification_id` 回写 + `module_inspections` 原始值未变（DB 断言）
- fail 结果 → 异常 + 批次 `rejected`
`test_reports.py`：
- released + release 报告：201，version=1；重复调 → 200 幂等返回 version=1（D10）
- rejected + incident 报告：201；rejected 调 release 型 → 409（D7）
- **自动归档**（D6a/D8）：released 批次写完 release 报告后 `delivery_batches.status == "archived"` 同事务内完成；archived 批次再调报告 API → 409
- 报告 JSON 含 7 项断言 + 三层留痕引用（`agent_task_id` / evidence id / report id 三链）

- [ ] **Step 2: 实现 + 全绿 + 提交**

dev-notes 节点 + `git commit -m "feat(api+skills): reinspection scope API + report skill w/ auto-archive (PB D5/D6/D6a/D7/D10)"`

**人工核验点**：范围校验 400 语义（Review Focus #2 的 API 面）；自动归档在报告写事务内（Review Focus #4）；幂等版本不增。

---

### Task 8: 放行 API（7 项断言 + 失败不改状态）

**Files:**
- Create: `app/api/release.py`
- Test: `tests/api/test_release.py`

**Interfaces:**
- Consumes: Task 2 `assert_release`（7 项）；Task 4 轨迹；Phase 0 `SampleConclusion` / `Reinspection` 模型
- Produces:
  - `POST /api/batches/{id}/release`：`{approved_by}`；服务端组装 7 项入参（批次状态 / 最新抽样结论 / 未关闭异常 / 隔离 SN / 必备证据 / CAPA verified / **复检范围行（按未关闭异常的 incident_id 聚出 ReinspectionScopeIn）+ ISO 表**）→ `assert_release` → **全通过**：写放行证据（`evidence`）+ 批次 `pending_release→released`（前置 assertions_passed/human_approved）+ 轨迹，**同事务内触发自动归档检查**（复用 Task 7 的归档函数——released 此时无报告，归档由后续报告 API 完成；即本事务只到 released，归档留给 Task 9 的报告调用——PB spec D6a 语义「报告生成完成即归档」；注意与 D4「断言失败不改状态」对齐：本端点只在全通过时改状态）；**任一未通过**：批次停留 `pending_release`，响应 200 + `AssertionResult`（逐条 + 未满足项 reason 引用 incident/SN/evidence id），**不调 transition、不写状态变更、不写轨迹中的状态转移**（轨迹记「assertion failed, retriable」）
  - 批次非 `pending_release` → 409；`archived` → 409（D8）

- [ ] **Step 1: 写失败单测（钉死 Review Focus #2 + #3 + #4）**

- `test_release_all_pass_releases`：全过 → `status="released"` + 放行证据行 + 轨迹；此时未归档（无报告）
- `test_release_assertion_fail_keeps_state`：人为造未 verified CAPA → 调放行 → 批次仍 `pending_release`、`release_decision` 仍 `undecided`、`reinspection_scope_covered` 或 `all_capa_verified` 项 reason 指明缺口
- `test_release_scope_gap_blocks`：复检只覆盖 1/3 SN → 第 7 项 Fail，reason 列缺的 SN
- `test_release_archived_409` / `test_release_wrong_state_409`
- 重试语义：阻断处置后（补 CAPA verified）再调 → 全过 → released

- [ ] **Step 2: 实现 + 全绿 + 提交**

dev-notes 节点 + `git commit -m "feat(api): release endpoint — 7-item assertions, fail-keeps-state (PB D4)"`

**人工核验点**：断言失败零状态写入（DB 断言）；第 7 项 reason 可读（含 incident/SN）；重试幂等。

---

### Task 9: FastAPI app 装配 + archived 终态守卫

**Files:**
- Create: `app/api/main.py`（暴露 `create_app() -> FastAPI`）
- Create: `app/main.py`（`app = create_app()`，`uv run uvicorn app.main:app` 入口，AGENT.md §5）
- Test: `tests/api/test_main_smoke.py`

**Interfaces:**
- Consumes: Task 6/7/8 全部 Router
- Produces:
  - `app.api.main.create_app() -> FastAPI`：挂 `incidents` / `reinspections` / `release` / `reports` Router + 静态目录 `app/static`（Task 10 的页面）+ `archived` 守卫依赖（所有写端点前置查批次状态：`archived` → 409 `{"error":"batch archived, terminal state"}`，D8；用 FastAPI 依赖 `require_not_archived(batch_id)` 注入各路由）
  - `app.main` 模块别名（`uv run uvicorn app.main:app` 可用，AGENT.md §5 演示命令入口）

- [ ] **Step 1: 写失败冒烟测试**

- 创建 app + `TestClient`（httpx ASGITransport）：四个 Router 的 GET 端点 200
- archived 批次：异常/复检/放行/报告 4 个写端点全部 409
- 非 archived：守卫不拦截

- [ ] **Step 2: 实现 + 全绿 + 提交**

`app/api/main.py` 实现 `create_app()`；`app/main.py` 一行 `from app.api.main import create_app; app = create_app()`。

dev-notes 节点 + `git commit -m "feat(api): app assembly + archived terminal-state guard (PB D8)"`

**人工核验点**：409 守卫覆盖全部写端点（Review Focus #4）；`uv run uvicorn app.main:app --reload` 可起（不起服务只验 import + TestClient）。

---

### Task 10: 静态页 + 样例三/四 E2E + 交付收口

**Files:**
- Create: `app/static/incident.html` / `assertion.html` / `report.html` / `trace.html`
- Create: `tests/e2e/test_sample3_incident_closure.py` / `tests/e2e/test_sample4_consistency.py`
- Modify: `dev-notes/phase-b/agnes-20261011.md`（finish 节点）

**Interfaces:**
- Consumes: Task 1–9 全部
- Produces: 4 个单 HTML + 原生 JS fetch 页面（异常详情：假设排序含 evidence 链接 + CAPA 表单 + 复检记录；放行断言：7 项逐条 + 阻断原因；报告：版本列表 + Markdown 渲染 + JSON 下载 + 三层留痕引用；轨迹：`agent_task_traces` 时间线）；E2E 演示命令

- [ ] **Step 1: 写 4 个静态页**

每页一个 HTML 文件，fetch `/api/...`，无构建链（总 spec §9.9）；断言页展示第 7 项（`reinspection_scope_covered`）与其他 6 项同格式。

- [ ] **Step 2: 写 E2E（样例三 + 样例四，mock 驱动，不依赖 Phase A）**

`test_sample3_incident_closure.py`（消费 `acceptance_incident.json` + `mock_llm_attribution.json`）：
- 批次 `isolated` → 创建异常 → 归因（Mock）→ assign → 复检（minimal 范围，pass）→ CAPA（verified=true）→ 批次 `pending_release` → 放行（7 项全过，含第 7 项）→ `released` → 报告（incident+release 由终态决定 release 型）→ 自动 `archived`
- 每步断言：三层留痕行存在（轨迹/证据/报告）+ 状态机边合法（domain `transition` 预演）
- 中途阻断场景：CAPA 未 verified 时放行 → 批次停留 + 第 6/7 项 reason 可读（Review Focus #2/#3 的 E2E 面）
`test_sample4_consistency.py`（消费 `acceptance_consistency.json`）：
- 同 SN 跨批次 → 一致性异常（`data_consistency`）→ 两批次均被断言阻断（第 4/7 项）→ 不可放行 → 处置报告 → `rejected` → 归档

- [ ] **Step 3: 演示命令 + 全量验证**

Run: `docker start zhl-pg && uv run pytest tests/ -v`（E2E 用 SQLite 内存库可脱离容器；DDL 验证走容器）
Run: `uv run pytest --cov=app --cov-report=term`（plugins/skills ≥ 70%；domain 不降 90%）
Run: `uv run ruff check . && uv run black --check . && uv run pytest -x` 全绿

- [ ] **Step 4: dev-notes finish 节点 + 提交**

finish 节点含：交付物清单 + 测试结果（pass/fail/skip 计数）+ 演示命令 + 已知遗留。
```
git add app/static/ tests/e2e/ dev-notes/phase-b/
git commit -m "feat(e2e+ui): 4 static pages + sample3/4 closure E2E + delivery wrap-up (PB B-8)"
```

**人工核验点（Phase B finish 评审，AGENT.md §5）**：
1. 功能演示命令可复现（含 DB 初始化 + 种子 + 样例驱动）
2. 全量测试结果输出
3. dev-notes 完整（每节点四要素）
4. 已知遗留清单

---

## 执行顺序与依赖图

```
Task 1 ─→ Task 2 ─┬─→ Task 6 ─→ Task 7 ─→ Task 8 ─→ Task 9 ─→ Task 10
      (契约 PR)   │
              Task 3 ─→ Task 5 ─┘
              Task 4 ────────→ Task 5 / 6 / 7 / 8
```

Task 3/4 可与 Task 1/2 并行（无依赖）；Task 6 需 Task 2 + 4 + 5；Task 7 需 Task 4 + 6；Task 8 需 Task 2 + 6（Task 7 的归档函数在 Task 9 前就位即可，Task 8 的「released 未归档」语义不依赖 Task 7 代码，仅依赖状态机边——Phase 0 已有）。

## 提交规范

- 一个提交一个逻辑变更；message 说清「为什么改」（AGENT.md §3.4）
- 每提交前 `uv run ruff check . && uv run black --check . && uv run pytest -x` 全绿
- `dev-notes/phase-b/` 随对应任务提交（不攒收尾补记）
- `uv.lock` 随依赖变更提交（Task 6 Step 0 `uv add langgraph` 后 lock 更新，随该任务提交）

## 已知遗留（预判，finish 时逐条对账）

- `main_chain_graph` 完整五段主链图属**集成阶段**（总 spec §4），Phase B 只交付 `incident_subgraph` + 挂载点常量；`pending_judge` 段的路由由 Phase A 提供
- 真实 LLM 接入（OpenAICompatClient 实调）不进 P0 验收，用 Mock；演示命令固定 Mock
- 静态页非生产级 UI（总 spec §9.9），仅演示与验收
