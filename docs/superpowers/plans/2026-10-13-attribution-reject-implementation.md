# T-1 归因接线 + T-3 人工拒绝交付 · Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让异常可触发 AI 归因（假设落库 + 前端三选一采纳），并补上批次「人工拒绝交付」路由（rejected + 同事务 D7 incident 报告 + D6a 自动归档）。

**Architecture:** T-1 把真 `AttributionSkill` 经**闭包工厂**接进 `incident_subgraph`（替掉 stub 节点），新增 `POST /api/incidents/{id}/attribute` 单请求同步三选一（memory checkpointer，decision 作为 resume 值，跨请求持久化留 P1）；子图不挂进 Phase A 主链（避免扩大冻结面）。T-3 在 `release.py` 新增 `POST /api/batches/{id}/reject`，复用 `human_rejected` 状态机边 + 提取的 D6a 共享归档 helper + 复用报告生成。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / LangGraph 1.2.14（memory checkpointer）/ httpx Mock LLMClient。

**Spec:** `docs/superpowers/specs/2026-10-13-attribution-reject-design.md`（本文档论证依据；执行者需同时读 spec + 本计划）。

## Global Constraints

- `uv run` 执行一切 Python/依赖命令；提交前门禁 `uv run ruff check . && uv run black --check . && uv run pytest -x`（AGENT.md §4）。
- 1 请求 = 1 事务（AGENT.md §4.3）：LLM 调用在事务外，DB 写 + trace 同一事务单元。
- 冻结契约：`app/domain/`（逻辑）、`app/infra/models.py`（既有列）、`migrations/001_init.sql`、`tests/fixtures/mock_llm_outputs.json` **零改动**（domain 层 docstring 允许）。`mock_llm_attribution.json` 只**加** UUID 形键、不改 `INC-001`/`INC-004`。
- 子图节点依赖（session/llm/evidence_store/trace）**经闭包工厂注入，不进 LangGraph state**（state 被 checkpointer JSON 序列化，活 session 不可序列化）。
- D2 准入门槛（假设须挂 ≥1 证据 ID，无证据丢弃）由 `AttributionSkill` 保证，端点不绕过；接线后 trace 的 `hypotheses_source` 不得再标 `stub`。
- D8（archived 终态 409）+ D6a（archived 边前置必须保持空，单一调用点）+ D7（rejected 仅 incident 报告）+ D10（报告幂等不升版本）。
- 静态页不引入构建链（沿用 `ZHL.request` 封装）。

## Review Focus

1. **活 session 泄漏进子图**：若把 SQLAlchemy session 放进 `IncidentSubgraphState`，checkpointer 序列化会炸——须走闭包工厂（Task 1 的测试 pin 住：子图 state 无 session 字段仍能被节点访问依赖）。
2. **未知 incident_id（Mock 无 fixture 键）**：应返回 `200 + hypotheses=[] + 提示`，不 500、不 404（Task 3 pin）。
3. **D2 准入门槛退化**：无证据假设不得进前端——Skill 过滤后为空则结果为空列表（Task 1/3 pin）。
4. **reject 后 archived 恒达成**：`rejected→archived` 前置为空，reject 同事务后批次应恒为 `archived`（Task 5 pin；若未来状态机加前置，D6a 单一调用点注释须同步）。
5. **D7 报告类型**：rejected 批次只允许 `incident` 处置报告——reject 路由生成报告时 report_type 固定 `"incident"`（Task 5 pin）。

---

### Task 1: 子图接线真 AttributionSkill（替换 stub）

**Files:**
- Modify: `app/workflow/incident_subgraph.py`（`_generate_hypotheses`/`_validate_evidence` 节点 + `build_incident_subgraph` 增加依赖闭包注入参数）
- Test: `tests/workflow/test_incident_subgraph.py`（新增）

**Interfaces:**
- Consumes: `app/plugins/attribution.py` 的 `AttributionSkill(llm, evidence_store, cross_batch_counts=None)` + `AttributionSkill.run(incident, *, trace: TraceStore, session, started_at=None) -> list[dict]`（已验证签名：`run` 返回 validated hypotheses list，内部写 `incident.root_cause_hypotheses`）。
- Produces: `build_incident_subgraph(checkpointer, *, reinspection_callable=None, attribution_ctx: dict | None = None)` —— 新增 `attribution_ctx`（`{"incident", "session", "llm", "evidence_store", "trace"}`），当 state 的 `hypotheses` 为空且 ctx 存在时，`generate_hypotheses` 调 Skill；state 非空（seeded）则透传。节点标记 `hypotheses_source ∈ {"seeded","attribution","stub"}`、`evidence_validation ∈ {"skill","stub"}`。

