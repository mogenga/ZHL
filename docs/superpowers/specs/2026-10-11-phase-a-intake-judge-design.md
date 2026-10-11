# Phase A · 数据入场 + 规则判定设计

日期：2026-10-11
定位：Phase A 的可执行开发规格。上游为 `2026-10-10-zhl-system-spec.md`（总 spec）、`2026-10-10-phase0-shared-contract-design.md`（P0 spec，已冻结）与 AGENT.md；本文把 Phase A 范围收敛为可直接写 plan 的实现规格。

---

## 1. 意图与为什么

Phase A 是五段主链的前两段（数据入场 + 规则判定）：批次从 `pending_intake` 推进到 `pending_judge` / `pending_release` / `isolated`，含完整判定证据。Phase B（异常闭环 + 报告归档）与本 Phase 并行开发，通过 P0 冻结契约（`Decision` / `IncidentTrigger` / `AssertionResult`）交换数据。

Phase 0 已完成并冻结（`phase0-frozen` tag）：domain 纯函数（judge / state_machine / release / sampling）、12 表 SQLAlchemy 模型、EvidenceStore Protocol 骨架、测试夹具（四组验收样例 + 规则种子 + ISO 表 + MockLLM 输出）。Phase A 在这些契约之上做「接线」：API 路由、LangGraph 图编排、Skill/Plugin 实现、证据落库。

## 2. 范围

### 2.1 Goals（Phase A 交付物）

1. **批次 API**：`POST /api/batches`（建草稿 + draft→pending_intake 转移）、`GET /api/batches/{id}`（批次详情，含判定证据 + 抽样结论 + 状态时间线）
2. **Intake API**：`POST /api/intake`（文件上传 / 内联数据摄入）、`POST /api/intake/{task_id}/confirm`（人工确认/修改/拒绝）
3. **抽取 Skill**：读 CSV / 模拟设备数据 → 调 `MockLLMClient` 抽取字段 + 置信度 → 写 `extraction_result` 证据
4. **判定 API + Skill**：`POST /api/batches/{id}/judge` → 逐组件判定 + 批次状态转移
5. **抽样 Plugin**：`iso_lookup` + `freeze_sample` 封装为 Skill 节点，写 `samples` + `sample_conclusions` 表
6. **LangGraph 图**：`main_chain_graph` 的「数据入场→规则判定」段（intake / extract / human_confirm / judge / sample / batch_transition 六节点）
7. **Checkpointer**：PostgreSQL 后端（`langgraph-checkpoint-postgres`），`thread_id = batch_id`
8. **证据落库**：`EvidenceStore` 的 DB 写入实现（`evidence` 表 INSERT）
9. **测试**：图回放测试（内存 Checkpointer，不依赖 DB）+ API 集成测试（httpx + PG Checkpointer）

### 2.2 Non-goals（Phase A 不做）

- 不做五段主链第 3–5 段（归因 / 复检 / 报告）——Phase B
- 不建 `incident_subgraph`——Phase B；Phase A 只在 `main_chain_graph` 上预留「已隔离」分支挂载点
- 不实现 `release` API / 报告 API / 异常 API / 复检 API——Phase B
- 不接真实 LLM 服务（P1）；Phase A 仅用 `MockLLMClient`（读固定 JSON 夹具）
- 不做认证 / 多角色（总 spec §9.8）

### 2.3 Anti-goals（做错了算失败）

- LangGraph 节点内部写业务判断（判定 / 状态转移 / 断言逻辑必须在 domain 纯函数，节点只做编排与 IO）
- 在节点内直接写库绕过 `evidence_writer` / Plugin 契约（AGENT.md §7.2）
- 人工 interrupt 点超过三处（低置信字段确认是 Phase A 唯一 interrupt；归因假设 / 放行批准属 Phase B）
- 确定性路径（断言失败 / SN 冲突 / 规则缺失）用 interrupt 而非直接路由
- 修改 P0 冻结项（`app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/`）——必须走 PR + review
- 模型调用（MockLLMClient）放在数据库事务内（AGENT.md §4.3）

## 3. 约束（全部来自上游，逐条引用）

