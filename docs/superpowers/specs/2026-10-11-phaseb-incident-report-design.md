# Phase B · 异常闭环 + 报告归档设计（Incident Closure & Report Archival）

日期：2026-10-11
定位：Phase B 的可执行开发规格。上游为 `2026-10-10-zhl-system-spec.md`（总 spec）、`2026-10-10-phase0-shared-contract-design.md`（Phase 0 共享契约，已冻结于 tag `phase0-frozen`）与 `AGENT.md`；本文把 Phase B 范围收敛为可直接写 plan 的实现规格。

> 本文在 brainstorming 会话中与人工逐条对齐了关键决策（§4 D1–D7）。带 * 的是我在 spec 之外自行拍板的默认值，**请在评审时逐条确认或推翻**，尤其是 L1/L2/L3（§4.1）这三条契约/路径决策（LLMClient 归属、复检范围 DDL 变更路径、CAPA verified 写入口）。

## 1. 意图与为什么

Phase B 负责批次主链的「异常闭环」与「报告归档」两段：从「已隔离」批次出发，经异常 API（创建/指派/CAPA 确认）→ AI 归因（假设排序 + 证据校验 + 三选一采纳）→ 复检（不覆盖初检）→ 放行断言（逐条校验）→ 报告生成 → 自动归档，驱动批次到达 `released` 或 `rejected` 终态。

Phase B 与 Phase A 并行开发（总 spec §4.0），双方共享 Phase 0 冻结的共享契约（DB Schema、domain 纯函数、接口契约结构）。Phase B 的判定输出可来自 Phase A 真实链路，也可用 Phase 0 测试夹具中的 mock `Decision` 驱动（总 spec §4 集成点说明）。

Phase B 完成的标准是**三条同时成立**（用户明确）：

1. **链路跑通**：异常 → 归因 → 复检 → 断言 → 报告 → 归档，端到端可复现
2. **记录留痕**：三层留痕（轨迹 / 证据 / 报告）各记其位，每步可追溯到原始证据
3. **状态归档**：批次最终到达 `released` 或 `rejected`，报告生成后自动进入 `archived`

## 2. 范围

### 2.1 Goals（Phase B 交付物）

| # | 模块 | 内容 | 依赖 |
| --- | --- | --- | --- |
| B-1 | 异常 API | `POST /api/incidents`（创建，经 `incident_sn_links` 关联 SN）+ 指派 + CAPA 确认（含 `verified` 显式声明，L3） | Phase 0 |
| B-2 | 归因 Skill + Traceability Plugin | 假设生成 + 证据准入门槛 + 置信度排序 + 影响范围 + 人工三选一采纳；LLM 调用经 `app/plugins/llm_client.py`（Phase B 创建，L1） | Phase 0 + L1 |
| B-3 | 复检 API | `POST /api/incidents/{id}/reinspections`，独立 `Reinspection` 记录，不覆盖初检；`scope_type`/`scope_sns` 显式列（L2 alembic revision）；覆盖范围被断言第 7 项 `check_reinspection_scope_covered` 校验（D7-7 方案 A，走 PR + review 改 Phase 0 冻结 `assert_release`） | Phase 0 + L2 + D7-7 |
| B-4 | 放行 API | `POST /api/batches/{id}/release`，6 项断言逐条返回；断言失败不改批次状态 | Phase 0 |
| B-5 | 报告 Skill + 报告 API | 放行报告 / 处置报告 + 版本化，`released` 挂 release 报告，`rejected` 挂 incident 报告 | Phase 0 |
| B-6 | 自动归档 | 报告生成完成后批次自动 `→ archived`（内嵌于报告 API 事务，不暴露独立端点，见 D6a） | B-5 |
| B-7 | 三层留痕贯穿 | 轨迹（agent_task_traces）+ 证据（evidence）+ 报告（reports）三表全部有对应记录 | B-1..B-6 |
| B-10 | LLMClient Protocol + MockLLMClient | `app/plugins/llm_client.py`：`chat(prompt, schema) -> dict`；Mock 按 `incident_id` 路由到 `mock_llm_attribution.json`（L1） | — |
| B-11 | 契约变更 alembic revision | `migrations/002_add_reinspection_scope.sql` + alembic revision：`reinspections.scope_type`/`scope_sns` + `quality_incidents.capa_verified`（L2/L3），存量行回填默认值，PR 描述含影响范围与回滚 | L2/L3 |
| B-8 | 状态机完整路径 | 已隔离 → 待处置 → 待复检 → 待放行 → 已放行 / 已拒绝 / 已归档 全链路 | Phase A 的判定输出（或 mock） |
| B-9 | LangGraph `incident_subgraph` | 归因 → CAPA → 复检 → 断言 子图 + `main_chain_graph` 路由 | Phase 0 Checkpointer |

**Phase B 交付物**：批次从「已隔离」经归因、复检、断言到「已放行」或「已拒绝」并自动归档，含完整三层证据链。

### 2.2 Non-goals（Phase B 不做）

- 不实现数据入场 / 结构化抽取 / 判定 / 抽样（Phase A 范围，总 spec §4）
- 不修改 Phase 0 冻结项（`app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/`）——如需调整必须走 PR + review（AGENT.md §6.1）。**例外**：`reinspections`/`quality_incidents` 两列的契约变更走 L2 的 alembic revision PR；`tests/fixtures/` 只新增（`mock_llm_attribution.json`）不修改已有文件（L1a）；`app/plugins/` 不在冻结范围，可新增 `llm_client.py`（L1）
- 不接入真实 LLM 供应商（用 `MockLLMClient` + `tests/fixtures/mock_llm_outputs.json`，总 spec §9.1）
- 不做前端构建链（静态页 6 个：批次列表 / 批次详情 / 异常详情 / 放行断言视图 / 报告查看 / 任务轨迹，总 spec §9.9）
- 不做认证（P0 单一质量工程师身份，总 spec §9.8）
- 不实现规则版本创建/发布 API（P1，总 spec §9.3）