- [ ] **Step 1: 写失败测试**

`tests/workflow/test_incident_subgraph.py`：建一个带固定 UUID `incident_id` 的 `QualityIncident`（走 SQLite session，`batch_id` 关联 isolated 批次）+ MockLLMClient（目录形式，含 attribution fixture 的 UUID 键，见 Task 2）；断言：
  - **seeded 路径**：state `hypotheses=[{...}]` 非空 + 无 `attribution_ctx` → 跑图 → `hypotheses_source == "seeded"`、`evidence_validation == "stub"`、假设透传。
  - **attribution 路径**：state `hypotheses=[]` + 提供 `attribution_ctx`（含 incident/session/llm/trace）→ 跑图 → `hypotheses_source == "attribution"`、`evidence_validation == "skill"`、`incident.root_cause_hypotheses` 非空且每个假设 `evidence_ids` 非空（D2）。
  - **无证据假设被丢弃**：attribution_ctx 下 fixture 假设全空 evidence → 结果 `hypotheses == []`（D2 pin）。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/workflow/test_incident_subgraph.py -v`
Expected: FAIL —— `build_incident_subgraph` 尚无 `attribution_ctx` 参数 / 节点仍回 `stub`。

- [ ] **Step 3: 实现闭包工厂 + 节点接线**

在 `incident_subgraph.py`：仿现有 `_make_run_reinspection` 模式，加 `_make_generate_hypotheses(attribution_ctx)` / `_make_validate_evidence(attribution_ctx)`。`_generate_hypotheses`：若 `state.get("hypotheses")` 非空 → `{"hypotheses": state["hypotheses"], "hypotheses_source":"seeded"}`；否则若 ctx 有 incident → 调 `AttributionSkill(ctx["llm"], ctx["evidence_store"]).run(ctx["incident"], trace=ctx["trace"], session=ctx["session"])` → `{"hypotheses": validated, "hypotheses_source":"attribution"}`；否则 `{"hypotheses": [], "hypotheses_source":"stub"}`。`_validate_evidence`：attribution 路径标 `evidence_validation:"skill"`（过滤已在 Skill 内），seeded/stub 路径标 `"stub"`。`build_incident_subgraph` 加 `attribution_ctx: dict | None = None` 参数，传入两个工厂。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/workflow/test_incident_subgraph.py -v`
Expected: PASS（3 例）。

- [ ] **Step 5: 提交**

```bash
git add app/workflow/incident_subgraph.py tests/workflow/test_incident_subgraph.py
git commit -m "feat(subgraph): wire real AttributionSkill into generate/validate nodes (closure-injected, replaces stub)"
```

---

### Task 2: attribution fixture 加 UUID 形键（供 Mock 按 incident UUID 路由）

**Files:**
- Modify: `tests/fixtures/mock_llm_attribution.json`（**只加**一个 UUID 形键，不改 `comment`/`INC-001`/`INC-004`）

**Interfaces:**
- Produces: 固定 UUID 键 `8f6e2a10-0000-4000-8000-0000000000aa`（测试与 Task 3 端点测试共用同一常量；假设含 `evidence_ids` 非空 + 一个 `evidence_ids` 为空（保留 D2 过滤用例），与既有 `INC-001` 结构一致：`{"hypotheses":[{"cause","confidence","evidence_ids","recommended_action"}]}`。

- [ ] **Step 1: 加键 + 结构测试**

给 `tests/plugins/test_llm_client.py` 加一例：`MockLLMClient(fixture_path=...mock_llm_attribution.json)` 对 prompt 含 `incident_id=8f6e2a10-...-aa` → 返回该键假设（`test_phase_b_mock_routes_by_uuid_incident`）；且 `INC-001`/`INC-004` 路由**不变**（回归）。

- [ ] **Step 2: 运行确认失败**（键不存在）

Run: `uv run pytest tests/plugins/test_llm_client.py::test_phase_b_mock_routes_by_uuid_incident -v`
Expected: FAIL（KeyError：无该 UUID 键）。

- [ ] **Step 3: 在 JSON 追加该键**（不动既有键，结构同 `INC-001`）。