### 3.1 技术栈（总 spec §3.1 锁定）

Python 3.11+ / FastAPI / PostgreSQL 15（Docker 容器）/ SQLAlchemy 2.0 / Pydantic v2 / LangGraph / langgraph-checkpoint-postgres / pytest + httpx。Phase A 新增依赖：`langgraph` + `langgraph-checkpoint-postgres`（走 `uv add`）。禁止引入 spec 之外的新库（AGENT.md §2.3）。

### 3.2 分层依赖（总 spec §3.2 + AGENT.md §3.2）

```
FastAPI 路由 → LangGraph 节点（编排 + IO）→ Skill / Plugin → domain 纯函数
                                                    ↓
                                               数据层（SQLAlchemy + Postgres）
                                                    ↓
                                               Checkpointer（langgraph-checkpoint-postgres）
```

- 节点是 Skill/Plugin 的封装壳，不直接写业务判断
- 跨层调用通过 Protocol 接口
- 不直接写库：节点通过 `TaskContext.evidence_writer` 写入证据，与 Plugin 契约一致（AGENT.md §7.2）

### 3.3 domain 纯度（总 spec §3.2 + AGENT.md §8）

判定 / 断言 / 状态机纯函数：不读库、不取时间、不调随机、不调模型、不 import `sqlalchemy` / `app.infra.*` / `langgraph`。Phase A 的 Skill/Plugin 调用 domain 纯函数，节点封装在 workflow 层。

### 3.4 判定顺序（总 spec §2.3）

① 基础校验 → ② 功率范围 → ③ 参数容差（闭区间）→ ④ 电流分档 → ⑤ 外观。边界值：等于上下限均 Pass。

### 3.5 状态机（总 spec §2.5）

批次 10 态，转移表写死在 domain（P0 已冻结），Phase A 消费 `transition()` 不修改。

### 3.6 事务边界（AGENT.md §4.3）

- 摄入一批组件：单事务写全部记录 + 证据，任一失败整批回滚
- 判定一个批次：单事务写全部组件判定 + 抽样结论 + 批次状态
- 状态转移：单事务校验前置条件 + 更新状态 + 写轨迹
- 模型调用在事务外完成，拿到结构化结果后再开事务落库

### 3.7 测试（AGENT.md §3.3）

- domain 单测 ≥ 90%（P0 已达成，Phase A 不改）
- plugins 集成测试 ≥ 70%（DB mock 或 testcontainers）
- skills 回放测试 ≥ 60%（固定模型输出）
- 端到端：四组验收样例全通过
- 图回放测试：内存 Checkpointer，不依赖 DB
- 集成测试：PG Checkpointer + FastAPI TestClient

### 3.8 冻结项（AGENT.md §6.1）

`app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/` 已冻结。Phase A 若发现契约需调整，必须走 PR + review，不允许直接改。

### 3.9 uv 环境（AGENT.md §10）

所有 Python 工具命令一律 `uv run`；新增依赖走 `uv add`（`langgraph` + `langgraph-checkpoint-postgres`）；`uv.lock` 必须提交。

## 4. 关键决策（含我拍板的项，标记 *；用户拍板项标注「用户确认」）

> 带 * 的是我在 spec 之外自行拍板的默认值，依据上游推导；标注「用户确认」的是用户在 brainstorming 中明确拍板的项。如有异议请在评审时指出。

### 4.1 用户拍板项（Ruling）