### 2.3 Anti-goals（做错了算失败）

- domain 层 import infra / sqlalchemy / langgraph / 模型服务（AGENT.md 红线）
- 没有证据 ID 支撑的归因假设进入界面展示（准入门槛退让）
- 复检记录覆盖初检 `module_inspections` 行（必须独立 `reinspections` 行）
- 断言失败触发状态机回退（断言失败没有对应的状态机边）
- 报告生成后批次不自动归档（须显式触发归档动作）
- 置信度分数用于过滤假设（置信度只是排序信号，不是门槛）

## 3. 约束（全部来自上游，逐条引用）

### 3.1 技术栈（总 spec §3.1 锁定）

Python 3.11+ / FastAPI / PostgreSQL 15+ / SQLAlchemy 2.0 / Pydantic v2 / LangGraph / pytest + httpx。禁止引入 spec 之外的新库（AGENT.md §2.3）。

### 3.2 依赖方向（总 spec §3.2 + AGENT.md §3.2）

```
接入层 (FastAPI) → 编排层 (LangGraph 图 + 节点) → 能力层 (Plugin) → domain (纯函数)
                                                          ↓
                                                     数据层 (SQLAlchemy + Postgres)
                                                          ↓
                                                     Checkpointer (LangGraph 状态持久化)
```

domain 层不依赖 infra 层，不 import LangGraph。Skill / Plugin 只做编排与 IO；LangGraph 节点是 Skill / Plugin 的封装壳，不直接写业务判断。

### 3.3 异常五态机（总 spec §2.6）

```
new → isolated → assigned → pending_recheck → closed
                                  └── rejected / scrapped
```

关闭五条硬约束（缺一即保持隔离）：

1. 已关联 SN 或声明整批（经 `incident_sn_links`）
2. 有原始判定证据（`evidence_ids` 非空）
3. 有责任人 + 临时措施 + 确认根因 + 纠正措施（`owner` + `containment_action` + `confirmed_root_cause` + `corrective_action` 全部非空）
4. 有独立复检记录（`verification_id` 非空，指向 `reinspections`）
5. 复检通过且质量工程师确认措施有效（`reinspection.result = pass` + CAPA 验证标记）

**Phase B 新增**：异常七态（含 `closed` / `rejected` / `scrapped` 终态），`rejected` 表示复检失败走拒绝分支，`scrapped` 表示报废（P0 不做）。

### 3.4 批次状态机（总 spec §2.5，10 态）

```
draft → pending_intake → pending_judge
  pending_judge → pending_release   (全部 Pass + 抽样 Pass)
  pending_judge → isolated          (任一 Fail / 数据冲突 / 规则缺失)
  isolated → disposing              (已指派责任人与时限)
  disposing → pending_recheck      (措施已确认)
  pending_recheck → pending_release (复检通过 + 措施有效)
  pending_recheck → rejected        (复检失败)
  pending_release → released        (断言全通过 + 人工批准)
  任意非归档 → rejected             (人工拒绝)
```

**Phase B 新增**：`archived` 态。`released` 或 `rejected` → `archived`，自动触发条件为「报告生成完成」（§4 D6）。

### 3.5 放行断言（总 spec §2.4）

`assert_release` 逐条返回 6 项：

```python
checks = [
    check_state(batch_id, "pending_release"),
    check_sample_passed(batch_id),          # 启用抽样时
    check_no_open_critical_incident(batch_id),
    check_no_isolated_sn(batch_id),
    check_all_evidence_present(batch_id),
    check_all_capa_verified(batch_id),
]
```

每次放行尝试必须逐条返回结果，界面展示未满足的断言项。**断言失败不改批次状态**（§4 D4）。

**Phase B 扩展（方案 A，用户确认）**：`assert_release` 增第 7 项 `check_reinspection_scope_covered`，要求：
- 复检覆盖范围（`scope_sns`）⊇ 所有未关闭异常的影响范围（`incident_sn_links` 全部 SN）
- 抽样方案符合 ISO 表（`iso_sampling_table` 查表命中）

该项需走 **PR + review 修改 Phase 0 冻结的 `assert_release`**（AGENT.md §6.1），Phase B 不直接改 domain（D14 不变：Phase B 自身不新增 domain 函数，但契约扩展允许走 PR + review）。

### 3.6 三层留痕（用户 brainstorming 对齐）

| 层 | 表 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| 轨迹层 | `agent_task_traces` | Agent 每步 Skill / Plugin 调用（输入、输出、模型版本、耗时） | 不存原始数据 |
| 证据层 | `evidence` | 关键原始产物（复检原始值、人工确认差异（CAPA 确认）、源文件哈希、报告快照）SHA-256 哈希 + 文件路径 | 不存计算逻辑；判定快照可复算，按架构 §7.1 存 `module_inspections` 字段，不进证据表 |
| 报告层 | `reports` | 最终归档与可下载产物（Markdown + JSON），版本化不覆盖 | 不存原始数据 |

三层各司其职，不互相替代（§4 D1）。

### 3.7 证据只追加（总 spec §2.1 不变量）

`evidence` 表 `REVOKE UPDATE, DELETE` 给应用角色（Phase 0 DDL 已建，Phase B 不修改）。Phase B 只能 `INSERT` 新证据行。

### 3.8 复检不覆盖初检（总 spec §2.1 不变量）

`Reinspection` 引用原异常与 SN，生成独立的 `reinspections` 行，不复写 `module_inspections` 原始值。复检通过 `verification_id` 回写异常。

