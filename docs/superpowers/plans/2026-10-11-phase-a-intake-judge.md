# Phase A · 数据入场 + 规则判定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 每个任务完成后停止，等待人工核验（AGENT.md 要求）。

**Goal:** 实现 Phase A：批次 API + Intake API + LangGraph 主线段（intake / extract / human_confirm / judge / sample / batch_transition 六节点）+ 抽样 Plugin + 证据落库，批次从 `pending_intake` 推进到 `pending_release` / `isolated`，含完整判定证据。

**Architecture:** FastAPI 路由 → LangGraph 节点（确定性封装，只做编排与 IO）→ Skill / Plugin → domain 纯函数（P0 冻结）；Checkpointer 双模式（内存 / PostgreSQL），`thread_id = batch_id`；MockLLMClient 读固定 JSON 夹具，无需 LLM 环境变量（R4）。

**Tech Stack:** Python 3.11+ / FastAPI / PostgreSQL 15（Docker，端口 15432）/ SQLAlchemy 2.0 / LangGraph / langgraph-checkpoint-postgres / pytest + httpx / uv

**Spec:** `docs/superpowers/specs/2026-10-11-phase-a-intake-judge-design.md`（下称 PA spec）；上游 `2026-10-10-zhl-system-spec.md`（总 spec）与 P0 spec（已冻结）。

## Global Constraints

（来自 PA spec §3 / §4，逐条引用）

- Python 3.11+，black 格式化，ruff lint；类型注解全覆盖（AGENT.md §3.1）
- 节点是 Skill/Plugin 的封装壳，不直接写业务判断；判定/状态转移逻辑全部在 domain 纯函数（R3 / AGENT.md §7.2）
- 人工 interrupt 点 Phase A 仅一处：`human_confirm`（低置信字段确认）；归因假设 / 放行批准属 Phase B
- 确定性路径（断言失败 / SN 冲突 / 规则缺失）直接路由到 isolated / END，不 interrupt
- P0 冻结项只读：`app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/`；需调整必须走 PR + review（AGENT.md §6.1 / PA spec §3.8）
- 新增依赖仅 `langgraph` + `langgraph-checkpoint-postgres`，走 `uv add`，`uv.lock` 必须提交（PA spec §3.9）
- 模型调用（MockLLMClient）在数据库事务外完成，拿到结构化结果后再开事务落库（AGENT.md §4.3 / PA spec §3.6）
- 事务边界：摄入一批组件 = 单事务；判定一个批次 = 单事务写全部组件判定 + 抽样结论 + 批次状态；状态转移 = 单事务（PA spec §3.6）
- 证据只追加：`evidence` 表只做 INSERT，不 UPDATE / DELETE（PA spec §5.8）
- MockLLMClient 读 `tests/fixtures/mock_llm_outputs.json`，不引入 `ZHL_LLM_*` 环境变量（R4 / B1）
- `task_id` = `thread_id` = `str(batch_id)`，无额外映射表（Q2）
- 多组件 `confidence = min(N 个 SN 的 confidence)`，整批一次确认，`intake_status` 整批统一（Q3）
- 批次 `pending_judge` 表示「至少一个组件已摄入」；`judge` 要求全部组件 `intake_status=confirmed`，否则 400 列出未确认 SN（总 spec §9.6 / A4）
- ISO 缺项 → `sample_conclusions.result='rule_missing'` → 批次走 isolated 分支，禁止默认值放行（总 spec §9.4 / A6 / D9）
- 每个任务完成后 `uv run ruff check . && uv run black --check . && uv run pytest -x` 全绿才可提交（AGENT.md §3.4 / §10）
- 所有 Python 工具命令一律 `uv run` 前缀；`uv.lock` 必须提交（AGENT.md §10）
- dev-notes 按 `dev-notes/zhl-agent.md` 逐节点留痕（AGENT.md §1）

## Review Focus

（spec 暗示但任务测试未覆盖、最可能伤人的 5 类输入/失败模式；每项钉死到所属任务的测试）

1. **低置信 interrupt 与自动确认的分界**：`confidence = 0.8` 恰好达标 → 不 interrupt，自动确认整批；`0.79` → interrupt 挂起。边界 0.8 的判定方向决定演示可复现性 —— 由 Task 7 图回放测试 `test_confidence_boundary_auto_confirm` / `test_low_conf_interrrupt` 钉死
2. **interrupt 恢复后不重放已成功节点**：`Command(resume=...)` 恢复后，intake / extract 节点不重新执行（检查点语义）—— 由 Task 7 `test_resume_does_not_replay_upstream` 钉死（节点副作用计数或 state 字段断言）
3. **judge 前置：未全 confirmed → 400**：批次存在未确认 SN 时 `POST /api/batches/{id}/judge` 返回 400 且列出未确认 SN 清单，不静默跳过 —— 由 Task 8 `test_judge_rejects_unconfirmed` 钉死
4. **SN 跨批次 EXCLUDE 双保险**：应用层兜底检查（repository 插入前查同 SN 跨批次）在 DDL EXCLUDE 之外生效；同 SN 第二批次 INSERT 被拒 —— 由 Task 2 `test_cross_batch_same_sn_rejected` 钉死（SQLite 下 DDL EXCLUDE 不生效，验证的是应用层兜底逻辑本身）
5. **ISO 缺项不放行**：`iso_lookup` 查不到组合 → `sample.result='rule_missing'` → `batch_transition` 路由到 isolated 分支（`transition` 前置 `any_fail=True`），不默认 n/ac/re —— 由 Task 7 `test_rule_missing_routes_isolated` 钉死