| # | 决策 | 依据 |
| --- | --- | --- |
| R1（用户确认） | Phase A 做成 LangGraph 图，只做批次主线段（intake / extract / human_confirm / judge / sample / batch_transition 六节点）；`incident_subgraph` 整个留给 Phase B，Phase A 只在 `main_chain_graph` 上预留「已隔离」分支挂载点 | 总 spec §3.2.1 |
| R2（用户确认） | `thread_id = batch_id`，Checkpointer 用 PostgreSQL 后端（`langgraph-checkpoint-postgres`）；测试策略：先用内存 Checkpointer 跑图回放（不依赖 DB），集成测试再挂 PG Checkpointer | 总 spec §3.2.1 / §7.2 |
| R3（用户确认） | 每个节点是确定性封装：同输入同输出，内部调 Skill/Plugin，节点本身不写业务判断 | 总 spec §3.2.1 约束 |
| R4（用户确认） | `MockLLMClient` 不算真实模型调用；P0 不需要任何 LLM 环境变量（`ZHL_LLM_API_KEY` / `BASE_URL` / `MODEL` 仅 P1 需要）；`extract` 节点调 `MockLLMClient` 读固定 JSON 夹具（`tests/fixtures/mock_llm_outputs.json`），行为确定可回放，写入 `evidence`（`extraction_result`） | 总 spec §9.1 |
| R5（用户确认） | 批次创建放 Phase A：`POST /api/batches` 建草稿 + `draft→pending_intake` 转移；`intake` 前提是批次已存在；批次详情 API 也在 Phase A | 总 spec §9.3 批次创建 |
| R6（用户确认） | Phase A 不做五段主链第 3–5 段（归因 / 复检 / 报告），那是 Phase B | 总 spec §4 |

### 4.2 我拍板的默认项（标记 *）

| # | 决策 | 依据 |
| --- | --- | --- |
| A1 * | `main_chain_graph` 六节点：`intake`（写 `module_inspections` 行 + `source_file` 证据）→ `extract`（调 MockLLMClient → 写 `extraction_result` 证据）→ `human_confirm`（`interrupt()`，等 `Command(resume=...)` 恢复；修改字段值写入 `module_inspections`，`intake_status=confirmed`；拒绝则 `intake_status=rejected`）→ `judge`（调 domain `judge()` 逐组件判定 + 写 `decision_evidence`）→ `sample`（调 domain `iso_lookup` + `freeze_sample`，写 `samples` + `sample_conclusions`）→ `batch_transition`（调 domain `transition()` 推进批次状态） | 总 spec §3.2.1 节点映射 |
| A2 * | `intake` 节点接收 CSV 文件上传（`multipart/form-data`）或内联 JSON；`source_type` 按文件扩展名 / 请求体推断（`IV_CSV` / `MANUAL` / `SIMULATED_DEVICE`） | 总 spec §9.7 人工确认 API |
| A3 * | 抽取 Skill 的置信度阈值：`confidence < 0.8` 时 `human_confirm` 节点 `interrupt()`，等人工确认；`confidence >= 0.8` 时自动确认（`intake_status=confirmed`，`human_diff=[]`） | 总 spec §9.7；需求 §4.1 FR-1.5 |
| A4 * | `judge` 节点要求批次内全部组件 `intake_status=confirmed`，否则返回 400 列出未确认 SN（总 spec §9.6） | 总 spec §9.6 |
| A5 * | 判定后批次状态转移：全部 Pass + 抽样 Pass → `pending_release`；任一 Fail / 数据冲突 / 规则缺失 → `isolated`（总 spec §9.6） | 总 spec §9.6 / §2.5 |
| A6 * | 抽样 Plugin 在 `sample` 节点执行，查 `iso_sampling_table`（只读配置表，P0 种子导入）；缺项 → `sample_conclusions.result = 'rule_missing'`，批次转移走 `isolated` 分支 | 总 spec §9.4 / P0 spec D9 |
| A7 * | 证据落库：`EvidenceStore` 的 `save()` 在 Phase A 补充 DB 写入（`evidence` 表 INSERT），`FileEvidenceStore` 构造函数 `db` 参数注入 SQLAlchemy `Session` | P0 spec D11 / §5.4 |
| A8 * | 批次详情 API 返回：批次基本信息 + 全部组件判定结果（`decision_evidence` 展开）+ 抽样结论 + 状态时间线（`agent_task_traces` 按 `batch_id` 查询） | 总 spec §9.9 页面 2 |
| A9 * | LangGraph 图构建：`main_chain_graph` 用 `StateGraph(BatchWorkflowState)`，六节点 + 条件路由（`human_confirm` 后根据 `intake_status` 路由；`judge` 后根据判定结果 + 抽样结论路由到 `pending_release` 或 `isolated` 分支） | 总 spec §3.2.1 |
| A10 * | `BatchWorkflowState`（TypedDict）：`batch_id` / `batch_status` / `inspections: list[dict]` / `extraction: dict` / `confidence: float` / `decision: dict` / `sample: dict` / `transition_result: dict` / `evidence_ids: list[str]` | 总 spec §3.2.1 |
| A11 * | 测试分层：① 图回放测试（`InMemorySaver`，固定 MockLLMClient 输出，断言节点序列 + 状态转移）；② Plugin 集成测试（`FileEvidenceStore` + DB mock）；③ API 集成测试（FastAPI `TestClient` + `httpx`，PG Checkpointer 可选） | AGENT.md §3.3 |
| A12 * | 人工确认 API：`POST /api/intake/{task_id}/confirm`（确认 / 修改 / 拒绝），对应 `human_confirm` 节点的 `interrupt()` 恢复点（总 spec §9.7） | 总 spec §9.7 |