### 3.9 测试（AGENT.md §3.3）

- domain 纯函数单测 ≥ 90% 覆盖（Phase 0 已覆盖；Phase B 不新增 domain 函数）
- plugins / skills 集成测试 ≥ 70%（Phase B 新增归因 Skill + 报告 Skill + 复检 Plugin 的测试）
- 端到端：四组验收样例（spec §6.1）中**样例三（异常隔离复检）与样例四（数据一致性）**归 Phase B 驱动，样例一 / 样例二归 Phase A

### 3.10 交付（AGENT.md §5）

一条可复现演示命令（含 DB 初始化 + 种子数据 + Phase B 四组样例驱动）+ 全量 pytest 结果 + dev-notes 路径 + 已知遗留。

## 4. 关键决策（含我拍板的项，标记 *）

> 带 * 的是我在 brainstorming 会话之外自行拍板的默认值，依据上游推导；如有异议请在评审时指出。

| # | 决策 | 依据 |
| --- | --- | --- |
| D1（用户） | **三层留痕，不是一层**：轨迹表（`agent_task_traces`）记 Agent 每步执行；证据库（`evidence`）记关键原始产物；报告（`reports`）做最终归档与可下载产物。三者各司其职，不互相替代 | 用户 brainstorming 原话 |
| D2（用户） | **归因假设准入门槛**：假设必须挂至少一个 `evidence_id`，没有证据支撑的假设在 Skill 层直接丢弃，不进入界面、不进入人工队列 | 用户 brainstorming 原话 |
| D3（用户） | **置信度只是排序信号**：用于假设排序展示，不用于过滤。质量工程师三选一（采纳 / 驳回 / 修改）才是最终裁决 | 用户 brainstorming 原话 |
| D4（用户） | **断言失败不改批次状态**：`assert_release` 是「当前能不能放行」的纯函数检查；`transition()` 是「能不能从 A 到 B」的状态机校验，两者互不调用。断言失败批次停留当前态（`pending_release`），工程师处置后重新调 `POST /api/batches/{id}/release` 幂等重跑 | 用户 brainstorming 原话 |
| D5（用户） | **复检范围是显式字段**：`reinspections` 表增 `scope_type` + `scope_sns` 两列（*：D5a）。默认值取原异常 `incident_sn_links` 里的最小 SN 集合；调用方可显式传更大范围（如整批重检），扩大动作必须在轨迹表 + 报告里可见「本次复检范围 = 整批，非默认」 | 用户 brainstorming 原话 |
| D5a * | `scope_type` 枚举：`minimal`（最小受影响集，默认）/ `expanded`（扩大复检）/ `full_lot`（整批重检）；`scope_sns` 为 `JSONB` 数组，保存本次复检实际覆盖的 SN 清单 | 总 spec §2.1 `Reinspection` 不变量 |
| D6（用户） | **报告生成完成即自动归档**：报告生成完成后批次自动 `→ archived`（前置断言「该批次已挂对应报告且报告版本 ≥ 1」），触发时机见 D6a | 用户 brainstorming 原话 |
| D6a * | 自动归档的触发时机：`POST /api/batches/{id}/report` 在写入 `reports` 行后自动检查「批次状态 ∈ {released, rejected} 且该批次的目标报告类型已有版本 ≥ 1」，满足即在同一事务内调 domain `transition(batch_id, target, preconditions, kind)` 完成 `→ archived`。不暴露独立的 `/archive` 端点（避免误调），归档动作内嵌于报告生成流程 | 总 spec §2.5 `archived` 态；AGENT.md §4.3 事务边界 |
| D7（用户） | **`rejected` 终态只挂处置报告（`report_incident` 模板）**，拒因作为处置报告的一个章节自然包含，不为「拒绝」单独出报告。`released` 终态挂放行报告（`report_release` 模板） | 用户 brainstorming 原话 |
| D8 * | **`archived` 态是终态**：批次进入 `archived` 后，任何后续请求（判定 / 放行 / 异常 / 报告）均 409 拒绝，不可逆。状态机里 `archived` 无出边 | 总 spec §2.5「任意非归档 → rejected」语义反向推导 |
| D9 * | **归因 Skill 的 LangGraph `interrupt()` 点**：仅在「归因假设采纳」一处 interrupt（总 spec §3.2.1 人工三处之一）。假设生成、证据校验、影响范围计算都是确定性节点；`interrupt()` 后等 `Command(resume=...)` 恢复，恢复值 = 工程师的三选一结果 | 总 spec §3.2.1 人工 interrupt 三处 |
| D10 * | **报告 API 幂等语义**：`POST /api/batches/{id}/report` 重复调用同一 `report_type` 不新增版本（返回现有最新版本 + 幂等标记）。归档为终态（D8），批次无状态变化窗口，不存在「状态变化后再生成新版本」的场景 | 总 spec §9.10「版本号自增不覆盖」；D8 `archived` 终态不可逆 |
| D11 * | **Phase B 静态页范围**：4 个页面（异常详情 / 放行断言视图 / 报告查看 / 任务轨迹），批次列表 + 批次详情归 Phase A（总 spec §9.9 全系统 6 页）。Phase B 页面共用 `app/static/` 目录 | 总 spec §9.9 |
| D12 * | **dev-notes ID**：按 `agnes-20261011` 记，路径 `dev-notes/phase-b/agnes-20261011.md` | AGENT.md §1.1；用户指定 `dev-notes/phase-b/{ID}.md` |
| D13 * | **LangGraph `incident_subgraph` 节点契约**：`incident_subgraph` 含 4 节点（`generate_hypotheses` / `validate_evidence` / `human_adopt`（interrupt）/ `run_reinspection`）+ 1 边路由（`route_reinspection_result`：pass → `pending_release`，fail → `rejected`）。`main_chain_graph` 路由「已隔离」分支挂载 `incident_subgraph`。Checkpointer `thread_id = batch_id`（总 spec §3.2.1） | 总 spec §3.2.1 |
| D14 * | **Phase B 不新增 domain 纯函数**：Phase 0 已冻结 `assert_release` / `transition` / `close_incident` 等全部 domain 函数，Phase B 只消费不新增。若 Phase B 发现 domain 缺失（如 `check_reinspection_scope`），必须走 PR + review 改 Phase 0，不直接改 | AGENT.md §6.1；Phase 0 冻结流程 |