---

## File Structure

| 文件 | 责任 |
| --- | --- |
| `pyproject.toml` | 加 `langgraph` 依赖（Task 1） |
| `app/main.py` | FastAPI 应用工厂 `create_app()`（Task 9） |
| `app/api/batches.py` | `POST /api/batches` / `GET /api/batches/{id}` / `POST /api/batches/{id}/judge`（Task 9） |
| `app/api/intake.py` | `POST /api/intake` / `POST /api/intake/{task_id}/confirm`（Task 9） |
| `app/workflow/state.py` | `BatchWorkflowState` TypedDict（Task 7） |
| `app/workflow/checkpointer.py` | `get_checkpointer(mode)` 工厂（Task 7） |
| `app/workflow/main_chain.py` | 六节点图构建 + 条件路由（Task 7） |
| `app/skills/extract.py` | 抽取 Skill（Task 5） |
| `app/skills/judge.py` | 判定 Skill（Task 6） |
| `app/plugins/llm_client.py` | `LLMClient` Protocol + `MockLLMClient`（Task 3） |
| `app/plugins/sampling.py` | 抽样 Plugin（Task 6） |
| `app/plugins/evidence_store.py` | `FileEvidenceStore.save` 补 DB 写入（Task 4，改 P0 骨架） |
| `app/infra/repositories.py` | 批次/检验/抽样/证据 仓库 + EXCLUDE 兜底（Task 2） |
| `tests/workflow/test_main_chain.py` | 图回放测试（Task 7） |
| `tests/plugins/test_sampling.py` / `test_evidence_store_db.py` | Plugin 集成测试（Task 4/6） |
| `tests/skills/test_extract.py` / `test_judge.py` | Skill 回放/集成测试（Task 5/6） |
| `tests/api/test_batches.py` / `test_intake.py` | API 集成测试（Task 8） |

## 任务总览

| Task | 交付 | 依赖 |
| --- | --- | --- |
| 1 | 依赖 + 包骨架（`uv add langgraph`；`app/api/` `app/workflow/` `app/skills/` 包） | 无 |
| 2 | 仓库层 `app/infra/repositories.py` + 单测（含 EXCLUDE 兜底） | Task 1 |
| 3 | `MockLLMClient` + `LLMClient` Protocol + 单测 | Task 1 |
| 4 | 证据 DB 写入（`FileEvidenceStore` 补 Session 落库）+ 集成测试 | Task 2 |
| 5 | 抽取 Skill + 回放测试 | Task 3, 4 |
| 6 | 判定 Skill + 抽样 Plugin + 集成测试 | Task 2, 4, 5 |
| 7 | LangGraph 六节点图 + 状态 + Checkpointer + 图回放测试 | Task 5, 6 |
| 8 | API 路由（batches / intake / judge）+ `app.main` + API 集成测试 | Task 7 |
| 9 | 演示 + 全量验证 + dev-notes 收口（Phase A finish） | Task 1–8 |

每个任务完成后**停止并等待人工核验**，再进入下一任务。

---

### Task 1: 依赖 + 包骨架

**Files:**
- Modify: `pyproject.toml`（加 `langgraph` 依赖）
- Create: `app/api/__init__.py`, `app/workflow/__init__.py`, `app/skills/__init__.py`
- Test: `tests/workflow/__init__.py`, `tests/api/__init__.py`, `tests/skills/__init__.py`（包可导入）

**Interfaces:**
- Produces: `langgraph` 可 import（`from langgraph.graph import StateGraph`、`from langgraph.checkpoint.memory import InMemorySaver`、`from langgraph.types import interrupt, Command`）；`app/api` / `app/workflow` / `app/skills` 包就位

- [ ] **Step 1: `uv add langgraph`**

Run: `uv add langgraph` → 更新 `pyproject.toml`（加 `langgraph` 行）+ `uv.lock`。
注意：`langgraph-checkpoint-postgres` 已在 P0 pyproject 中（P0 Task 7 依赖清单），无需重复 add。

- [ ] **Step 2: 建包骨架**

`app/api/__init__.py`、`app/workflow/__init__.py`、`app/skills/__init__.py` 空文件（或包 docstring）；`tests/api/`、`tests/workflow/`、`tests/skills/` 各放 `__init__.py`。

- [ ] **Step 3: 验证**