## 5. 产出物规格

### 5.1 新增 / 修改文件清单

| 文件 | 责任 | 说明 |
| --- | --- | --- |
| `app/api/__init__.py` | 包初始化 | 新建 |
| `app/api/batches.py` | `POST /api/batches` / `GET /api/batches/{id}` / `POST /api/batches/{id}/judge` | 批次创建 + 详情 + 判定触发 |
| `app/api/intake.py` | `POST /api/intake` / `POST /api/intake/{task_id}/confirm` | 摄入 + 人工确认 |
| `app/workflow/__init__.py` | 包初始化 | 新建 |
| `app/workflow/main_chain.py` | `main_chain_graph` 构建（六节点 + 条件路由） | R1 / A1 / A9 |
| `app/workflow/state.py` | `BatchWorkflowState` TypedDict | A10 |
| `app/workflow/checkpointer.py` | Checkpointer 配置（内存 / PG 双模式） | R2 |
| `app/skills/__init__.py` | 包初始化 | 新建 |
| `app/skills/extract.py` | 抽取 Skill（调 MockLLMClient → 写 evidence） | R4 |
| `app/skills/judge.py` | 判定 Skill（调 domain `judge()` → 写 `decision_evidence`） | A4 / A5 |
| `app/plugins/__init__.py` | 包初始化（已有） | |
| `app/plugins/sampling.py` | 抽样 Plugin（`iso_lookup` + `freeze_sample` 封装） | A6 |
| `app/plugins/evidence_store.py` | 补充 DB 写入（`evidence` 表 INSERT） | A7 |
| `app/plugins/llm_client.py` | `MockLLMClient`（读 `tests/fixtures/mock_llm_outputs.json`） | R4 |
| `app/infra/repositories.py` | 仓库层（批次 / 检验 / 抽样 CRUD + EXCLUDE 兜底检查） | P0 spec §7 留待 builder |
| `tests/workflow/` | 图回放测试（InMemorySaver） | A11 |
| `tests/plugins/` | Plugin 集成测试（补充 sampling / evidence_store DB 写入） | A11 |
| `tests/api/` | API 集成测试（FastAPI TestClient） | A11 |

### 5.2 `main_chain_graph` 节点规格（R1 / A1）

```
intake_node
  输入：batch_id + 文件上传 / 内联 JSON
  动作：解析 CSV / JSON → 写 module_inspections（intake_status=pending）
        + 写 evidence（source_file）
  输出：inspections: list[dict]（含 sn / pmax / temp / ... / appearance_result）

extract_node
  输入：inspections + batch_id
  动作：调 MockLLMClient.chat(prompt, schema) → 结构化抽取结果
        + 写 evidence（extraction_result）
  输出：extraction: dict + confidence: float

human_confirm_node（interrupt() 点，R3；Q1 / Q3）
  触发条件：confidence < 0.8（confidence = min(N 个 SN 的 extraction confidence)，Q3）
  动作：interrupt() 挂起，等 Command(resume={action, fields})（API 侧见 §5.6，Q1）
        低置信 SN 列表记录在 state.low_conf_sns（Q3），供确认界面展示
        action=confirm → fields 可修改任意 SN 的字段值（Q3），整批 intake_status=confirmed
        action=reject  → 整批 intake_status=rejected（Q3），保留抽取证据
  输出：intake_status: str（整批统一，Q3）

judge_node
  前置：批次内全部组件 intake_status=confirmed（A4）
  动作：对每个组件调 domain judge() → Decision
        + 写 module_inspections.decision_evidence + rule_snapshot
        + 写 evidence（decision_snapshot）
  输出：decision: dict（含 result / counts / per-SN checks）

sample_node
  输入：batch_id + lot_size + rule_version_id + sample_plan
  动作：调 domain iso_lookup() → 缺项则 rule_missing
        调 domain freeze_sample() → 冻结样本
        + 写 samples + sample_conclusions
        + 写 evidence（decision_snapshot）
  输出：sample: dict（n / ac / re / result）

batch_transition_node
  输入：judge 结果 + sample 结果 + 当前批次状态
  动作：调 domain transition() 推进批次状态
        + 写 agent_task_traces（轨迹留痕）
        + 写 evidence（task_trace）
  输出：batch_status: str + transition_result: dict
```