## 4.1 契约/路径默认决策（我拍板，* 标记，待评审确认）

> 以下 3 条是 builder-check 视角下 Phase B 实现时必须锁定的契约/路径决策，由我依据上游文档推导拍板（*）。它们覆盖 §4 中对应的 * 项；**如与你的判断冲突，请在评审时逐条指出，以你为准**。

| # | 决策 | 依据 | 影响 |
| --- | --- | --- | --- |
| L1（用户确认） | **LLMClient 协议 + Mock 实现由 Phase B 创建**：落 `app/plugins/llm_client.py`（`LLMClient` Protocol：`chat(prompt, schema) -> dict` + `MockLLMClient`）。`app/plugins/` 不在 Phase 0 冻结目录（AGENT.md §6.1 只冻 `domain/`、`infra/models.py`、`migrations/`、`tests/fixtures/`），Phase B 可落。 | 总 spec §9.1 定协议但未指定创建者；归因 Skill 依赖 LLMClient | B-2 归因 Skill 的 LLM 调用有明确实现归属 |
| L1a（用户确认） | **`mock_llm_outputs.json` 零改动**：该文件在冻结的 `tests/fixtures/` 内，Phase B 不修改它；**新增** `tests/fixtures/mock_llm_attribution.json`（多假设数组 + 各假设独立 `evidence_ids`），MockLLMClient 按 `incident_id` 路由到该夹具。原夹具文件保持冻结态。 | AGENT.md §6.1 冻结范围 | D2/D3 假设准入 + 排序语义可被多假设夹具验证，无需动冻结文件 |
| L2（用户确认） | **复检范围两列走 alembic revision（契约变更 PR）**：`reinspections` 增 `scope_type`（枚举 `minimal`/`expanded`/`full_lot`，默认 `minimal`）+ `scope_sns`（JSONB 数组，默认 = 原异常 `incident_sn_links` 全部 SN）两列。Phase B 提交 `migrations/002_add_reinspection_scope.sql` + alembic revision，作为契约变更 PR；PR 描述写明影响范围（仅加两列，存量行回填默认 `minimal`）与回滚方案（drop column）。 | AGENT.md §4.1/§6.1：Phase 0 冻结后增量 DDL 走 alembic revision | D5/D5a 的显式列语义成立；断言校验与报告引用依赖显式列，不藏在 JSONB |
| L3（用户确认） | **CAPA verified 由工程师在 CAPA 确认端点显式声明**：`POST /api/incidents/{id}/capa` 请求体增 `verified: bool` 字段（默认 `false`），工程师确认措施有效时显式传 `true`。复检 pass 仅是该动作的前置条件之一，**不自动推导** verified（避免把人工判断塌缩成机器判断，违反人工三职责）。`quality_incidents` 增 `capa_verified` 布尔列（同 L2 的 alembic revision 一并提交）持久化该标志；`assert_release` 的 `check_all_capa_verified` 读此列。 | 总 spec §2.6 关闭硬约束第 5 条「复检通过**且**质量工程师确认措施有效」；AGENT.md §8 红线「模型不可改写批次状态」同理延伸到人工判断不可被机器塌缩 | 关闭硬约束第 5 条语义完整；断言第 6 项有明确写入口 |
| D7-7（用户确认，方案 A） | **断言增第 7 项 `check_reinspection_scope_covered`**：复检覆盖范围（`scope_sns`）⊇ 所有未关闭异常的影响范围（`incident_sn_links` 全部 SN），且抽样方案符合 ISO 表。需走 **PR + review 修改 Phase 0 冻结的 `assert_release`**（AGENT.md §6.1）；Phase B 自身不新增 domain 函数（D14），但契约扩展允许走 PR + review。§3.5 / §2.1 B-3 / §6 完成标准同步更新 | 用户复审意见第 4 条 | 复检覆盖范围从 B-3 的「被断言校验」落到具体断言项，闭合 B-3 与 §3.5 的矛盾 |

## 5. 产出物规格

### 5.1 异常 API（`app/api/incidents.py`）

| 端点 | 方法 | 请求体 | 响应 | 状态码 |
| --- | --- | --- | --- | --- |
| `/api/incidents` | POST | `{batch_id, trigger_type, severity, affected_sns[], evidence_ids[], owner?, due_at?, ...}` | `{incident_id, status: "new", sn_links_created: int}` | 201 / 400 |
| `/api/incidents/{id}/assign` | POST | `{owner, due_at, containment_action}` | `{incident_id, status: "assigned"}` | 200 / 409 |
| `/api/incidents/{id}/capa` | POST | `{confirmed_root_cause, corrective_action, verified}` | `{incident_id, status: "assigned" → "pending_recheck", capa_verified}` | 200 / 409 |
| `/api/incidents` | GET | `?batch_id=&status=` | `[IncidentView]` | 200 |
| `/api/incidents/{id}` | GET | — | `IncidentDetailView`（含假设排序 + 复检 + CAPA） | 200 / 404 |

**CAPA 确认约束（L3）**：

