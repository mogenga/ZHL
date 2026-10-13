# 异常归因接线 + 人工拒绝交付 · 设计文档

> 日期：2026-10-13 · 分支：main（Phase B 已并入） · 关联：`docs/superpowers/plans/2026-10-13-frontend-todos.md`（T-1 / T-3）
> 用户裁定：T-1 走**方案 A**（接线真 Skill 进子图 + 触发端点）；T-3 reject 路由**同事务生成 incident 处置报告（D7）+ 自动归档检查（D6a）**（rejected 本就可归档，终态一致）。

## 1. 意图与为何做

- **T-1**：Phase B spec B-2（归因 Skill）+ B-9（incident_subgraph）的**接线**未完成——Skill 与子图代码存在但没进链路，`root_cause_hypotheses` 恒空，前端 incident 页只能人工填根因。FR-3.2（P0）要求「结构化假设排序 + 证据引用 + 置信度」。目标：让异常创建后**可触发 AI 归因**，假设落库并在前端渲染，三选一采纳走 D9 `interrupt()`。
- **T-3**：状态机「任意非归档 → rejected」边已齐（precondition `human_rejected`），但**放行段缺人工主动拒绝的 API 入口**。目标：补一个薄路由，质量工程师可主动把批次拒绝交付，拒绝后与「复检 fail 转 rejected」终态一致（可归档）。

## 2. 目标 / 非目标 / 反目标

### 目标
- T-1：`POST /api/incidents/{id}/attribute` 触发归因子图，跑 `generate_hypotheses`（调真 `AttributionSkill`）→ `validate_evidence` → `human_adopt`（`interrupt()` 三选一），采纳后把假设写 `incident.root_cause_hypotheses` + 写 trace；前端 incident.html「AI 归因」按钮调该端点，假设非空即渲染 `ZHL.evidenceBlock`。
- T-3：`POST /api/batches/{id}/reject`（body `{reason}`）：批次 → `rejected`（`human_rejected` 边）+ 写 trace + 同事务触发 incident 处置报告（D7）+ 自动归档检查（D6a）；前端 release.html 补「人工拒绝」按钮。

### 非目标
- 不接真实 LLM 供应商（沿用 `MockLLMClient` + `mock_llm_attribution.json`，总 spec §9.1）。
- 不实现规则版本选择 / 版本 CRUD（T-2，P1）。
- 不实现 FR-5.6 报告 NL 摘要与追问（T-4，P1）。
- 不改 `main_chain.py` 主链把子图挂进 `batch_transition`（T-1 用**独立触发端点**驱动子图，不侵入 Phase A 主链——避免扩大 Phase A 冻结面与回归）。
- 不动 Phase 0 冻结件（`app/domain/` 逻辑、`infra/models.py` 既有列、`migrations/001`、`tests/fixtures/mock_llm_outputs.json`）。

### 反目标（做错即失败）
- 归因假设无证据 ID 进入前端（违反 D2 准入门槛——由 `AttributionSkill.validate_evidence` 保证，端点不绕过）。
- 子图接线后 `hypotheses_source` 仍标 `stub`（trace 失真；接线真实 Skill 后须改真实来源标记）。
- reject 路由绕过 D8 归档守卫 / D6a 自动归档一致性。
- 断言失败触发状态回退（T-3 是**人工主动**拒绝，走 `human_rejected` 边，非断言失败路径，勿混）。

## 3. 约束（上游）
- AGENT.md §4.3：1 请求 = 1 事务；§8：plugins 可 import httpx/stdlib，不 import langgraph/模型服务。
- D2（假设须挂 ≥1 证据 ID，无证据丢弃）、D7（rejected 仅允许 incident 处置报告）、D8（archived 终态不可逆 + 后续 API 409）、D9（归因三选一 interrupt）、D10（报告幂等不覆盖版本）、D1（三层留痕：trace + evidence + report）。
- 冻结契约：`app/domain/`、`infra/models.py`、`migrations/001`、`mock_llm_outputs.json` 零改动（docstring 允许）。

## 4. 设计（用户已裁定部分 + 构建者决策）

### T-1 · 归因接线（方案 A）