### 5.3 条件路由（A9）

```
intake → extract → human_confirm → [intake_status == confirmed] → judge → sample → batch_transition
                                         → [intake_status == rejected] → END
judge → [all pass + sample pass] → batch_transition → END（pending_release）
judge → [any fail / rule missing] → batch_transition → END（isolated，预留 incident_subgraph 挂载点）
```

### 5.4 `BatchWorkflowState`（A10）

```python
class BatchWorkflowState(TypedDict):
    batch_id: str
    batch_status: str
    inspections: list[dict]
    extraction: dict
    confidence: float          # Q3: min(N 个 SN 的 extraction confidence)
    low_conf_sns: list[str]    # Q3: confidence < 0.8 的 SN 列表，供确认界面展示
    intake_status: str         # Q3: 整批统一（pending / confirmed / rejected）
    decision: dict
    sample: dict
    transition_result: dict
    evidence_ids: list[str]
```

### 5.5 Checkpointer 配置（R2）

| 场景 | 实现 | 说明 |
| --- | --- | --- |
| 图回放测试 | `InMemorySaver`（langgraph.checkpoint.memory） | 不依赖 DB，纯内存 |
| 集成 / 演示 | `PostgresSaver`（langgraph-checkpoint-postgres） | `thread_id = batch_id`，连接串读 `ZHL_DATABASE_URL` |
| 配置注入 | `checkpointer.py` 提供 `get_checkpointer(mode="memory" | "pg")` | 工厂函数，测试注入 memory，生产注入 pg |

### 5.6 人工确认 API（A12 / 总 spec §9.7 / Q1 / Q2 / Q3）

- `POST /api/intake/{task_id}/confirm`：`task_id` = `thread_id` = `str(batch_id)`（Q2）；body `{action: "confirm" | "reject", fields?: dict}`
  - `action=confirm`：`fields` 为修改后的字段值（可选，未修改则 `human_diff=[]`；可修改任意 SN，Q3）；写 `module_inspections`，`intake_status=confirmed`（整批统一，Q3）；写 `evidence`（`human_confirmation`）；随后 `Command(resume=payload)` 恢复图（human_confirm→judge→sample→batch_transition 跑到底，Q1）
  - `action=reject`：`intake_status=rejected`（整批统一，Q3）；保留抽取证据；写 `evidence`（`human_confirmation`）；恢复图后路由到 END
- `POST /api/intake` 触发整图从 `intake` 节点跑起，至 `human_confirm` 的 `interrupt()` 挂起（Q1）
- `POST /api/batches/{id}/judge` 为独立判定 Skill 调用（不经图，Q1）

### 5.7 MockLLMClient（R4）

```python
class MockLLMClient:
    """Reads fixed JSON from tests/fixtures/mock_llm_outputs.json.
    
    chat(prompt, schema) -> dict: deterministic, no network, no env vars.
    Used by extract Skill; Phase A does NOT require ZHL_LLM_* env vars.
    """
    def __init__(self, fixture_dir: Path):
        self._outputs = json.loads((fixture_dir / "mock_llm_outputs.json").read_text())
    
    def chat(self, prompt: str, schema: dict) -> dict:
        # Deterministic: keyed by prompt hash or a stable fixture key
        ...
```