- `verified` 字段为 `bool`，默认 `false`；工程师确认措施有效时显式传 `true`
- 前置条件：异常已有复检记录（`verification_id` 非空）且复检结果为 `pass`；不满足时 `verified` 拒绝置位（400 列出未满足项）
- `verified = true` 时写 `quality_incidents.capa_verified = true`（L2 新增列），供 `assert_release` 的 `check_all_capa_verified` 读取
- 复检 pass 是人工确认的前置条件之一，但**不自动推导** verified（L3 语义：措施有效是独立人工判断）

**约束**：
- 创建异常时 `affected_sns` 非空，经 `incident_sn_links` 写关联行（Phase 0 D4 表结构）
- `evidence_ids` 非空且每个 ID 必须存在于 `evidence` 表，否则 400
- `trigger_type` 枚举遵循总 spec §4 接口契约：`rule_fail` / `sample_fail` / `appearance_manual` / `data_consistency` / `rule_missing`
- 指派 / CAPA 驱动异常状态机转移（`new → isolated → assigned → pending_recheck`），每次转移单事务（校验前置 + 更新状态 + 写轨迹）
- 异常创建时批次状态必须为 `isolated`（前置断言），否则 409

### 5.2 归因 Skill + Traceability Plugin（`app/skills/attribution.py` + `app/plugins/traceability.py`）

**LLM 接入（L1）**：归因 Skill 的假设生成调 `app/plugins/llm_client.py` 的 `LLMClient.chat(prompt, schema)`。生产走 OpenAI-compatible API（总 spec §9.1 环境变量），测试/演示走 `MockLLMClient`，按 `incident_id` 路由到 `tests/fixtures/mock_llm_attribution.json`（L1a，多假设数组 + 各假设独立 `evidence_ids`）。

**Skill 输入**：`incident_id` + 跨批次证据（经 `EvidenceStore.list_by_object` 拉取）

**LLM 契约（`app/plugins/llm_client.py`，Phase B 创建，L1）**：

```python
class LLMClient(Protocol):
    def chat(self, prompt: str, schema: dict) -> dict: ...

class MockLLMClient:
    """测试/演示用；按 prompt 中的 incident_id 路由到 mock_llm_attribution.json。"""
    def __init__(self, fixture_path: Path): ...
    def chat(self, prompt: str, schema: dict) -> dict: ...
```

- `app/plugins/` 不在 Phase 0 冻结范围（AGENT.md §6.1），Phase B 可新增该文件
- `mock_llm_attribution.json` 结构（L1a，新增文件，不改 `mock_llm_outputs.json`）：

```json
{
  "INC-001": {
    "hypotheses": [
      {"cause": "...", "confidence": 0.87, "evidence_ids": ["EV-..."], "recommended_action": "..."},
      {"cause": "...", "confidence": 0.62, "evidence_ids": ["EV-..."], "recommended_action": "..."},
      {"cause": "...", "confidence": 0.40, "evidence_ids": [], "recommended_action": "..."}
    ]
  }
}
```

第 3 条假设 `evidence_ids` 为空——用于验证 D2 准入门槛（该假设被丢弃，不进入输出）。

```python
**Skill 输出**（LLM 返回假设数组 → 准入门槛过滤 → 排序）：```

**Skill 流程**（确定性）：

1. **假设生成**：调 `LLMClient.chat(prompt, schema)` 生成假设数组（含 rank / cause / confidence / evidence_ids / recommended_action）
2. **证据准入门槛过滤**：对每条假设，`validate_evidence(hypothesis.evidence_ids)` 检查每个 ID 在 `evidence` 表存在，且 `evidence.object_ref.kind` 与假设关联的 SN / 批次匹配。任一证据缺失 → 该假设丢弃（不进入输出数组）
3. **影响范围计算**：对通过门槛的假设，统计 `affected_sns` 数量 + 跨批次命中数（经 `module_inspections` 查同 SN 是否存在于其他批次）
4. **排序**：按 `confidence` 降序排列，`rank` 重编号（1..N）
5. **写回**：假设数组存入 `quality_incidents.root_cause_hypotheses`（JSONB），写轨迹表（`agent_task_traces`）

**LangGraph 节点**（`incident_subgraph`）：

| 节点 | 类型 | 说明 |
| --- | --- | --- |
| `generate_hypotheses` | 确定性 | 调 LLM Client，输出假设数组 |
| `validate_evidence` | 确定性 | 证据准入门槛过滤 + 影响范围计算 |
| `human_adopt` | **interrupt()** | 等工程师三选一（采纳 / 驳回 / 修改），`Command(resume=...)` 恢复 |
| `run_reinspection` | 确定性 | 调复检 Plugin，结果路由到 `route_reinspection_result` |
| `route_reinspection_result` | 边路由 | pass → `pending_release` 分支；fail → `rejected` 分支 |

**`interrupt()` 恢复值**：

```python
{
  "decision": "adopt" | "reject" | "modify",
  "modified_hypothesis": {...}   # 仅 decision == "modify" 时
}
```

采纳后写入 `quality_incidents.confirmed_root_cause` + `corrective_action`；驳回后异常保持 `assigned`，工程师需重新指派。

### 5.3 复检 API（`app/api/reinspections.py`）

| 端点 | 方法 | 请求体 | 响应 | 状态码 |
| --- | --- | --- | --- | --- |
| `/api/incidents/{id}/reinspections` | POST | `{inspection_id, rule_version_id, pmax, temp, isc, voc, vm, im, ff, result, executor, executed_at, scope_type?, scope_sns?, source_file_id?, evidence_id?}` | `{reinspection_id, incident_id, sn, passed, scope_applied}` | 201 / 400 / 409 |
| `/api/incidents/{id}/reinspections` | GET | — | `[ReinspectionView]` | 200 |