**4.1 子图接线真 Skill（替换 stub）**
- `app/workflow/incident_subgraph.py` `_generate_hypotheses` 节点：接收 `state` 携带的 `incident_id` / `session` / `llm_client`（或经 checkpointer 闭包注入）→ 调 `AttributionSkill(session, llm).run(incident)` → 返回 `{"hypotheses": validated, "hypotheses_source": "attribution"}`。
  - 子图节点体目前读 `state.get("hypotheses")` seed；接线后改为**调用 Skill 生成**（Skill 内部调 `llm_client.chat`，Mock 路由按 `incident_id`）。
  - 为保持「子图可测试（固定输入）」，保留 seed 路径：若 `state["hypotheses"]` 已非空（测试注入）则跳过 Skill 直接透传（`hypotheses_source="seeded"`）；否则跑 Skill（`hypotheses_source="attribution"`）。
- `_validate_evidence` 节点：现 Skill 内已做 `validate_evidence` 过滤；节点改为「搬运 + 标 `evidence_validation="skill"`」（不再 `stub`）。
- **不挂进 main_chain**（非目标）；子图由 T-1 端点独立驱动。

**4.2 触发端点（incidents.py 新增，单请求同步三选一 — 见 §6 裁决）**
- `POST /api/incidents/{incident_id}/attribute`（body `{decision: adopt|reject|defer, cause?: str}`）：
  - 前置：incident 存在；批次非 `archived`（D8 守卫 `require_not_archived`）。
  - 经**闭包工厂**（复用 `_make_run_reinspection` 模式）构造子图：`_make_generate_hypotheses(session, llm, evidence_store)` / `_make_validate_evidence(...)`，依赖不进 LangGraph state。
  - 组装 `IncidentSubgraphState`：`incident_id`/`batch_id`/`hypotheses=[]`（空 seed → 触发 Skill 生成）。
  - `graph.invoke(state, config)` 单次跑到 END；`human_adopt` 的 `interrupt()` 由请求 body 的 `decision`/`cause` 作为 resume 值同步驱动（P1 再启用跨请求持久化）。
  - 子图节点 + 端点落库在同一请求事务（§4.3）：
    - `adopt`：写 `incident.confirmed_root_cause = cause` + `root_cause_hypotheses = validated`（D2 过滤）+ trace（`validation_result` 记真实来源 `attribution`/`seeded`）。
    - `reject`/`defer`：不改 `confirmed_root_cause`，trace 记决策。
- Mock 下假设来自 `mock_llm_attribution.json`（按 incident UUID 键，见 Q3 裁决）；全链路零网络、确定性。未知 incident_id（无 fixture 键）→ Skill 捕获 KeyError → 返回空假设 + 提示（不 404）。

**4.3 前端 incident.html**
- 「AI 归因」按钮：调 `POST .../attribute` → 渲染假设（`ZHL.evidenceBlock`）+ 三选一（adopt 填 cause / reject / defer 按钮）→ 调 `.../attribute/{decision}`。
- 移除现有 `if (!hyp)` 的 TODO 占位分支（接线后假设非空即渲染；仅当 Mock 无该 incident_id 时保留友好提示，不再是「接口未实现」）。

### T-3 · 人工拒绝交付（release.py 新增）

**4.4 `POST /api/batches/{batch_id}/reject`（body `{reason}`）**
- 前置：批次存在；`require_not_archived`（D8）；批次状态 ∈「可拒绝」集合（任意非归档：draft/pending_intake/pending_judge/pending_release/isolated/pending_recheck）——用 `transition(current, "rejected", {"human_rejected": True}, kind="batch")`，状态机已定义这些边（state_machine.py:40-44）。
- 同事务内（§4.3，1 请求 1 事务）：
  1. `transition(batch.status, "rejected", {"human_rejected": True}, kind="batch")` + `batch.status = "rejected"`。
  2. 写 trace（B2/三层留痕，记人工拒绝 + reason）。
  3. **D7：rejected 批次生成 incident 处置报告**——**调用 reports.py 的报告生成路径**（复用 `create_report(report_type="incident")` 的服务端逻辑，不重复实现；D10 幂等已存在同版本则返回既有）。
  4. **D6a 自动归档检查**——**提取 reports.py L149-154 的 D6a 归档逻辑为共享 helper**（如 `app/api/_archival.py` 的 `auto_archive_if_terminal(session, batch)`），reject 路由与 reports 路由都调它（避免 D6a 单一调用点被破坏）。`rejected` 后调之；前置为空（`frozenset()`）故恒可归档 → `archived`。
- 响应：`ReleaseResponse` 风格或新 `RejectOut`（batch_id / status=rejected / auto_archived / report_version）。
- 与「复检 fail 转 rejected」区分：那是 `reinspections.py` 复检驱动；这里是**人工主动**，precondition 显式 `human_rejected`。