- 不绑定具体供应商（总 spec §9.1）；P1 替换为真实 LLMClient 时只改 Protocol 实现
- Phase A 不引入 `ZHL_LLM_API_KEY` / `ZHL_LLM_BASE_URL` / `ZHL_LLM_MODEL` 环境变量

### 5.8 证据落库（A7）

`FileEvidenceStore` 补充 DB 写入：

```python
class FileEvidenceStore:
    def __init__(self, root: Path, db: Session | None = None):
        ...
    
    def save(self, etype, object_ref, *, content=None, path=None, meta=None) -> EvidenceRef:
        # ... file + hash logic (P0) ...
        if self._db is not None:
            self._db.add(Evidence(
                evidence_id=uuid.UUID(evidence_id),
                evidence_type=etype,
                object_ref=object_ref,
                content_hash=digest,
                file_path=rel,
                content=meta.get("content_inline"),
                generated_at=datetime.utcnow(),
                generated_by=meta.get("generated_by", "system"),
            ))
            self._db.flush()
        return ref
```

- `evidence` 表只追加（D8 权限回收）：Phase A 只做 INSERT，不 UPDATE / DELETE
- 模型调用（MockLLMClient）在事务外完成（AGENT.md §4.3），拿到结构化结果后再开事务写 evidence + module_inspections

### 5.9 仓库层（P0 spec §7 留待 builder）

`app/infra/repositories.py`：

```python
class BatchRepository:
    def create_batch(session, batch_no, project_id, model, rule_version_id, planned_quantity) -> DeliveryBatch
    def get_batch(session, batch_id) -> DeliveryBatch | None
    def list_batches(session, status=None) -> list[DeliveryBatch]
    
class InspectionRepository:
    def create_inspections(session, batch_id, rows: list[dict]) -> list[ModuleInspection]
    def confirm_inspection(session, inspection_id, fields: dict) -> ModuleInspection
    def reject_inspection(session, inspection_id) -> ModuleInspection
    
class SampleRepository:
    def freeze_samples(session, batch_id, inspection_type, rule_version_id, sns, seed) -> list[Sample]
    def save_conclusion(session, batch_id, inspection_type, rule_version_id, n, ac, re, result) -> SampleConclusion
    
class EvidenceRepository:
    def save(session, ref: EvidenceRef) -> Evidence
```

- EXCLUDE 兜底检查（P0 spec §7 留待 builder）：`InspectionRepository.create_inspections` 插入前查同 SN 跨批次，应用层拒绝（与 DDL EXCLUDE 双保险）

### 5.10 测试规格（A11）

| 层 | 文件 | 内容 | 覆盖率目标 |
| --- | --- | --- | --- |
| 图回放 | `tests/workflow/test_main_chain.py` | 六节点序列 + 状态转移断言（InMemorySaver + MockLLMClient） | ≥ 60% |
| Plugin 集成 | `tests/plugins/test_sampling.py` | iso_lookup + freeze_sample + DB 写入（SQLite 内存库） | ≥ 70% |
| Plugin 集成 | `tests/plugins/test_evidence_store_db.py` | FileEvidenceStore DB 写入（SQLite 内存库） | ≥ 70% |
| Skill 回放 | `tests/skills/test_extract.py` | MockLLMClient 固定输出回放（≥ 60%） | ≥ 60% |
| API 集成 | `tests/api/test_batches.py` | POST /api/batches + GET /api/batches/{id} + POST /judge | — |
| API 集成 | `tests/api/test_intake.py` | POST /api/intake + confirm / reject | — |

### 5.11 演示命令（AGENT.md §5.2）

Phase A 演示从批次创建到判定完成：

```bash
# 1. DB 初始化（P0 已冻结，幂等）
psql $ZHL_DB -f migrations/001_init.sql
# 2. 跑全部测试
uv run pytest tests/ -v
# 3. API 演示（可选）
uv run uvicorn app.main:app --reload
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' -d '{...}'
curl -X POST localhost:8000/api/intake -F batch_id=... -F file=@test.csv
curl -X POST localhost:8000/api/batches/{id}/judge
```