**约束**：

- 异常状态必须为 `assigned` 或 `pending_recheck`（前置断言），否则 409
- `inspection_id` 必须是该异常 `incident_sn_links` 关联的检验记录 ID 之一（范围校验）
- **复检范围校验**（§4 D5）：
  - `scope_type` 缺省 = `minimal`，`scope_sns` 缺省 = 原异常 `incident_sn_links` 里全部 SN
  - `scope_type = expanded` / `full_lot` 时，`scope_sns` 必填且必须覆盖 `minimal` 集合
  - 扩大动作写轨迹表（`agent_task_traces.params_summary.scope_expansion = true`）+ 报告引用
- 生成独立 `reinspections` 行，**不改** `module_inspections` 原始值
- 复检后回写 `quality_incidents.verification_id` = 本行 `reinspection_id`
- 复检结果 `result = pass` → 异常 `→ pending_recheck`（若已处于）或保持；`result = fail` → 异常 `→ rejected`（走拒绝分支，§3.3）
- 单事务：写 `reinspections` 行 + 更新 `quality_incidents.verification_id` + 写轨迹

### 5.4 放行 API（`app/api/release.py`）

| 端点 | 方法 | 请求体 | 响应 | 状态码 |
| --- | --- | --- | --- | --- |
| `/api/batches/{id}/release` | POST | `{approved_by: "quality-engineer"}`（可选 `resume` 字段若来自 interrupt） | `AssertionResult`（逐条） | 200 / 400 / 409 |

**语义**（§4 D4）：

- 调用 domain `assert_release(batch, sample, incidents, isolated_sns, evidence, capa)`，逐条返回 6 项
- **全部通过**：批次 `→ released`，写 `release_evidence_id`（指向 `evidence` 新行），写轨迹，**触发自动归档**（§4 D6a）
- **任一未通过**：批次**停留当前态**（`pending_release`），返回未满足项 + 具体原因（引用 incident_id / SN / evidence_id），**不**调 `transition()`，不写状态变更
- 工程师处置后重新调本端点（幂等「再试一次」语义）
- 批次状态非 `pending_release` 时调本端点 → 409
- 单事务：写放行证据 + 更新批次状态（仅全通过时）+ 写轨迹 + （条件）自动归档

### 5.5 报告 Skill + 报告 API（`app/skills/report.py` + `app/api/reports.py`）

**Skill 输入**：`batch_id` + `report_type`（`release` | `incident`）

**Skill 流程**（确定性，不调模型）：

1. **拉数据**：批次信息 + 判定统计（`module_inspections` 聚合）+ 异常摘要（`quality_incidents` + `incident_sn_links`）+ 复检结果（`reinspections`）+ 放行断言（`AssertionResult`）+ 三层留痕引用（`agent_task_traces` + `evidence` + `reports`）
2. **渲染**：`templates/report_release.md`（放行报告）或 `templates/report_incident.md`（处置报告），Markdown 模板 + 结构化 JSON
3. **写报告**：`POST /api/batches/{id}/report` 单事务写 `reports` 行（`version` 自增，不覆盖），快照证据写 `evidence`（`evidence_type = report_snapshot`）
4. **自动归档**（§4 D6a）：写入后检查「批次状态 ∈ {released, rejected} 且目标报告类型版本 ≥ 1」，满足即同事务内调 `transition(batch_id, "archived", ...)`
5. **写轨迹**：`agent_task_traces` 记报告生成 Skill 调用

**报告 API**：

| 端点 | 方法 | 请求体 | 响应 | 状态码 |
| --- | --- | --- | --- | --- |
| `/api/batches/{id}/report` | POST | `{report_type: "release" \| "incident"}` | `{report_id, version, content_md, content_json}` | 201 / 200（幂等）/ 409 |
| `/api/batches/{id}/reports` | GET | `?report_type=` | `[ReportView]` | 200 |
| `/api/batches/{id}/reports/{report_id}` | GET | — | 报告详情（Markdown + JSON + 三层留痕引用） | 200 / 404 |

**约束**（§4 D7 / D8 / D10）：

- `rejected` 批次只能生成 `report_incident`（处置报告），拒因作为报告章节自然包含
- `released` 批次生成 `report_release`（放行报告）
- `archived` 批次再调报告 API → 409（§4 D8 终态不可逆）
- 同一 `report_type` 重复调用不新增版本（幂等）。归档为终态（D8），批次不存在「状态变化后再生成新版本」的场景（§4 D10）

### 5.6 自动归档（§4 D6 / D6a）

**不暴露独立 `/archive` 端点**（避免误调）。归档动作内嵌于 `POST /api/batches/{id}/report` 流程：

```python
# 伪代码（单事务）
report = write_report(batch_id, report_type)
if batch.status in ("released", "rejected") and report.version >= 1:
    result = transition(batch_id, "archived", preconditions={"report_generated": True}, kind="batch")
    batch.status = result.new_state  # archived
    write_trace(batch_id, "auto_archive", ...)
```

**`archived` 终态约束**（§4 D8）：

- 批次进入 `archived` 后，所有后续 API（判定 / 放行 / 异常 / 报告 / 复检）均 409 拒绝
- 状态机里 `archived` 无出边（总 spec §2.5 语义反向）

### 5.7 LangGraph `incident_subgraph`（§4 D13）

