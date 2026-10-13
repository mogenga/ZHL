# 前端 TODO 汇总与处置方案

> 日期：2026-10-13 · 范围：`app/static/*.html`（Phase B 静态页 6 + 批次/详情）
> 结论：**5 个 TODO 全部真实存在**，逐条给出证据、定性（缺陷/设计边界/维护约定）与处置方案。

## 0. 总览

| # | 页面 | TODO 内容 | 定性 | 处置方案 | 优先级 |
|---|---|---|---|---|---|
| T-1 | `incident.html:109` | AI 归因假设接口未实现，`root_cause_hypotheses` 恒为空 | **功能缺口（真实缺陷）** | 方案 A：接线现有 `AttributionSkill` + 子图；方案 B：加手动触发端点 | P0 |
| T-2 | `recheck.html:90` | 复检固定批次 `rule_version_id`，无"原/新版本"选择 | 设计边界（P1） | 记录为 P1 已知限制，本期不动 | P1 |
| T-3 | `release.html:41` | 「人工拒绝交付」无 `POST /reject` 路由 | 设计缺口（半真实） | 放行段补 `POST /api/batches/{id}/reject`，复用 `human_rejected` 边 | P0/P1 |
| T-4 | `report.html:48` | FR-5.6 报告 NL 摘要与追问未实现 | P1 非目标 | 明确为 P1，本期不实现；摘要可用模板占位 | P1 |
| T-5 | `state.html:92` | 纯静态页，状态机变更需人工同步 | 维护约定 | 加同步检查清单 / 状态机导出的快照注释 | P2 |

---

## T-1 · AI 归因假设未生成（`incident.html`）

**TODO 原文**（`incident.html:109`）：
> AI 归因假设接口未实现。后端尚未生成假设（root_cause_hypotheses 为空）。需求 FR-3.2 要求结构化假设排序 + 证据引用 + 置信度；当前请人工填写根因。

**证据（已核对）**：
- `app/plugins/attribution.py:149` 有 `incident.root_cause_hypotheses = validated`（`AttributionSkill.run` 会写该列），但**全仓库无任何生产调用点**——唯一引用是子图 docstring 里的注释（`incident_subgraph.py:62,70`）。
- `app/workflow/incident_subgraph.py` 的 `_generate_hypotheses` 是 **pass-through stub**：`return {"hypotheses": state.get("hypotheses") or [], "hypotheses_source": "stub"}`（docstring 明说「the real AttributionSkill is wired in」尚未接线）。
- `app/workflow/main_chain.py:363` 主链 `graph.add_edge("batch_transition", END)` 即终止——**isolated 分支的 `incident_subgraph` 挂载点（R1）并未真正挂进主链**（仅注释提及）。
- `incidents.py:207` 创建异常时 `QualityIncident(...)` 未设 `root_cause_hypotheses`，`models.py` 该列默认 `NULL`。
- 结果：`GET /api/incidents/{id}` 返回的 `root_cause_hypotheses` 恒为 `None` → 前端走 `if (!hyp)` 分支渲染 TODO。

**定性**：**真实功能缺口**。Phase B spec B-2（归因 Skill）+ B-9（incident_subgraph）的**接线**未完成，skill/subgraph 代码在但没进链路。FR-3.2 是 P0。

**处置方案**：
- **方案 A（推荐，接线现有资产）**：
  1. 在 `incident_subgraph._generate_hypotheses` 节点体内调用 `AttributionSkill(session, llm).run(incident)`（替换 stub pass-through），把 `validated` 写回 `state["hypotheses"]` + 落库。
  2. 给子图补一个触发入口：新增 `POST /api/incidents/{incident_id}/attribute`（incidents.py），构造子图入参（`incident_id`/`batch_id`/`hypotheses` seed）+ checkpointer，跑 `generate_hypotheses → validate_evidence → human_adopt(interrupt)`，把结果写 `incident.root_cause_hypotheses`。
  3. 前端 `incident.html` 的「AI 归因」按钮改调该端点；假设非空时渲染 `ZHL.evidenceBlock(hyp)`（现有 else 分支已支持）。
  - 复用现有 `MockLLMClient`（合并后双路由版）+ `mock_llm_attribution.json`，测试零网络。
- **方案 B（最小改动）**：不挂 langgraph，直接在 `POST .../attribute` 里调 `AttributionSkill.run`（绕过子图），子图留作后续。牺牲 D9 的 `interrupt()` 三选一，改由前端表单收集 `adopt/reject/defer`。
- 两方案都需补 `tests`（API + skill 接线）；冻结件不动（skill/subgraph 均在 `app/plugins`/`app/workflow`，非冻结）。

---

## T-2 · 复检无"规则版本选择"参数（`recheck.html`）

**TODO 原文**（`recheck.html:90-92`）：
> 复检参数的规则重算由后端完成；本页只录入人工读取的新实测值。「重跑相同规则版本」的选择（原版本 / 新版本）后端接口未提供参数，按当前批次固定 rule_version_id 执行。

**证据**：
- `reinspections.py` `create_reinspection`：`rule_version_id = batch.rule_version_id`（写死取批次的版本），请求体无 `rule_version_id` 字段。
- Phase B spec §2.2 非目标：「不实现规则版本创建/发布 API（P1，总 spec §9.3）」——即**规则版本本身的 P1**，复检选版本自然落在 P1。

**定性**：**设计边界（P1）**，非缺陷。本期行为正确（用批次锁定版本复检，保证可复现）。