演示输出含四组验收样例中 Phase A 覆盖的部分（正常放行 + 边界值 + 数据一致性 + 异常隔离的判定侧）。

## 6. 完成标准（Phase A 退出条件）

- [ ] `app/api/batches.py` + `app/api/intake.py` 路由可被 FastAPI TestClient 调用
- [ ] `app/workflow/main_chain.py` 六节点图构建成功，条件路由正确
- [ ] `MockLLMClient` 读 `tests/fixtures/mock_llm_outputs.json`，行为确定可回放
- [ ] `FileEvidenceStore` 补充 DB 写入（`evidence` 表 INSERT），只追加不更新
- [ ] `repositories.py` 批次 / 检验 / 抽样 CRUD 完成
- [ ] 图回放测试（InMemorySaver）通过：六节点序列 + 状态转移断言
- [ ] Plugin 集成测试（SQLite 内存库）通过
- [ ] API 集成测试通过
- [ ] `uv run ruff check . && uv run black --check . && uv run pytest -x` 全绿
- [ ] `uv add langgraph langgraph-checkpoint-postgres` 完成，`uv.lock` 已更新并提交
- [ ] dev-notes `zhl-agent.md` 完整（每节点四要素）
- [ ] P0 冻结项未修改（`git diff phase0-frozen -- app/domain/ app/infra/models.py migrations/ tests/fixtures/` 为空）
- [ ] 演示命令可复现（含 DB 初始化 + 种子数据 + 四组样例 Phase A 部分）

## 7. 已拍板项（builder-check 后补充，标记 *）

> 以下为 builder 可读但 spec 初稿未明确的实现细节，由 builder 拍板并标注；不改变交付物，仅锁定实现方式。

| # | 决策 | 说明 |
| --- | --- | --- |
| B1 * | `MockLLMClient.chat()` 路由规则：按 `source_type` 路由到 `mock_llm_outputs.json` 中对应 key（`extraction` 节点统一用 `extraction` key；`attribution` 留给 Phase B）；无匹配时抛 `KeyError` | 确定性，可回放 |
| B2 * | `agent_task_traces` 写入时机：图执行结束时（`batch_transition` 节点）写一条，记录全部节点序列 + 最终状态；不在每个节点后各写一条 | 减少轨迹表噪声 |
| B3 * | API 错误响应格式：`{"detail": "..."}`（FastAPI 默认 400/404/500 结构）；自定义业务错误用 `HTTPException(status_code=400, detail={...})` 携带结构化 body | 与 FastAPI 惯例一致 |
| B4 * | PG Checkpointer 连接池：`pool_size=5, max_overflow=10`（默认值），连接串读 `ZHL_DATABASE_URL` | 单进程演示场景 |
| B5 * | `BatchWorkflowState.inspections` 字段为 `list[dict]`，每个 dict 含 `inspection_id` / `sn` / 六项电参数 / `appearance_result` / `intake_status`；`decision` 字段为 `dict`（含 `counts` / `fail_sns` / `per_sn: dict[str, list[dict]]`） | 保持 TypedDict 简单，具体子结构在节点实现时确定 |

## 8. 留待 builder 的项（plan 阶段可定，不改变交付物）

- 各节点函数的具体实现细节（错误处理、日志、trace 写入）
- FastAPI 路由的依赖注入方式（`Depends` 获取 `Session` / `checkpointer` / `evidence_store`）
- `app/main.py` 的应用工厂（`create_app()` 挂载路由 + 生命周期管理 Checkpointer 初始化）

## 9. 待用户确认项（builder-check 识别，影响实现边界）

> 以下 3 项是 builder 计划时无法自行决断、且答案会改变模块边界或测试写法的实现约束。需用户在 spec 评审时拍板。