- [ ] **Step 4: 运行确认通过 + 全 llm 测试回归**

Run: `uv run pytest tests/plugins/test_llm_client.py -v`
Expected: PASS（含原 Phase A/B 例 + 新 UUID 例）。

- [ ] **Step 5: 提交**

```bash
git add tests/fixtures/mock_llm_attribution.json tests/plugins/test_llm_client.py
git commit -m "test(fixture): add UUID-shaped attribution fixture key (mock routes by incident uuid); keep INC-001/INC-004 intact"
```

---

### Task 3: `POST /api/incidents/{id}/attribute` 触发端点（单请求同步三选一）

**Files:**
- Modify: `app/api/incidents.py`（新增 `AttributeRequest` body + `attribute_incident` 端点 + 复用现有 `_record_trace`/`SessionDep`/`require_not_archived`）
- Test: `tests/api/test_incident_attribute.py`（新增）

**Interfaces:**
- Consumes: Task 1 `build_incident_subgraph(checkpointer, attribution_ctx=...)`；Task 2 UUID fixture 键；`app.plugins.llm_client.MockLLMClient`（目录形式，读 fixtures 目录）。
- Produces: `POST /api/incidents/{incident_id}/attribute`，body `AttributeRequest{decision: "adopt"|"reject"|"defer", cause: str | None}`，返回 `{"incident_id","decision","hypotheses":[...],"confirmed_root_cause":str|None}`。语义：
  - D8：incident 的批次 `archived` → 409（复用 `require_not_archived`）。
  - incident 不存在 → 404。
  - 组 `attribution_ctx={incident, session, llm: MockLLMClient(_FIXTURES_DIR), evidence_store, trace: TraceStore()}` → 单请求 `graph.invoke(state, config)` 跑到 END，`decision`/`cause` 作为 `human_adopt` 的 resume 值。
  - `adopt`：写 `incident.confirmed_root_cause = cause` + `session.commit()`；`reject`/`defer`：不改 `confirmed_root_cause`。trace 记 decision（复用 `_record_trace`，`validation_result` 记 Skill 来源）。
  - 未知 incident（Mock 无 UUID 键）：`hypotheses=[]`，返回 200 + `hint`（不 404/不 500）。

- [ ] **Step 1: 写失败测试**

`tests/api/test_incident_attribute.py`（TestClient + SQLite，仿 `tests/api/test_incidents.py` 的注入模式）：
  - `test_attribute_adopt_writes_confirmed_cause`：预建 isolated 批次 + incident（UUID 键=Task2 常量）→ POST attribute `{decision:"adopt",cause:"焊接虚焊"}` → 200，`confirmed_root_cause=="焊接虚焊"`，incident.root_cause_hypotheses 非空。
  - `test_attribute_d8_archived_409`：批次 archived → 409。
  - `test_attribute_unknown_incident_empty_hypotheses`：incident UUID 无 fixture 键 → 200 + `hypotheses==[]` + hint。
  - `test_attribute_d2_no_evidence_dropped`：用 D2 过滤（fixture 空证据假设）→ hypotheses 不含无证据项。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_incident_attribute.py -v`
Expected: FAIL（路由不存在 404/405）。

- [ ] **Step 3: 实现端点**

incidents.py 加 `AttributeRequest` + `@router.post("/{incident_id}/attribute")`；组 ctx、单请求 invoke、按 decision 落库、复用 `_record_trace`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_incident_attribute.py -v`
Expected: PASS（4 例）。

- [ ] **Step 5: 全量 + 提交**

Run: `uv run pytest -x`（确保无回归）；`uv run ruff check . && uv run black --check .`
```bash
git add app/api/incidents.py tests/api/test_incident_attribute.py
git commit -m "feat(api): POST /incidents/{id}/attribute triggers attribution subgraph (single-request 3-way, D8/D2/D10-safe)"
```

---

### Task 4: 前端 incident.html 接「AI 归因」按钮（替换 TODO 占位）

**Files:**
- Modify: `app/static/incident.html`（移除 L109 的 `if (!hyp)` TODO 占位分支，补「AI 归因」按钮 + 三选一交互）

**Interfaces:**
- Consumes: Task 3 端点 `POST /api/incidents/{id}/attribute`（`ZHL.request("POST", ...)`）。

- [ ] **Step 1: 改前端**