```python
incident_subgraph = StateGraph(IncidentSubgraphState)
incident_subgraph.add_node("generate_hypotheses", generate_hypotheses_node)
incident_subgraph.add_node("validate_evidence", validate_evidence_node)
incident_subgraph.add_node("human_adopt", human_adopt_node)  # interrupt()
incident_subgraph.add_node("run_reinspection", run_reinspection_node)
incident_subgraph.add_edge("generate_hypotheses", "validate_evidence")
incident_subgraph.add_edge("validate_evidence", "human_adopt")
incident_subgraph.add_edge("human_adopt", "run_reinspection")
incident_subgraph.add_conditional_edges("run_reinspection", route_reinspection_result, {
    "pass": END,  # 异常 closed，批次 → pending_release
    "fail": "rejected_route",  # 批次 → rejected
})
```

`main_chain_graph` 在 `isolated` 分支挂载 `incident_subgraph`：

```python
main_chain_graph.add_conditional_edges("pending_judge_route", route_to_subgraph, {
    "all_pass": "pending_release",
    "any_fail": "incident_subgraph",  # 子图入口
    "sample_fail": "incident_subgraph",
    "rule_missing": "incident_subgraph",
    "data_consistency": "incident_subgraph",
})
```

**Checkpointer**：PostgreSQL 后端（`langgraph-checkpoint-postgres`），`thread_id = batch_id`，支持时间旅行回看（总 spec §3.2.1）。

**节点契约**：每个节点是 `add_node` 注册的 callable，接收 `IncidentSubgraphState`，返回状态更新字典。节点内部调 Skill / Plugin，不直接写业务判断（总 spec §9.2 / AGENT.md §7.2）。

### 5.8 静态页（§4 D11，Phase B 范围 4 页）

| 页面 | 文件 | 内容 |
| --- | --- | --- |
| 异常详情 | `app/static/incident.html` | 假设排序（含证据 ID 链接）+ 影响范围 + CAPA 表单 + 复检记录 |
| 放行断言视图 | `app/static/assertion.html` | 6 项断言逐条结果 + 未满足项原因（incident / SN / evidence 引用） |
| 报告查看 | `app/static/report.html` | 版本列表 + Markdown 渲染 + JSON 下载 + 三层留痕引用 |
| 任务轨迹 | `app/static/trace.html` | `agent_task_traces` 全量查询 + 时间线 |

页面共用 `app/static/` 目录，单 HTML + 原生 JS fetch，无构建链（总 spec §9.9）。

### 5.9 测试夹具扩展（Phase B 增量，不修改 Phase 0 冻结夹具）

Phase 0 已冻结 `tests/fixtures/` 六类夹具。Phase B 可**新增**以下夹具文件（不修改已有）：

| 文件 | 内容 | 对应验收样例 |
| --- | --- | --- |
| `acceptance_incident_reinspection.json` | 复检数据（`reinspections` 行 + 异常五态机推进） | 样例三 复检段 |
| `acceptance_incident_rejected.json` | 复检失败 → 批次 `rejected` → 处置报告 | 样例三 拒绝分支 |
| `acceptance_incident_archived.json` | 批次 `released` → 报告 → 自动 `archived` | 样例三 归档段 |

若需修改 Phase 0 冻结夹具（如 `acceptance_incident.json` 增加复检段），必须走 PR + review（AGENT.md §6.1）。

### 5.10 报告模板（`app/templates/`）

| 模板 | 内容 |
| --- | --- |
| `report_release.md` | 批次信息 + 判定统计 + 抽样结论 + 异常摘要（若有）+ 复检结果 + 放行断言（6 项逐条）+ 三层留痕引用 |
| `report_incident.md` | 批次信息 + 判定统计 + 异常详情（假设排序 + CAPA + 复检）+ 拒因（若 `rejected`）+ 三层留痕引用 |

Markdown 渲染为 HTML 页面 + 可下载 `.md`；结构化 JSON（含全部断言结果 + 证据引用）（总 spec §9.10）。

## 6. 完成标准（Phase B 退出条件）

- [ ] 异常 API（创建 / 指派 / CAPA / 查询）全端点可用，驱动异常五态机，单事务边界正确
- [ ] 归因 Skill：证据准入门槛过滤 + 置信度排序 + 影响范围计算 + LangGraph `interrupt()` 三选一采纳，全链路可回放
- [ ] 复检 API：独立 `Reinspection` 记录不覆盖初检，范围显式声明（minimal / expanded / full_lot）且被断言校验，扩大动作写轨迹
- [ ] 放行 API：7 项断言逐条返回（含 `check_reinspection_scope_covered`，D7-7 方案 A），断言失败不改批次状态，幂等可重入
- [ ] `assert_release` 契约扩展 PR + review 通过（AGENT.md §6.1），`migrations/002_add_reinspection_scope.sql` + alembic revision 已提交
- [ ] 报告 Skill + API：放行 / 处置报告版本化不覆盖，`rejected` 只挂处置报告，`released` 挂放行报告
- [ ] 自动归档：报告生成完成后批次自动 `→ archived`（内嵌于报告 API 事务），`archived` 为终态不可逆
- [ ] LangGraph `incident_subgraph` 4 节点 + 边路由 + `main_chain_graph` 挂载，Checkpointer `thread_id = batch_id`
- [ ] 三层留痕：`agent_task_traces` + `evidence` + `reports` 三表在异常闭环全链路上各有对应记录
- [ ] 静态页 4 个（异常详情 / 放行断言 / 报告查看 / 任务轨迹）可用
- [ ] 状态机完整路径：已隔离 → 待处置 → 待复检 → 待放行 → 已放行 / 已拒绝 → 已归档 全链路可复现
- [ ] 样例三（异常隔离复检）+ 样例四（数据一致性）端到端通过
- [ ] `uv run pytest -x` 全量通过；`uv run ruff check . && uv run black --check .` 通过
- [ ] dev-notes `dev-notes/phase-b/agnes-20261011.md` 完整（每节点四要素）
- [ ] 演示命令（AGENT.md §5.2）：一条可复现命令驱动批次从 `isolated` 到 `archived`，含 DB 初始化 + 种子数据 + Phase B 四组样例