Run: `uv run python -c "from langgraph.graph import StateGraph; from langgraph.checkpoint.memory import InMemorySaver; from langgraph.types import interrupt, Command; print('ok')"` → `ok`
Run: `uv run pytest -q`（P0 72 用例不回归）
Run: `uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 4: Commit**

```
git add pyproject.toml uv.lock app/api app/workflow app/skills tests
git commit -m "chore(phase-a): add langgraph dep + package skeletons"
```

**人工核验点**：`uv.lock` 已提交；`langgraph` 可 import；P0 测试不回归。

---

### Task 2: 仓库层 `app/infra/repositories.py`

**Files:**
- Create: `app/infra/repositories.py`
- Test: `tests/infra/test_repositories.py`

**Interfaces:**
- Consumes: `app.infra.models`（P0 冻结）
- Produces:
  - `get_session() -> Session`（同步 Session 工厂，连接串读 `ZHL_DATABASE_URL` 环境变量，缺省回落到 `.env` 解析值 `postgresql+psycopg://...@localhost:15432/zhl`；测试可注入 SQLite 内存库）
  - `BatchRepository.create_batch(session, batch_no, project_id, model, rule_version_id, planned_quantity) -> DeliveryBatch`（建 draft 批次）
  - `BatchRepository.get_batch(session, batch_id) -> DeliveryBatch | None`
  - `InspectionRepository.create_inspections(session, batch_id, rows: list[dict]) -> list[ModuleInspection]`（批量 INSERT，`(batch_id, sn)` 唯一；**EXCLUDE 兜底**：插入前查同 SN 在其他批次的记录，冲突抛 `CrossBatchSnConflict`）
  - `InspectionRepository.get_batch_sns(session, batch_id) -> list[str]`
  - `InspectionRepository.confirm_inspection(session, inspection_id, fields: dict | None) -> ModuleInspection`（写修改后字段值，`intake_status='confirmed'`；原始六项电参数列不可改——D7 只允许 `intake_status` / `human_diff` 语义变更）
  - `InspectionRepository.reject_inspection(session, inspection_id) -> ModuleInspection`
  - `SampleRepository.freeze_samples(session, batch_id, inspection_type, rule_version_id, sns, seed, selected_at) -> list[Sample]`（签名补充：还需 `iso_row: IsoRow` 或等价的 `IsoResult`，决定抽样量 n/ac/re——已按 Task 6 的 `run_sampling` 插件实际传入补上该参数，见 Ruling T2-1）
  - `SampleRepository.save_conclusion(session, batch_id, inspection_type, rule_version_id, sample_round, n, ac, re, result) -> SampleConclusion`
  - `SampleRepository.get_latest_conclusion(session, batch_id) -> SampleConclusion | None`
  - `IsoRepository.load_rows(session) -> list[IsoRow]`（读 `iso_sampling_table` 只读配置表）
  - `CrossBatchSnConflict(Exception)` 异常类

- [ ] **Step 1: 写失败单测 `tests/infra/test_repositories.py`**

用 SQLite 内存库（`Base.metadata.create_all`，P0 models 已做跨库兼容）+ 规则种子（`rules_seed.json` 的 JH-550W）。必含：
- `test_create_batch_draft`：建批次 → `status='draft'`
- `test_create_inspections_unique`：同批次同 SN 二次插入 → 唯一约束冲突（IntegrityError 或业务异常，断言行为一致即可）
- `test_cross_batch_same_sn_rejected`（Review Focus #4）：批次 A 插入 SN-001，批次 B 插入同 SN-001 → 抛 `CrossBatchSnConflict`（应用层兜底；SQLite 无 EXCLUDE，验证的是兜底查询逻辑）
- `test_confirm_inspection_fields`：confirm 带 fields 修改 → `intake_status='confirmed'` + 修改值落库
- `test_freeze_samples_deterministic`：同 seed 两次 freeze → 样本列表一致
- `test_save_conclusion_unique`：同批次同组合二次 save → 唯一键冲突

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/infra/test_repositories.py -v` → Expected: FAIL（repositories 未实现）

- [ ] **Step 3: 实现 `app/infra/repositories.py`**

按 Interfaces 签名实现；`create_inspections` 内做 EXCLUDE 兜底查询（`SELECT sn FROM module_inspections WHERE sn IN (...) AND batch_id != :batch_id` 命中即抛 `CrossBatchSnConflict`）。`get_session()` 读 `ZHL_DATABASE_URL`，缺省解析项目根 `.env`（与 `alembic/env.py` 同法，不引第三方 dotenv）。

- [ ] **Step 4: 跑测试确认全绿**

Run: `uv run pytest tests/infra/test_repositories.py -v` → PASS
Run: `uv run pytest tests/ -q && uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 5: Commit**

```
git add app/infra/repositories.py tests/infra/test_repositories.py
git commit -m "feat(infra): repositories — batch/inspection/sample CRUD + cross-batch SN fallback check"
```

**人工核验点**：EXCLUDE 兜底逻辑正确（Review Focus #4）；`get_session` 不硬编码凭据。

**Ruling（偏离 plan 原签名的拍板项，已同步补进上方 Interfaces）**：
- **T2-1** `freeze_samples` 签名补 `iso_row: IsoRow` 参数（原 plan 只列 `seed, selected_at`，但确定性抽样必须知道 n/ac/re；Task 6 的 `run_sampling` 插件先 `iso_lookup` 解析出 `IsoRow`，再传入本方法——这是唯一能同时满足「确定性」+「纯函数分离」的接法，已实测通过）
- **T2-2** `save_conclusion` 签名补 `sample_round` 参数（原 plan 未列，但 `sample_conclusions` 唯一键含 `sample_round`，不传则默认为 1，与 Task 6「复检轮次」语义冲突；补齐后可正确记录第几轮抽样结论）