- 「AI 归因」按钮（id 如 `doAttribute`）：调 `attribute`（decision 先置 `adopt`/由下方三选一决定）→ 渲染假设 `ZHL.evidenceBlock`；三选一（adopt 填 cause / reject / defer）→ 调 `attribute` 带对应 decision → 刷新 incident 详情。
- `if (!hyp)` 分支：改为「尚无假设，点『AI 归因』生成」提示（非「接口未实现」）；Mock 无该 incident 假设时显示「无匹配归因 fixture」。
- 无构建链，手工浏览器核验（spec §2.2：静态页不纳入 pytest）。

- [ ] **Step 2: 核验 + 提交**

浏览器核验（Postgres 或临时 `.env`）：选含 Mock 假设的 incident → 点 AI 归因 → 见假设 + 三选一 → DB `confirmed_root_cause`/`root_cause_hypotheses` 更新。
```bash
git add app/static/incident.html
git commit -m "feat(ui): incident.html AI-attribution button + 3-way adoption (replaces unimplemented-TODO placeholder)"
```

---

### Task 5: D6a 自动归档共享 helper 提取（reject 与 reports 共用）

**Files:**
- Create: `app/api/_archival.py`
- Modify: `app/api/reports.py`（L142-154 的 D6a 段改为调 helper，保留 D6a 不变式注释迁移到 helper）
- Test: `tests/api/test_archival.py`（新增，pin helper 行为）

**Interfaces:**
- Produces: `auto_archive_if_terminal(session, batch) -> bool` —— 当 `batch.status in ("released","rejected")` 时 `transition(batch.status,"archived",{},kind="batch")`；accepted 则 `batch.status="archived"` 返回 True，否则 False。含 D6a 不变式注释（archived 边前置须保持空）。

- [ ] **Step 1: 写 helper 行为测试**

`test_archival.py`：released 批次 → 调 helper → `batch.status=="archived"` 返回 True；pending_release 批次 → 返回 False（status 不变）；archived 批次 → 返回 False。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_archival.py -v`
Expected: FAIL（helper 未建）。

- [ ] **Step 3: 建 `app/api/_archival.py` + reports.py 改调它**

`_archival.auto_archive_if_terminal`；reports.py `create_report` 的 D6a 段（L149-154）替换为 `archived = auto_archive_if_terminal(session, batch)`（不变式注释移入 helper）。

- [ ] **Step 4: 运行确认通过 + reports 回归**

Run: `uv run pytest tests/api/test_archival.py tests/api/test_reports.py -v`
Expected: PASS（helper 例 + 原 reports 例全绿，D10 幂等/自动归档行为不变）。

- [ ] **Step 5: 全量 + 提交**

```bash
git add app/api/_archival.py app/api/reports.py tests/api/test_archival.py
git commit -m "refactor(api): extract D6a auto-archive into shared helper (single call site, used by reports + upcoming reject)"
```

---

### Task 6: `POST /api/batches/{id}/reject`（T-3 人工拒绝交付）

**Files:**
- Modify: `app/api/release.py`（新增 `RejectRequest` + `reject_batch` 端点）
- Test: `tests/api/test_release_reject.py`（新增）

**Interfaces:**
- Consumes: `app.domain.state_machine.transition`（`human_rejected` 边）、Task 5 `auto_archive_if_terminal`、`app.api.guards.require_not_archived`、`app.skills.report_skill.ReportSkill`（incident 报告，D7）、`app.api.reports._record_trace`。
- Produces: `POST /api/batches/{batch_id}/reject`，body `RejectRequest{reason: str}`，返回 `{"batch_id","status":"rejected"|"archived","auto_archived":bool,"report_version":int|None,"reason":str}`。语义：
  - incident/批次不存在 → 404；批次 `archived` → 409（D8）。
  - `transition(batch.status, "rejected", {"human_rejected": True}, kind="batch")`；rejected。
  - 同事务：生成 `incident` 处置报告（D7，`ReportSkill().generate(session, batch_id, "incident", generated_at=...)`，D10 幂等已存在则不升版本）+ `auto_archive_if_terminal(session, batch)`（D6a）+ trace（记人工拒绝 + reason + report_version + auto_archived）。
  - 1 请求 1 事务（§4.3）。

- [ ] **Step 1: 写失败测试**

`test_release_reject.py`（SQLite + TestClient）：
  - `test_reject_from_pending_release`：pending_release 批次 → POST reject `{reason:"..."}` → 200，`status=="archived"`（D6a 恒达成）、`auto_archived==True`、incident 报告行存在（version=1）。
  - `test_reject_d8_archived_409`：已 archived 批次 → 409。
  - `test_reject_d7_incident_report`：reject 后该批次 `incident` 报告存在、report_type=="incident"。
  - `test_reject_trace`：agent_task_traces 有对应行（D1 三层留痕）。
  - `test_reject_unknown_batch_404`。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/api/test_release_reject.py -v`