## 7. 已确认决策与留待 builder 的项

**已确认（D1–D7 + D7-7，用户 brainstorming 对齐 + 复审拍板）**：

- D1 三层留痕分工
- D2 证据 ID 准入门槛
- D3 置信度只是排序信号
- D4 断言失败不改批次状态
- D5 复检范围显式字段 + 最小受影响集默认
- D6 报告生成完成即自动归档
- D7 `rejected` 只挂处置报告
- **D7-7（用户复审拍板，方案 A）**：断言增第 7 项 `check_reinspection_scope_covered`（复检覆盖范围 ⊇ 未关闭异常影响范围 + 抽样方案符合 ISO 表）；需走 PR + review 修改 Phase 0 冻结的 `assert_release`

**已确认（L1/L1a/L2/L3，用户复审拍板）**：

- L1 LLMClient 协议 + Mock 实现由 Phase B 创建，落 `app/plugins/llm_client.py`
- L1a `mock_llm_outputs.json` 零改动，新增 `mock_llm_attribution.json`
- L2 复检范围两列走 alembic revision（契约变更 PR）
- L3 CAPA `verified` 由工程师在 CAPA 端点显式声明，不自动推导；`quality_incidents.capa_verified` 列持久化

**留待 builder（不改变交付物，plan 阶段可定，* 标记）**：

- D5a `scope_type` 枚举具体取值（`minimal` / `expanded` / `full_lot`）与 `scope_sns` JSONB 结构
- D6a 自动归档触发时机（内嵌于 `POST /report` 事务，不暴露独立 `/archive` 端点，避免误调；已在 §5.6 拍板，评审可推翻）
- D8 `archived` 终态不可逆 + 所有后续 API 409
- D9 `interrupt()` 恢复值结构（`decision` + `modified_hypothesis`）
- D10 报告 API 幂等语义（同 `report_type` 不新增版本；归档为终态，无状态变化窗口，不存在「状态变化后再生成新版本」场景）
- D11 静态页 4 个 vs 全系统 6 页的分工（Phase B 4 页，Phase A 2 页）
- D12 dev-notes 路径 `dev-notes/phase-b/agnes-20261011.md`
- D13 LangGraph `incident_subgraph` 节点命名与边路由结构
- D14 Phase B 自身不新增 domain 纯函数（缺则走 PR + review 改 Phase 0；D7-7 的 `check_reinspection_scope_covered` 属于契约扩展，走 PR + review 允许）

## 8. 与 AGENT.md 的对应

| AGENT.md 节点 | 本 Phase 落点 |
| --- | --- |
| §1 brainstorm 定稿 | 本文档提交 + 人工评审通过 |
| §1 计划评审 | 后续 writing-plans 产出 plan，人工批准 |
| §3.3 测试 | domain ≥ 90%（Phase 0 已覆盖）；plugins / skills ≥ 70%（Phase B 新增）；E2E 样例三 / 四 |
| §4.1 DDL | Phase B 不修改 Phase 0 冻结 DDL；如需 `reinspections.scope_type` / `scope_sns` 两列，走 PR + review（D5a） |
| §4.3 事务边界 | 异常指派 / CAPA / 复检 / 放行 / 报告 + 归档 各自单事务 |
| §5 交付 | §6 完成标准 |
| §6.1 冻结项 | `app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/` Phase B 只读；改契约走 PR + review |
| §7.2 LangGraph | `incident_subgraph` 4 节点 + `interrupt()` + Checkpointer `thread_id = batch_id` |
| §8 红线 | domain 纯度 / 状态机确定性 / 证据只追加 / 不跳过 Phase 0 / dev-notes 不补记 / Context7 先查 |

## 9. 已知风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| `reinspections.scope_type` / `scope_sns` 两列需改 Phase 0 冻结 DDL | 中 | 走 PR + review（AGENT.md §6.1），Phase 0 tag `phase0-frozen` 后增量 DDL 走 alembic revision（Phase 0 D19） |
| `check_reinspection_scope_covered`（断言第 7 项）需改 Phase 0 冻结的 `assert_release` domain 函数 | 中 | D7-7 方案 A：走 PR + review（AGENT.md §6.1），`migrations/002` 与本断言扩展同一 PR 提交；Phase B 自身不新增 domain 函数（D14），契约扩展允许走 PR + review |
| LangGraph `interrupt()` + `Command(resume=...)` 契约在 P0 阶段不够稳定 | 中 | 先实现 domain + Plugin 确定性逻辑，节点编排层可替换；依赖锁在 `uv.lock` |
| 报告 + 自动归档单事务范围过大（写 `reports` + `evidence` + `delivery_batches` + `agent_task_traces`） | 低 | 单事务内只做状态转移 + 证据写入 + 轨迹，不做 LLM 调用（LLM 在事务外，AGENT.md §4.3） |
| 样例三 / 四端到端依赖 Phase A 判定输出（或 mock） | 中 | Phase 0 夹具 `acceptance_incident.json` 提供 mock `Decision`，Phase B 可独立驱动；Phase A 就绪后切换真实链路 |
| 静态页 4 个与 Phase A 2 个共用 `app/static/` 目录，文件冲突 | 低 | Phase B 新增 4 个独立 HTML 文件，不修改 Phase A 已有文件；目录结构按总 spec §3.3 |

## 10. 开发留痕路径

- dev-notes：`dev-notes/phase-b/agnes-20261011.md`
- 每节点四要素（关键原话 / 关键产出 / 拒绝与纠偏 / 翻车与返工）
- 节点：brainstorm 定稿 / 计划评审通过 / 每任务完成 / code review 结论 / finish