---

### Task 3: `MockLLMClient` + `LLMClient` Protocol

**Files:**
- Create: `app/plugins/llm_client.py`
- Test: `tests/plugins/test_llm_client.py`

**Interfaces:**
- Consumes: `tests/fixtures/mock_llm_outputs.json`（P0 冻结，R4）
- Produces:
  - `LLMClient(Protocol)`：`chat(prompt: str, schema: dict) -> dict`（总 spec §340 的 Protocol 签名；MockLLMClient 实现同款）
  - `MockLLMClient`：`__init__(fixture_dir: Path)`；`chat` 按 B1 规则——prompt 中嵌入稳定 fixture key（`[SOURCE_SN:SN-I-OODB]` 标记，由 extract Skill 拼接），MockLLMClient 从中解析 key 查 `mock_llm_outputs.json` 的 `extraction` 区块；key 缺失或无匹配抛 `KeyError`；返回 fixture 原样（`extracted_params` / `confidence` / `appearance_findings`）
  - 无网络、无 `ZHL_LLM_*` 环境变量（R4）

- [ ] **Step 1: 写失败单测 `tests/plugins/test_llm_client.py`**

- `test_chat_returns_extraction_fixture`：`prompt = f"Extract ... [SOURCE_SN:SN-I-OODB]"` → `chat(prompt, {})` 返回 fixture 中 `extraction` 区块，`confidence == 0.81`
- `test_chat_deterministic`：同 prompt 两次调用结果一致
- `test_chat_missing_key_raises`：prompt 内 `[SOURCE_SN:UNKNOWN]` → `KeyError`
- `test_no_env_vars_required`：清空 `ZHL_LLM_*` 环境变量后 `chat` 仍可调用（不读 env）

- [ ] **Step 2: 跑测试确认失败 → 实现 `app/plugins/llm_client.py`**

Run: `uv run pytest tests/plugins/test_llm_client.py -v` → FAIL 后实现，再 → PASS

- [ ] **Step 3: 验证纯度**

Run: `uv run ruff check app/plugins/llm_client.py && uv run black --check app/plugins/llm_client.py` → 全绿
grep 确认无 `requests` / `httpx` / `os.environ` 调用

- [ ] **Step 4: Commit**

```
git add app/plugins/llm_client.py tests/plugins/test_llm_client.py
git commit -m "feat(plugins): MockLLMClient — deterministic fixture-backed LLMClient (no env vars, R4)"
```

**人工核验点**：行为确定可回放；无 LLM 环境变量依赖；fixture key 规则与 B1 一致。

---

### Task 4: 证据 DB 写入（`FileEvidenceStore` 补 Session 落库）

**Files:**
- Modify: `app/plugins/evidence_store.py`（P0 骨架：`save` 补 DB 写入分支；`db=None` 时行为不变）
- Test: `tests/plugins/test_evidence_store_db.py`

**Interfaces:**
- Consumes: `app.infra.models.Evidence`（P0 冻结）、`app.infra.repositories`（Task 2）
- Produces:
  - `FileEvidenceStore.save` 在 `self._db is not None` 时 INSERT `evidence` 行（`evidence_id` 由 `make_evidence_id` 确定性派生；`content_hash` 为 SHA-256；`file_path` 为相对 `root` 路径；`generated_at` 用调用方注入的 `now` 参数保持可测，缺省 `datetime.utcnow()`）
  - `evidence` 表只追加：`save` 不 UPDATE / DELETE（PA spec §5.8）
  - P0 已有测试（`tests/plugins/test_evidence_store.py`）不回归

- [ ] **Step 1: 写失败单测 `tests/plugins/test_evidence_store_db.py`**

用 SQLite 内存库 + `FileEvidenceStore(root=tmp_path, db=session)`：
- `test_save_writes_db_row`：save 后 `session` 内 `evidence` 表 1 行，`evidence_id` / `content_hash` / `file_path` 与 `EvidenceRef` 一致
- `test_save_append_only`：同 `object_ref` 二次 save → 同 `evidence_id`（幂等复用，`make_evidence_id` 确定性），DB 不出现第二行冲突（`ON CONFLICT DO NOTHING` 语义或先查后插）
- `test_db_none_fallback`：`db=None` 时 save 只写文件不落库（P0 行为保持）

- [ ] **Step 2: 跑测试确认失败 → 修改 `app/plugins/evidence_store.py`**

Run: `uv run pytest tests/plugins/test_evidence_store_db.py -v` → FAIL 后实现（P0 骨架加 DB 分支），再 → PASS
Run: `uv run pytest tests/plugins/test_evidence_store.py -q` → P0 测试不回归

- [ ] **Step 3: 全绿验证**