**处置方案**：
- 本期**不实现**，在 spec/计划里显式登记为 P1 backlog：「复检支持指定 rule_version（原版本/新版本）」+「规则版本 CRUD API」。
- 前端 TODO 保留，措辞可补一句「P1：规则版本 API 落地后，`POST .../reinspections` 增加可选 `rule_version_id` 参数，缺省回退 `batch.rule_version_id`」。

---

## T-3 · 人工拒绝交付无 `POST /reject`（`release.html`）

**TODO 原文**（`release.html:41-43`）：
> 「人工拒绝交付（任意非归档 → 已拒绝）」后端接口未实现（无 POST .../reject 路由）。质量工程师主动拒绝交付需走数据库/状态机层，暂不在前端提供入口。

**证据**：
- `state_machine.py:40-44` 状态机**已有**「任意非归档 → rejected」边（precondition `human_rejected`），`rejected → archived`（L45）。
- `intake.py:221` 已有 `confirm/reject` 动作（`new_status = "confirmed" if body.action == "confirm" else "rejected"`），但只覆盖 **intake 段**。
- `reinspections.py:232-249` 复检 fail 时把 incident+batch 转 `rejected`（这是**复检驱动**的拒绝，非人工主动拒绝）。
- `release.py` **没有** `/reject` 路由；放行段（`pending_release`/`isolated`/`draft` 等）的人工主动拒绝**无 API 入口**。

**定性**：**设计缺口（半真实）**。状态机边与判定逻辑都齐了，缺的只是一个放行段的 `POST /api/batches/{id}/reject` 薄路由。

**处置方案**：
- 新增 `POST /api/batches/{batch_id}/reject`（release.py 或独立 `reject.py`）：body `{reason}`；服务端 `transition(batch.status, "rejected", {"human_rejected": True}, kind="batch")` + 写轨迹 +（可选）同事务生成 `incident` 型处置报告。批次非目标态/已归档 → 409（D8 守卫复用）。
- 前端 `release.html` 的 TODO 块换成「人工拒绝」按钮调该端点。
- 需与 D4「断言失败不改状态」区分：这是**人工主动**拒绝，走状态机 `human_rejected` 边，非断言失败路径。
- 优先级：若 P0 要求「批次可达 rejected 终态」，则补；否则归 P1。

---

## T-4 · 报告 NL 摘要与追问未实现（`report.html`）

**TODO 原文**（`report.html:48-50`）：
> 「报告自然语言摘要与追问」（FR-5.6，P1）后端未实现。报告变更时生成新版本（不覆盖）由后端 D10 幂等机制保证。

**证据**：
- 总 spec `质慧链-功能需求文档.md:295`：`FR-5.6 | 报告自然语言摘要与追问 | P1`。
- `report_renderer.py` 只做**结构化** markdown 渲染（`report_release.md` / `report_incident` 模板 + 拒因 section），无 NL 摘要/追问能力。
- `reports.py` 端点：`create/list/get report`，无 `summary`/`ask` 端点。

**定性**：**P1 非目标，真实未实现**（符合预期，非缺陷）。

**处置方案**：
- 本期**不实现**；在 P1 backlog 登记「FR-5.6 报告 NL 摘要 + 追问（需 LLMClient 多轮）」，依赖 T-1 的归因接线（摘要可复用归因假设 + 报告数据）。
- 若需演示占位：`report_renderer` 加一个确定性「模板摘要」段落（纯拼接，不调 LLM），前端渲染；追问留 P1。
- 备注：FR-5.5「变更生成新版本不覆盖」已由 D10 幂等机制实现（重复调用返回已有版本 200）——该半句 TODO 其实是**已实现**的，措辞可澄清。

---

## T-5 · 静态状态页需人工同步（`state.html`）

**TODO 原文**（`state.html:92`）：
> 本页为纯静态参考（0 接口），状态与规则值随 Phase 0 冻结；仅用于界面导航与讲解。若后端状态机变更，本页需人工同步。

**证据**：
- `state.html:101` 自注「本页无任何 fetch 调用」，硬编码批次/异常状态机与三重校验说明。
- 状态机为 Phase 0 冻结件（`app/domain/state_machine.py`），变更须走 PR + review。

**定性**：**维护约定（非缺陷）**。风险是「状态机若变更而页面未同步」导致讲解页失实。

**处置方案**（低优先级）：
- 在 `state_machine.py` 顶部 docstring 加一条「变更时同步 `app/static/state.html`」的指针注释（domain 层 docstring，符合冻结件"doc-only 放行"约定）。
- 或写一个 pytest 断言：`state.html` 内出现的所有状态名 ⊆ 状态机定义的状态集合（防漂移），纳入 CI。
- 本期可仅加 docstring 指针 + 测试，成本低。

---

## 附：与 Phase B 代码审查遗留项的关系

前序审查遗留（fix log 记录的非阻塞项）中，**C-2 / M-2 / M-3 / M-5 / L-1** 与本 5 项 TODO 独立，无重叠。T-1 接线时需顺带核对：子图 stub 标记（`hypotheses_source="stub"`）在接线真实 Skill 后应改为实际来源，trace 的 `validation_result` 同步。

## 建议的执行顺序

1. **T-1（P0）**：接线归因（方案 A 或 B）+ 触发端点 + 前端。
2. **T-3（P0/P1）**：补 `POST /reject` 薄路由 + 前端按钮。
3. **T-5（P2，可随手做）**：`state.html` 漂移测试 + docstring 指针。
4. **T-2 / T-4（P1）**：登记 backlog，本期不实现；T-4 摘要可加模板占位。