Expected: FAIL（路由不存在）。

- [ ] **Step 3: 实现端点**

release.py 加 `RejectRequest` + `@router.post("/{batch_id}/reject")`；transition + ReportSkill(incident) + `auto_archive_if_terminal` + trace，单事务 commit。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/api/test_release_reject.py -v`
Expected: PASS（5 例）。

- [ ] **Step 5: 全量 + 提交**

Run: `uv run pytest -x`；`uv run ruff check . && uv run black --check .`
```bash
git add app/api/release.py tests/api/test_release_reject.py
git commit -m "feat(api): POST /batches/{id}/reject — manual reject (human_rejected edge) + same-tx incident report (D7) + auto-archive (D6a)"
```

---

### Task 7: 前端 release.html 补「人工拒绝」按钮（替换 TODO 占位）

**Files:**
- Modify: `app/static/release.html`（移除 L41-43 TODO 块，补「人工拒绝」按钮调 Task 6 端点）

- [ ] **Step 1: 改前端**

「人工拒绝」按钮（填 reason）→ `ZHL.request("POST", /api/batches/{id}/reject, {reason})` → 展示 `status`(archived) + report_version + 提示。移除原「未实现」TODO 块。

- [ ] **Step 2: 核验 + 提交**

浏览器核验：pending_release 批次点拒绝 → 200 + 批次 archived + incident 处置报告。
```bash
git add app/static/release.html
git commit -m "feat(ui): release.html manual-reject button (POST /batches/{id}/reject), replaces unimplemented-TODO"
```

---

## Self-Review（对 spec 逐节核对）

- **§4.1 子图接线** → Task 1（闭包工厂替 stub，seeded/attribution/stub 三态标记）。✅
- **§4.2 触发端点单请求三选一** → Task 3。✅（interrupt 保留代码，P1 才启用跨请求——spec §6 裁决）
- **§4.3 前端 incident.html** → Task 4。✅
- **§4.4 reject 端点（transition + D7 + D6a + trace 同事务）** → Task 6。✅
- **§4.5 前端 release.html** → Task 7。✅
- **§6 裁决**：单请求三选一（Task 1/3）、UUID fixture 键（Task 2）、D6a 共享 helper（Task 5）——三处 builder-check 裁决全部落到任务。✅
- **Review Focus 5 条**：#1 活 session 不进 state（Task 1 测试）、#2 未知 incident 空假设（Task 3 测试）、#3 D2 退化（Task 1/3 测试）、#4 reject 后 archived（Task 6 测试）、#5 D7 报告类型（Task 6 测试）。✅ 全部有归属任务。
- **类型一致性**：`AttributionSkill(llm, evidence_store)` 构造顺序、`run(incident, *, trace, session)`、`auto_archive_if_terminal(session, batch) -> bool`、`AttributeRequest{decision,cause}` / `RejectRequest{reason}` 在相邻任务签名一致。✅
- **比例**：计划 ≈ spec 1.4×，以签名 + 测试名 + 断言为主，未复制整段实现体。✅

**执行交接**：计划已存 `docs/superpowers/plans/2026-10-13-attribution-reject-implementation.md`，7 任务（TDD，每任务独立可测、独立提交）。任务依赖：Task 1←2（UUID 键）←3←4；Task 5←6←7。请审阅计划是否符合预期，并选执行方式：

- **Subagent-driven**：新 subagent 逐任务实现 + 新 reviewer 复核，收尾整分支审。最彻底；每任务/每复核开新上下文。
- **Native**：我在本会话逐任务实现，收尾一个最强模型 reviewer 审整分支。最省；中间无独立复核。

本计划 7 任务、接口耦合集中（Task 1 的 ctx 契约 + Task 5 的 helper 是 3/6 的前置），且 T-1 归因接线是 P0 功能缺口（漏做会持续误导前端）——**我推荐 Native + 收尾整分支审**（任务有前后依赖、本会话上下文已含全部代码细节，subagent 冷启动重读成本高；收尾审覆盖整分支安全网）。请确认执行方式。