Run: `uv run pytest tests/ -q && uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 4: Commit**

```
git add app/plugins/evidence_store.py tests/plugins/test_evidence_store_db.py
git commit -m "feat(plugins): FileEvidenceStore — optional DB insert (append-only), keeps P0 skeleton intact"
```

**人工核验点**：P0 骨架行为（`db=None`）不变；DB 写入只 INSERT。

---

### Task 5: 抽取 Skill

**Files:**
- Create: `app/skills/extract.py`
- Test: `tests/skills/test_extract.py`

**Interfaces:**
- Consumes: `app.plugins.llm_client`（Task 3）、`app.plugins.evidence_store.FileEvidenceStore`（Task 4）
- Produces:
  - `extract_inspection_data(llm: LLMClient, source_type: str, payload: dict, evidence: EvidenceStore, object_ref: dict) -> dict`
    - 调 `llm.chat`（事务外，AGENT.md §4.3）→ 结构化结果
    - 写 `evidence.save('extraction_result', object_ref, content=结构化结果 JSON, ...)`
    - 返回 `{"fields": extracted_params, "confidence": float, "appearance_findings": [...]}`

- [ ] **Step 1: 写失败单测 `tests/skills/test_extract.py`**

固定 MockLLMClient 输出（回放测试，AGENT.md §3.3 skills ≥ 60%）；extract Skill 负责把 `source_sn` / `source_type` 拼进 prompt（`[SOURCE_SN:{sn}]` 标记，与 Task 3 的 B1 规则对应）：
- `test_extract_returns_confidence`：fixture 的 SN → `confidence == 0.81`
- `test_extract_writes_evidence`：save 后 evidence store 有 1 条 `extraction_result`（`EVIDENCE_TYPE` 枚举值，models.py 行 102），`content_hash` 为 JSON 内容的 SHA-256
- `test_extract_min_confidence`（Q3 聚合的输入侧）：两次 extract 取 `min` → 断言聚合逻辑（聚合在 Task 7 图节点做，本 Skill 只返回单 SN 值；此用例验证返回值可被 min 聚合消费）

- [ ] **Step 2: 跑测试确认失败 → 实现 `app/skills/extract.py`**

Run: `uv run pytest tests/skills/test_extract.py -v` → FAIL 后实现，再 → PASS

- [ ] **Step 3: 全绿验证 + 覆盖率**

Run: `uv run pytest tests/skills/ -q && uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 4: Commit**

```
git add app/skills/extract.py tests/skills/test_extract.py
git commit -m "feat(skills): extract — LLM-backed field extraction + extraction_result evidence (mock, deterministic)"
```

**人工核验点**：模型调用在证据写库之前且独立（事务外）；输出可回放。

---

### Task 6: 判定 Skill + 抽样 Plugin

**Files:**
- Create: `app/skills/judge.py`, `app/plugins/sampling.py`
- Test: `tests/skills/test_judge.py`, `tests/plugins/test_sampling_plugin.py`

**Interfaces:**
- Consumes: `app.domain.judge` / `app.domain.sampling` / `app.domain.types`（P0 冻结）、`app.infra.repositories`（Task 2）、`app.plugins.evidence_store`（Task 4）
- Produces:
  - `judge_batch(session, evidence: EvidenceStore, batch_id, rule_version_id) -> dict`
    - 单事务（AGENT.md §4.3）：读全部 confirmed 组件 + 规则快照 → 逐组件 `judge()` → 写 `decision_evidence` / `rule_snapshot`（decision_evidence JSONB 内含 `rule_snapshot`）/ `decision_status` → 写 `decision_snapshot` 证据（`EVIDENCE_TYPE` 值，models.py 行 104）→ 返回 `{"result": "Pass"|"Fail"（整批）, "counts": {"Pass": n, "Fail": m}, "fail_sns": [...], "per_sn": dict}`（B5）
    - 未全 confirmed 抛 `UnconfirmedSnsError(unconfirmed: list[str])`（A4；API 层转 400）
  - `run_sampling(session, iso_rows: list[IsoRow], batch_id, inspection_type, aql, lot_size, sns, seed) -> dict`
    - `iso_lookup` 缺项 → 返回 `{"result": "rule_missing"}`（不写 samples，A6；`SAMPLE_RESULT` 枚举值，models.py 行 99）
    - 命中 → `freeze_sample` + 写 `samples` + `sample_conclusions`（result 按样本判定结果）→ 返回 `{"result": ..., "n": ..., "ac": ..., "re": ...}`

- [ ] **Step 1: 写失败单测**

`tests/skills/test_judge.py`（SQLite + 规则种子 + 全 Pass 夹具组件）：
- `test_judge_batch_all_pass`：`counts == {"Pass": 10, "Fail": 0}`（消费 `acceptance_normal` 的数据形态）
- `test_judge_batch_one_fail`（Review Focus 输入侧）：1 件 pmax 越界 → `fail_sns` 含该 SN，`result == "Fail"`
- `test_judge_batch_unconfirmed_raises`（Review Focus #3 的 Skill 侧）：1 件 pending → `UnconfirmedSnsError`，`unconfirmed == [该 SN]`
- `test_judge_writes_decision_snapshot_evidence`：evidence store 有 1 条 `decision_snapshot`