**4.5 前端 release.html**
- 移除「人工拒绝交付未实现」TODO 块；补「人工拒绝」按钮（填 reason）→ 调 `POST .../reject`。

## 5. 测试策略（TDD，RED→GREEN）
- T-1：
  - `tests/workflow/test_incident_subgraph.py`（新增或扩）：seeded（注入假设）与 attribution（Mock 按 incident_id）两路径；`hypotheses_source` 断言（`seeded`/`attribution`）。
  - `tests/api/test_incident_attribute.py`（新增）：`POST .../attribute` 返回假设（Mock 已知 incident_id 非空 / 未知 id 抛 404 或空提示）；三选一 `.../attribute/{decision}` 落库（adopt 写 confirmed_root_cause + root_cause_hypotheses；defer 不改；D8 归档批次 409）。
  - 前端 incident.html：手工核验（静态页，无构建链——spec §2.2 不做前端构建；按钮联调靠浏览器 + 后端端点，非 pytest 覆盖；至少加注释对齐端点）。
- T-3：
  - `tests/api/test_release_reject.py`（新增）：各可拒态 → 200/`status=rejected`；archived 批次 409（D8）；reject 后同事务生成 incident 报告（D7，断言 report 行 + version）；auto-archive 触发后 `archived`（D6a）；trace 行存在（D1 三层留痕）。
- 门禁：`uv run ruff check . && uv run black --check . && uv run pytest -x`；冻结契约 `git diff phase0-frozen -- app/domain/ app/infra/models.py migrations/001_init.sql tests/fixtures/mock_llm_outputs.json` 仅允许 docstring。

## 6. 构建者剩余决策（已自决，标注）

### builder-check（2026-10-13 subagent）暴露的 4 点 + 裁决
- **Q1/Q2 interrupt 跨请求 → 单请求同步三选一**：`InMemorySaver` 每请求新建、PG checkpointer 需跨请求 thread_id，均增复杂度且非 spec 必需。裁决：**端点 `POST .../attribute` 把三选一（decision+cause）作为请求参数，单次 `graph.invoke` 从 START 跑到 END**；`human_adopt` 节点的 `interrupt()` 在代码层保留（未来 P1 接 PG 持久化跨请求才启用），本期由请求 body 直接 resume。子图 `generate_hypotheses`/`validate_evidence` 通过**闭包工厂**（复用现有 `_make_run_reinspection` 模式）注入 `session`/`llm`/`evidence_store`/`trace_store`，**不放进 LangGraph state**（state 会被 checkpointer JSON 序列化，活 session 不可序列化）。
- **Q3 fixture 键 vs UUID**：fixture 键 `INC-001`/`INC-004` 是人读 ID，`incident.incident_id` 是 UUID，正则无法命中。裁决：**测试预建 incident 拿 UUID，给 `mock_llm_attribution.json` 新增一个 UUID 形键**（只加不改，保持已有键）。端点对未知 incident_id 返回 `hypotheses=[]`（Mock 无 fixture 键，KeyError 在 Skill 内捕获为友好空结果，不 404）。
- **Q4 D6a 归档重复 + 前置**：`rejected→archived` 前置为空（`frozenset()`），reject 后自动归档**恒可达成**（测试断言 archived 平凡但非 bug）。reports.py L149-154 的 D6a 归档逻辑若内联进 reject 会违反「单一调用点」注释。裁决：**提取 D6a 自动归档逻辑为共享 helper**（reports.py 与 reject 路由都调它），保持单一实现。

### 其余自决
- 子图驱动：请求级 memory checkpointer（测试零依赖）；跨请求持久化三选一留 P1。【builder-check 后由原裁定升级为单请求】
- reject 报告 report_type 固定 `"incident"`（D7）；已有则 D10 幂等复用不升版本。【构建者裁定】
- attribute 端点未知 incident_id：返回 200 + `hypotheses=[]` + 提示。【构建者裁定，Q3 细化】
- 前端按钮沿用 `ZHL.request`，不引入构建链。【构建者裁定】

## 7. 验收（人工核验点）
- T-1：浏览器开 incident.html → 选含 Mock 假设的 incident → 点「AI 归因」→ 见假设排序 + 证据 → adopt/reject/defer → DB `confirmed_root_cause`/`root_cause_hypotheses` 更新 + trace 行。
- T-3：release.html → 对 pending_release 批次点「人工拒绝」填 reason → 批次 `rejected`（或 auto `archived`）+ incident 处置报告 + trace。
- 全量 pytest 全绿；冻结契约零 diff（除 docstring）。