| # | 问题 | 为什么影响 plan | 建议默认（可推翻） |
| --- | --- | --- | --- |
| Q1 | **API ↔ 图触发模型**：`POST /api/intake` 是否触发整图从 `intake` 节点跑起（跑到 `human_confirm` interrupt 挂起）？`POST /api/batches/{id}/judge` 是「恢复图执行从 judge 节点跑到底」还是「重新跑一次判定 Skill（不经过图）」？ | 决定 `judge` API 是否依赖 LangGraph Checkpointer 状态（若 judge 是独立 Skill 调用则不需图状态），以及 `POST /api/intake/{task_id}/confirm` 的 `Command(resume=...)` 语义 | 建议：`POST /api/intake` 触发整图（intake→extract→human_confirm interrupt）；`POST /api/intake/{task_id}/confirm` 用 `Command(resume=...)` 恢复图（human_confirm→judge→sample→batch_transition）；`POST /api/batches/{id}/judge` 为独立判定 Skill 调用（不经图，直接调 `app/skills/judge.py` + 写库），用于「重新判定」场景 |
| Q2 | **`task_id` 语义**：`POST /api/intake/{task_id}/confirm` 的 `task_id` 是 LangGraph `thread_id`（= `batch_id`），还是独立的 task 标识（需存储映射）？P0 `IntakeResponse.task_id` 字段如何生成？ | 若 `task_id = batch_id` 则 confirm API 直接用 `batch_id` 作为 `thread_id` 调 `Command(resume=...)`；若是独立 task_id 则需一张 `intake_tasks` 表（或 `agent_task_traces` 复用）存 `task_id ↔ thread_id` 映射 | 建议：`task_id` 即 `thread_id`（= `batch_id` 字符串），无需额外映射表；`IntakeResponse.task_id` 返回 `str(batch_id)` |
| Q3 | **多组件 confidence 语义**：一次 `POST /api/intake` 可含 N 个 SN（CSV 多行）。`BatchWorkflowState.confidence` 是单值。N 个 SN 各自的 extraction confidence 如何聚合？`human_confirm` 是「整批一次确认」还是「低置信 SN 逐个确认」？ | 若整批一次确认：`confidence = min(N 个 SN 的 confidence)`，`human_confirm` 一次 interrupt；若逐个确认：图结构需循环，`human_confirm` 多次 interrupt，`inspections` 状态需 per-SN confidence 列表 | 建议：整批一次确认——`confidence = min(SN confidences)`；`human_confirm` 一次 interrupt；`fields` 参数可修改任意 SN 的字段值；`intake_status` 为整批统一（confirmed / rejected） |

## 10. 与 AGENT.md 的对应

| AGENT.md 节点 | 本 Phase 落点 |
| --- | --- |
| §1 brainstorm 定稿 | 本文档提交 + 人工评审通过 |
| §1 计划评审 | 后续 writing-plans 产出 plan，人工批准 |
| §3.3 测试 | §5.10 测试规格 |
| §4.3 事务边界 | §5.8 证据落库（模型调用在事务外） |
| §5 交付 | §5.11 演示命令 + §6 完成标准 |
| §6.1 冻结项 | §3.8（P0 冻结项只读） |
| §7.2 LangGraph | §5.2–5.5（节点 / interrupt / Checkpointer） |
| §10 uv | §3.9（新增依赖走 `uv add`） |

## 11. 已知风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| LangGraph API 变动（interrupt / Checkpointer 契约） | 中 | P0 已锁 `langgraph-checkpoint-postgres`；节点编排层可替换；依赖锁在 `uv.lock` |
| `MockLLMClient` 与 P1 真实 LLM 的 prompt 契约漂移 | 中 | `MockLLMClient` 实现 `LLMClient` Protocol；P1 替换时只改实现，节点不变 |
| 图回放测试与 PG Checkpointer 行为差异 | 低 | 先内存 Checkpointer 验证图逻辑，集成测试挂 PG；两阶段测试覆盖 |
| `main_chain_graph` 预留的 `isolated` 分支与 Phase B `incident_subgraph` 集成冲突 | 中 | Phase A 只做路由到 `END`（isolated 状态），不挂载子图；Phase B 在集成阶段挂载 |
| `FileEvidenceStore` DB 写入与 P0 文件骨架的兼容性 | 低 | P0 骨架 `db=None` 可构造；Phase A 注入 `Session` 后启用 DB 写入，文件 + 哈希逻辑不变 |