`tests/plugins/test_sampling_plugin.py`：
- `test_sampling_hit_freezes_samples`：iso 命中 → samples 行数 == n，结论 `result` 正确
- `test_sampling_missing_combo_rule_missing`（Review Focus #5 的 Plugin 侧）：查无组合 → `result == "rule_missing"` 且不写 samples

- [ ] **Step 2: 跑测试确认失败 → 实现两个模块**

Run: `uv run pytest tests/skills/test_judge.py tests/plugins/test_sampling_plugin.py -v` → FAIL 后实现，再 → PASS

- [ ] **Step 3: 全绿验证**

Run: `uv run pytest tests/ -q && uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 4: Commit**

```
git add app/skills/judge.py app/plugins/sampling.py tests/skills/test_judge.py tests/plugins/test_sampling_plugin.py
git commit -m "feat: judge skill (single-tx batch decisions) + sampling plugin (ISO lookup, rule_missing gate)"
```

**人工核验点**：判定单事务；`UnconfirmedSnsError` 语义正确；缺项不放行。

---

### Task 7: LangGraph 六节点图 + 状态 + Checkpointer + 图回放测试

**Files:**
- Create: `app/workflow/state.py`, `app/workflow/checkpointer.py`, `app/workflow/main_chain.py`
- Test: `tests/workflow/test_main_chain.py`

**Interfaces:**
- Consumes: `app.skills.extract` / `app.skills.judge` / `app.plugins.sampling` / `app.plugins.evidence_store` / `app.infra.repositories`（Task 2–6）；`langgraph.graph.StateGraph` / `langgraph.checkpoint.memory.InMemorySaver` / `langgraph.types.interrupt, Command`（Task 1；Context7 已查证：`StateGraph.add_node` / `add_edge` / `add_conditional_edges` / `compile(checkpointer=...)`；interrupt 必须在带 checkpointer 的图上才生效；`graph.stream(inputs, config)` 遇 interrupt 产出 `__interrupt__` chunk，用 `Command(resume=payload)` 同 `thread_id` 继续 `stream`）
- Produces:
  - `BatchWorkflowState`（TypedDict，PA spec §5.4 + B5 + Q3）：`batch_id: str` / `batch_status: str` / `inspections: list[dict]` / `extraction: dict` / `confidence: float` / `low_conf_sns: list[str]` / `intake_status: str` / `decision: dict` / `sample: dict` / `transition_result: dict` / `evidence_ids: list[str]`
  - `get_checkpointer(mode: str) -> Checkpointer`：`"memory"` → `InMemorySaver()`；`"pg"` → `PostgresSaver.from_conn_string(ZHL_DATABASE_URL)`（上下文管理器语义由调用方持有，B4）
  - `build_main_chain_graph(deps: dict, checkpointer: Checkpointer) -> CompiledGraph`（deps 注入 repositories / evidence_store / llm_client，供测试打桩）
  - 六节点：`intake` / `extract` / `human_confirm`（interrupt 点，仅当 `confidence < 0.8`，A3/Q3）/ `judge` / `sample` / `batch_transition`；路由：`human_confirm` 后 `intake_status == 'rejected'` → END；`judge` + `sample` 后全 Pass 且 sample pass → `pending_release`，任一 Fail / rule_missing → `isolated`（预留 incident_subgraph 挂载点，R1）
  - `batch_transition` 节点：调 domain `transition()` 推进批次状态 + 写 `agent_task_traces`（B2 图执行结束时写一条）+ 写 `task_trace` 证据（`EVIDENCE_TYPE` 值，models.py 行 105；PA spec §5.2）
  - `human_confirm` 节点只消费 resume payload；`human_confirmation` 证据（`EVIDENCE_TYPE` 值，models.py 行 103）与 `intake_status` 落库在 API confirm 端点先于 resume 完成（PA spec §5.6），图节点不重复写

- [ ] **Step 1: 写失败图回放测试 `tests/workflow/test_main_chain.py`**

InMemorySaver + 打桩 deps（repositories / llm / evidence 用 fixture 数据源，不依赖 DB）：
- `test_full_pass_path`：全部 confirmed + 全 Pass + sample pass → 最终 `batch_status == 'pending_release'`，节点序列完整
- `test_confidence_boundary_auto_confirm`（Review Focus #1）：`confidence == 0.8` → 图跑到底无 `__interrupt__` chunk，`intake_status == 'confirmed'`
- `test_low_conf_interrupt`（Review Focus #1）：`confidence == 0.79` → 出现 `__interrupt__` chunk；`Command(resume={"action": "confirm"})` 同 `thread_id` 续跑 → 最终 `pending_release`
- `test_reject_path`（Review Focus #1 的 reject 侧）：resume `{"action": "reject"}` → `intake_status == 'rejected'`，图 END，judge / sample 未执行
- `test_resume_does_not_replay_upstream`（Review Focus #2）：interrupt 前记录 intake/extract 副作用计数（evidence 条数 / 写入行数），resume 后续跑 → 计数不变（已成功节点不重放，AGENT.md §7.2）
- `test_rule_missing_routes_isolated`（Review Focus #5）：sample 缺项 → `sample['result'] == 'rule_missing'`，`batch_status == 'isolated'`
- `test_thread_isolation`：两个不同 `thread_id` 并行跑，状态互不串

- [ ] **Step 2: 跑测试确认失败 → 实现三个 workflow 模块**

Run: `uv run pytest tests/workflow/test_main_chain.py -v` → FAIL 后实现，再 → PASS
实现要点：`human_confirm` 节点内先判 `confidence >= 0.8` → 直接写 confirmed 不 interrupt；否则 `interrupt()` 收 resume payload（`{action, fields}`）；节点只编排，判定/转移逻辑调 domain / Skill（R3）。

- [ ] **Step 3: 全绿验证**

Run: `uv run pytest tests/ -q && uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 4: Commit**

```
git add app/workflow tests/workflow/test_main_chain.py
git commit -m "feat(workflow): main_chain_graph — 6 nodes + interrupt + InMemory/PG checkpointer, replay tests"
```

**人工核验点**：interrupt 单点（human_confirm）；Resume 不重放；缺项路由 isolated；两 thread 隔离。

---

### Task 8: API 路由 + `app.main`

**Files:**
- Create: `app/api/batches.py`, `app/api/intake.py`, `app/main.py`
- Test: `tests/api/test_batches.py`, `tests/api/test_intake.py`

**Interfaces:**
- Consumes: `app.workflow.main_chain.build_main_chain_graph`（Task 7）、`app.infra.repositories`（Task 2）、`app.infra.schemas`（P0 骨架，可增字段但不动已有签名）
- Produces:
  - `create_app(checkpointer_mode: str = "memory") -> FastAPI`（应用工厂；路由挂 `/api`；`app.main` 导出 `app` 供 uvicorn）
  - `POST /api/batches`：body `{batch_no, project_id, model, rule_version_id, planned_quantity}` → 建 draft + 转移 `pending_intake`（R5）；响应 201 + `batch_id`
  - `GET /api/batches/{id}`：批次信息 + 组件判定明细（decision_evidence 展开）+ 抽样结论 + 状态时间线（agent_task_traces 按 batch_id，A8/B2）
  - `POST /api/intake`：`multipart/form-data`（`batch_id` + 文件）或 JSON 内联（A2）→ 触发图（intake→extract→human_confirm），低置信时挂起；响应 `IntakeResponse`（`task_id = str(batch_id)`，Q2）
  - `POST /api/intake/{task_id}/confirm`：`{action, fields?}` → 先落库 `intake_status`（confirmed/rejected，整批统一，Q3）+ 写 `human_confirmation` 证据（PA spec §5.6），再 `Command(resume=payload)` 恢复图（Q1/Q2）
  - `POST /api/batches/{id}/judge`：独立判定 Skill 调用（不经图，Q1）；未全 confirmed → 400 + 未确认 SN 清单（A4 / Review Focus #3）

- [ ] **Step 1: 写失败 API 测试 `tests/api/test_batches.py` + `tests/api/test_intake.py`**

FastAPI `TestClient` + 内存 Checkpointer + SQLite 注入（或打桩 repositories）：
- `test_create_batch`：POST /api/batches → 201，`GET /api/batches/{id}` 状态 `pending_intake`
- `test_intake_triggers_graph`（Review Focus #1 的 API 侧）：高置信内联 payload → 响应 `status == 'confirmed'`，无 interrupt；低置信 → 响应 `status == 'pending'`
- `test_confirm_resumes_graph`：低置信 intake 后 POST confirm `{"action": "confirm"}` → 批次推进到 `pending_release`；GET 详情含 decision_evidence；evidence 表含 1 条 `human_confirmation` 行
- `test_judge_rejects_unconfirmed`（Review Focus #3）：存在 pending 组件时 POST judge → 400，`detail` 含未确认 SN 清单
- `test_judge_replays_skill`（Q1 语义）：全 confirmed 后 POST judge → 200，返回 `JudgeResponse`（`Decision` + counts）

- [ ] **Step 2: 跑测试确认失败 → 实现 `app/main.py` + 两个路由模块**

Run: `uv run pytest tests/api/ -v` → FAIL 后实现，再 → PASS
错误响应格式按 B3（`{"detail": ...}`）；依赖注入用 `Depends`（Session / checkpointer / evidence_store / llm_client）。

- [ ] **Step 3: 全绿验证**

Run: `uv run pytest tests/ -q && uv run ruff check . && uv run black --check .` → 全绿

- [ ] **Step 4: Commit**

```
git add app/api app/main.py tests/api
git commit -m "feat(api): batches + intake routes wired to main_chain_graph, judge as standalone skill call (Q1)"
```

**人工核验点**：Q1 触发模型落地（intake 走图 / judge 独立）；task_id = batch_id；400 带未确认 SN 清单。

---

### Task 9: 演示 + 全量验证 + dev-notes 收口（Phase A finish）

**Files:**
- Modify: `dev-notes/zhl-agent.md`（每任务节点 + finish 节点）
- Create: `tests/api/test_e2e_acceptance.py`（Phase A 四组验收样例的 API 侧消费）

**Interfaces:**
- Consumes: Task 1–8 全部产物；`tests/fixtures/*`（P0 冻结）

- [ ] **Step 1: 写 Phase A 验收样例消费 `tests/api/test_e2e_acceptance.py`**

四组样例中 Phase A 侧（总 spec §6.1）：
- 样例一 正常放行（判定侧）：10 组件全 Pass → 批次到 `pending_release`
- 样例二 边界值：533.5 Pass / 533.4 Fail（P0 已钉，这里走 API 消费）
- 样例三 异常隔离（判定侧）：Impp 超档 → 批次 `isolated`
- 样例四 数据一致性：同 SN 跨批次 → `CrossBatchSnConflict` → 400
Run: `uv run pytest tests/api/test_e2e_acceptance.py -v` → PASS

- [ ] **Step 2: 全量验证（PA spec §6 完成标准逐条）**

Run: `uv run pytest tests/ -v`（全量，P0 72 + Phase A 新增）
Run: `uv run pytest tests/workflow tests/plugins tests/skills --cov=app/workflow --cov=app/plugins --cov=app/skills --cov-report=term-missing`（skills/plugins 覆盖率目标 60/70%，AGENT.md §3.3）
Run: `uv run ruff check . && uv run black --check .` → 全绿
Run: `git diff phase0-frozen -- app/domain/ app/infra/models.py migrations/ tests/fixtures/` → 空（P0 冻结项未动）

- [ ] **Step 3: 演示命令（PA spec §5.11，从零可复现）**

```
# 1. 起 PG 容器（AGENT.md §4.0；本机无 compose 插件走 start-pg.ps1）
.\scripts\start-pg.ps1
# 2. 全量测试（含图回放 + API 集成）
uv run pytest tests/ -v
# 3. API 演示（内存 Checkpointer，不依赖 PG 也可跑）
uv run uvicorn app.main:app --reload
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' -d '{"batch_no":"B-001","project_id":"PRJ-1","model":"JH-550W","rule_version_id":"...","planned_quantity":10}'
curl -X POST localhost:8000/api/intake -F batch_id=... -F source_type=IV_CSV -F file=@tests/fixtures/acceptance_normal.json
curl -X POST localhost:8000/api/batches/{id}/judge
```

- [ ] **Step 4: dev-notes 收口 + 交付清单**

`dev-notes/zhl-agent.md` 追加 finish 节点（交付物清单 + 测试结果 + 已知遗留 + Context7 查过 LangGraph `interrupt`/`Command`/`InMemorySaver`/`PostgresSaver` 的留痕）。

- [ ] **Step 5: Commit**

```
git add tests/api/test_e2e_acceptance.py dev-notes/zhl-agent.md
git commit -m "chore(phase-a): finish — e2e acceptance, coverage gate, dev-notes"
```

**人工核验点**：全量测试全绿；P0 冻结项零 diff；演示命令从零可复现；skills ≥ 60% / plugins ≥ 70% 覆盖。

---

## Self-Review

1. **Spec 覆盖**：PA spec §2.1 交付物 → Task 1（依赖）/ 2（仓库）/ 3（LLM）/ 4（证据）/ 5（抽取）/ 6（判定+抽样）/ 7（图+Checkpointer）/ 8（批次+Intake+判定+详情 API）；§6 完成标准 → Task 9。§5.2 节点动作的三类证据写入（extraction_result / decision_snapshot / task_trace）分别落在 Task 5 / Task 6 / Task 7；`human_confirmation` 落在 Task 8 API 侧（§5.6）。`MockLLMClient.chat` 签名与总 spec §340 Protocol `chat(prompt, schema)` 一致（Task 3 修正）。无 gap。
2. **步骤粒度**：每步单一可检查结果；测试步骤给出测试名 + 断言值；实现步骤只给签名 + 一句方法，不写函数体。
3. **类型一致性**：`BatchWorkflowState` 字段（含 `low_conf_sns`）Task 7 统一定义，Task 8 只消费；`UnconfirmedSnsError` Task 6 定义、Task 8 捕获转 400；`CrossBatchSnConflict` Task 2 定义、Task 9 样例四消费；`task_id` 全链 = `str(batch_id)`（Q2）。
4. **Review Focus 5 项**：#1 边界 0.8 → Task 7（`test_confidence_boundary_auto_confirm` / `test_low_conf_interrupt`）+ Task 8（`test_intake_triggers_graph`）；#2 不重放 → Task 7（`test_resume_does_not_replay_upstream`）；#3 400 未确认 → Task 8（`test_judge_rejects_unconfirmed`）；#4 EXCLUDE 兜底 → Task 2（`test_cross_batch_same_sn_rejected`）；#5 缺项不放行 → Task 7（`test_rule_missing_routes_isolated`）+ Task 6（`test_sampling_missing_combo_rule_missing`）。
5. **篇幅**：plan 以任务 + 指针到 spec 为主；未复制 spec 全文；LangGraph API 细节（interrupt/Command/InMemorySaver/PostgresSaver 用法）经 Context7 查证后写在 Task 7 Interfaces，避免执行者凭记忆写。
