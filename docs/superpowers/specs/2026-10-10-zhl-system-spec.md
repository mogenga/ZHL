# 质慧链光伏检验数字化平台 · 系统 Spec

日期：2026-10-10
定位：可执行的开发规格。基于《功能需求文档》与《架构设计文档》细化，面向并行开发。

---

## 1. 文档定位与依据

- **上游依据**：`docs/质慧链-功能需求文档.md`（业务边界与验收）、`docs/质慧链-架构设计.md`（系统架构与分层）。
- **本 Spec 的增量**：
  1. 将业务需求收敛为可开发的任务清单（P0/P1）
  2. 明确并行拆分边界与共享契约
  3. 锁定技术栈与代码组织
  4. 列出待决事项（O-1/O-2/O-3）的锁定条件
  5. 补充两份上游文档未覆盖的实现约束

---

## 2. 系统需求（收敛后）

### 2.1 核心对象与不变量

| 对象 | 不变量（数据库层强制） |
| --- | --- |
| `QualityRuleVersion` | 发布后不可修改；判定可复算的唯一依据 |
| `DeliveryBatch` | 一个批次绑定一个规则版本；状态机强制转移 |
| `ModuleInspection` | `(batch_id, sn)` 唯一；原始值不可改 |
| `QualityIncident` | 必须关联至少一个 SN；异常五态机 |
| `Reinspection` | 引用原异常与 SN，不覆盖初检 |
| `Evidence` | 只追加，不可修改删除 |
| `AgentTaskTrace` | 每次 Agent 任务全留痕 |
| `SampleConclusion` | 唯一键 `(batch_id, inspection_type, rule_version_id, sample_round)` |

### 2.2 五段主链（P0 必须全部可复现）

| 段 | 输入 | 输出 | 状态变化 | 失败去向 |
| --- | --- | --- | --- | --- |
| 1 数据入场 | 质检单/CSV/模拟设备 | 带证据的检验记录 | 待入场→待判定 | 解析失败→重新提交任务 |
| 2 规则判定 | 记录+规则版本 | Pass/Fail+证据 | 待判定→待放行/已隔离 | 异常事件 |
| 3 AI 归因 | 异常+跨批次证据 | 假设排序+影响范围 | 已隔离→待处置 | 保持隔离 |
| 4 复检放行 | 复检记录+措施确认 | 放行/拒绝决定 | 待复检→待放行/已拒绝 | 拒绝交付 |
| 5 报告归档 | 已放行/已隔离数据 | 版本化报告 | 已放行/已拒绝→已归档 | 不生成错误类型报告 |

### 2.3 判定规则（确定性核心）

判定引擎为纯函数，约束四不：不读库、不取时间、不调随机、不调模型。

判定顺序（三重校验 + 外观）：

```
① 基础校验：项目/SN/等级/日期/六项电参数非空
② 功率范围：实测 ∈ [铭牌×(1−容差), 铭牌×(1+容差)]
③ 参数容差：温度/Isc/Voc/Vm/FF 逐项闭区间
④ 电流分档：Impp 落对应区间
⑤ 外观：Pass
```

容差公式（闭区间）：`lower = 标准值 × (1 − 容差%)`，`upper = 标准值 × (1 + 容差%)`，`lower ≤ 实测 ≤ upper`。

**边界值语义**：等于下限或上限均判 Pass。

### 2.4 放行断言（逐条验证）

```python
assert_release(batch_id) -> AssertionResult:
    checks = [
        check_state(batch_id, "pending_release"),
        check_sample_passed(batch_id),          # 启用抽样时
        check_no_open_critical_incident(batch_id),
        check_no_isolated_sn(batch_id),
        check_all_evidence_present(batch_id),
        check_all_capa_verified(batch_id),
    ]
```

每次放行尝试必须逐条返回结果，界面展示未满足的断言项。

### 2.5 批次状态机（10 态）

```
draft → pending_intake → pending_judge
  pending_judge → pending_release   (全部 Pass + 抽样 Pass)
  pending_judge → isolated          (任一 Fail / 数据冲突 / 规则缺失)
  isolated → disposing              (已指派责任人与时限)
  disposing → pending_recheck       (措施已确认)
  pending_recheck → pending_release (复检通过 + 措施有效)
  pending_recheck → rejected        (复检失败)
  pending_release → released        (断言全通过 + 人工批准)
  任意非归档 → rejected             (人工拒绝)
```

转移为纯函数，前置条件写死在状态机模块，模型不可改写。

### 2.6 异常状态机（五态）

```
new → isolated → assigned → pending_recheck → closed
                                  └── rejected / scrapped
```

关闭五条硬约束（缺一即保持隔离）：
1. 已关联 SN 或声明整批
2. 有原始判定证据
3. 有责任人+临时措施+确认根因+纠正措施
4. 有独立复检记录
5. 复检通过且质量工程师确认措施有效

---

## 3. 技术栈与代码组织

### 3.1 技术选型（锁定）

| 项 | 选型 | 备注 |
| --- | --- | --- |
| 语言 | Python 3.11+ | |
| Web | FastAPI + 内置静态页 | 单进程，零前端构建 |
| DB | PostgreSQL 15（Docker 容器） | 声明式约束 + JSONB + 行级锁；compose.yaml / scripts/start-pg.ps1 管理 |
| ORM | SQLAlchemy 2.0 | 显式事务边界 |
| 校验 | Pydantic v2 | JSON Schema 约束 |
| 编排 | LangGraph（Python） | 五段主链 Workflow 编排、状态管理、节点执行、Human-in-the-loop |
| 持久化 | LangGraph Checkpointer（PostgreSQL） | 断点恢复、故障续跑、时间旅行回看 |
| 测试 | pytest + httpx (async) | 分层测试 |

### 3.2 依赖方向（唯一固定约束）

```
接入层 (FastAPI) → 编排层 (LangGraph 图 + 节点) → 能力层 (Plugin) → domain (纯函数)
                                                          ↓
                                                     数据层 (SQLAlchemy + Postgres)
                                                          ↓
                                                     Checkpointer (LangGraph 状态持久化)
```

**domain 层不依赖 infra 层。** 判定/断言/状态机是纯函数，不读库、不取时间、不调模型、不 import LangGraph，可脱离数据库与模型独立测试。Skill 与 Plugin 只做编排与 IO；LangGraph 节点是 Skill/Plugin 的封装壳，不直接写业务判断。

### 3.2.1 LangGraph 工作流设计

LangGraph 作为编排层实际执行框架，替代原「AGH Skill/Plugin」抽象。核心映射：

| 架构概念 | LangGraph 对应 | 说明 |
| --- | --- | --- |
| 五段主链 | 一张主图（`main_chain_graph`） | 段间转移由 LangGraph 边 + 条件路由表达 |
| 段内步骤 | `add_node` | 每个 Skill/Plugin 调用封装为一个节点 |
| 批次状态 | 自定义 `BatchWorkflowState`（TypedDict） | 含 `batch_id`、当前状态、检验记录、异常、证据引用、人工反馈字段 |
| 人工确认（低置信） | `interrupt()` | 检查点挂起，等 `Command(resume=...)` 恢复；对应 §9.7 人工确认 API |
| 归因假设采纳 | `interrupt()` | 质量工程师三选一（采纳/驳回/修改）后恢复 |
| 放行断言 | 断言节点（纯函数，domain 层） | 断言失败直接路由到拒绝分支，不 interrupt |
| 故障恢复 | Checkpointer + 断点重放 | 节点失败时从最后成功检查点续跑，已成功节点不重放 |
| 轨迹留痕 | 检查点历史 + `get_state_history` | 任务轨迹即 LangGraph 检查点序列，满足架构 §7.2 可复演 |

**两张 LangGraph 图**：

1. `main_chain_graph`：五段主链 + 状态转移，覆盖 待入场 → 已归档 全路径
2. `incident_subgraph`：异常子图（归因 → CAPA → 复检 → 断言），作为主图「已隔离」分支的子图挂载

**约束**：
- 节点必须是确定性封装：同一状态输入 + 同一模型输出 → 同一图状态输出。模型随机性通过固定 seed（抽样）与可回放测试（固定模型输出）控制
- 人工 interrupt 点仅三处：低置信字段确认、归因假设采纳、放行前最终批准（对应人工三职责）
- 断言失败、SN 冲突、规则缺失**不 interrupt**，直接路由到隔离/拒绝分支（确定性路径）
- Checkpointer 用 PostgreSQL 后端（`langgraph-checkpoint-postgres`），`thread_id = batch_id`，支持时间旅行回看

### 3.3 目录结构（示意，可微调）

```
zhl/
├── app/
│   ├── api/          # FastAPI 路由
│   ├── workflow/     # LangGraph 图与节点（main_chain_graph / incident_subgraph）
│   ├── skills/       # Skill（编排语义，被 workflow 节点封装）
│   ├── plugins/      # Plugin（能力层）
│   ├── domain/       # 纯函数（判定/断言/状态机）
│   ├── infra/        # SQLAlchemy 模型 + 仓库 + Checkpointer 配置
│   ├── governance/   # 证据/轨迹/报告
│   └── static/       # 内置页面
├── tests/
│   ├── domain/
│   ├── plugins/
│   ├── workflow/     # LangGraph 图回放测试 + interrupt/resume 断言
│   └── e2e/
├── docs/
├── dev-notes/
├── AGENT.md
└── pyproject.toml
```

---

## 4. 模块并行拆分（业务价值切法）

> 用户已确认按**业务价值切法**。先落共享层，再并行两条工作流。

### 4.0 并行策略

```
Phase 0 (串行，共享契约) ──→ Phase A ∥ Phase B (并行) ──→ 集成
```

### Phase 0 · 共享契约（必须先行，两条流共用）

| 模块 | 内容 | 产出 |
| --- | --- | --- |
| DB Schema | 全部表 DDL + 约束 + 索引 | `migrations/001_init.sql` |
| domain 纯函数 | 判定引擎、放行断言、状态转移表 | `app/domain/` |
| 数据模型 | SQLAlchemy 模型 + Pydantic schema | `app/infra/models.py` |
| 证据库接口 | `EvidenceStore` Protocol + 实现骨架 | `app/plugins/evidence_store.py` |
| 测试夹具 | 四组验收样例数据 + 规则版本种子 | `tests/fixtures/` |

**Phase 0 完成标准**：判定引擎 + 状态机 + 断言的纯函数单测全绿，DB 迁移可独立执行。

### Phase A · 数据入场 + 规则判定（工作流 A）

| 模块 | 内容 | 依赖 |
| --- | --- | --- |
| Intake API | `POST /api/intake` 文件上传 + 任务状态查询 | Phase 0 |
| 结构化抽取 Skill | 读 CSV/模拟设备 → 字段+置信度 → 人工确认任务 | Phase 0 |
| 判定 API + Skill | `POST /api/batches/{id}/judge` → 逐组件判定 + 批次状态 | Phase 0 |
| 抽样 Plugin | ISO 表查 + 样本冻结（按 O-1 锁定结果实现） | Phase 0 |
| 批次详情 API | `GET /api/batches/{id}` 含判定证据与抽样结论 | Phase 0 |

**Phase A 交付物**：批次从「待入场」推进到「待判定」或「待放行」，含完整判定证据。

### Phase B · 异常闭环 + 报告归档（工作流 B）

| 模块 | 内容 | 依赖 |
| --- | --- | --- |
| 异常 API | 异常创建/指派/CAPA 确认 | Phase 0 |
| 归因 Skill + Traceability Plugin | 假设生成 + 证据校验 + 影响范围 | Phase 0 |
| 复检 API + Reinspection 记录 | 复检创建、不覆盖初检 | Phase 0 |
| 放行 API | `POST /api/batches/{id}/release` 逐条断言 | Phase 0 |
| 报告 Skill + 报告 API | 放行报告/处置报告 + 版本化 | Phase 0 |
| 状态机完整路径 | 已隔离→待处置→待复检→已放行/已拒绝 全链路 | Phase A 的判定输出 |

**Phase B 交付物**：批次从「已隔离」经归因、复检、断言到「已放行」或「已拒绝」，含完整证据链。

**Phase B 与 Phase A 的接口契约**（共享，Phase 0 定义）：
- 判定输出：`Decision` 对象含 `result`, `checks`, `rule_snapshot`
- 异常触发：`QualityIncident.trigger_type` 枚举 + 关联 SN
- 放行断言：`AssertionResult` 逐条结构

### 4.1 两条工作流的并行边界

| 事项 | Phase A 负责 | Phase B 负责 | 共享 |
| --- | --- | --- | --- |
| DB 迁移 | 批次/检验/抽样表 | 异常/复检/报告/证据表 | Phase 0 统一定义全部表 |
| domain | 判定引擎 + 抽样 | 状态机 + 断言 + 异常关闭 | Phase 0 定义 Protocol |
| API | intake / judge / batch detail | incident / recheck / release / report | Phase 0 定义路由骨架 |
| 页面 | 批次列表 + 判定详情 | 异常详情 + 放行断言 + 报告 | 共用同一静态目录 |

**集成点**：Phase A 完成后，Phase B 可直接消费批次判定结果。若 Phase A 未就绪，Phase B 可用 mock 判定结果驱动异常链路（测试夹具提供）。

---

## 5. 待决事项锁定

| 编号 | 事项 | 锁定条件 | 影响 |
| --- | --- | --- | --- |
| O-1 | ISO 抽样方案 | 开发前锁定 | 本 Spec 按**方案一（插件级）**实现；若升级为方案三需追加「待抽样」状态 |
| O-2 | 历史数据可得性 | 开发前确认 | 决定 C-8 规则阈值推荐做/不做；AI 归因验证方式 |
| O-3 | 规则阈值推荐存废 | 依赖 O-2 | P1，不影响 P0 交付 |

**本 Spec 假设**：O-1 = 方案一，O-2 = 历史数据不可得（归因用模拟数据验证，规则推荐降级 P1）。若 O-2 确认可得，则 C-8 升级为 P0。

---

## 6. 验收标准

### 6.1 四组样例（必须可复现）

| 样例 | 输入 | 预期 |
| --- | --- | --- |
| 正常放行 | 10 组件全 Pass | 待入场→待判定→待放行→已放行→已归档 |
| 边界值 | Pmax = 功率下限 | 判 Pass；对照组 Pmax = 下限 − 0.1 判 Fail |
| 异常隔离复检 | Impp 超档 + 历史同类 | 待入场→已隔离→待处置→待复检→待放行→已放行 |
| 数据一致性 | 同 SN 跨批次 | 创建一致性异常，两批次均不可放行 |

### 6.2 P0 完成标准（四条链路可复现）

- [ ] 完整放行链路（样例一）
- [ ] 完整异常隔离→复检→放行链路（样例三）
- [ ] 放行断言阻断场景（样例三中间态）
- [ ] 四组样例 + 完整任务轨迹

### 6.3 AI 落点

- P0 必选：结构化抽取（含置信度）+ 证据化归因
- P1 可选：规则阈值推荐（视 O-2）

---

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 并行集成冲突（DB/状态机） | 中 | Phase 0 锁定全部契约，Phase A/B 只读共享模块 |
| LangGraph 节点/Checkpointer 契约不稳定 | 中 | 先实现 domain + Plugin，节点编排层可替换；依赖锁在 uv.lock |
| ISO 抽样表数据准确性 | 低 | 预置固定表，测试夹具覆盖边界 |
| 归因幻觉 | 高 | 证据存在性校验 + 比例阈值拒绝（架构 §6.2） |
| 单进程性能瓶颈 | 低 | P0 不优化，批量判定用批量接口 |

---

## 8. 开发规范（摘要，完整见 AGENT.md）

- 过程留痕：`dev-notes/{开发人员ID}` 追记，每阶段必记
- 库/框架用法：Context7 MCP 先查后写
- 完结交付：功能演示命令 + 测试结果 + dev-notes 路径
- 详见根目录 `AGENT.md`

---

## 9. 实现层默认决策（Builder 拍板，可推翻）

> 以下为本 Spec 作者替开发拍板的默认项，依据上游文档推导。若与评审意见冲突，以评审为准。

| 项 | 默认决策 | 依据 |
| --- | --- | --- |
| 前端形态 | 纯 HTML + 原生 JS + Fetch，零构建；6 个页面：批次列表、批次详情（含判定证据+抽样）、人工确认队列、异常详情（含假设排序）、放行断言视图、报告查看 | 架构 §8.1「不引入前端构建链，单进程」 |
| 规则版本管理 | P0 通过 SQL 种子 + 只读 API `GET /api/rules/{versionId}`；创建/发布走独立管理接口 `POST /api/rules`（P1），历史版本不可改由 DB 触发器保证 | 需求 §3.1「发布后不可修改」 |
| 批次创建 | `POST /api/batches` 显式创建草稿批次（指定 projectId/model/ruleVersionId/plannedQuantity）；`POST /api/intake` 必须携带 `batchId`，向已有批次摄入数据 | 主链第 1 段输入为「批次已存在」 |
| 证据文件存储 | 本地目录 `./evidence_store/`，可配置 `EVIDENCE_DIR`；入库时计算 SHA-256 哈希存证据表 | 架构 §7.3 |
| ISO 抽样表 | 预置 3 种检验类型 × 3 个 AQL 等级（0.65/1.0/1.5）× 批量区间，约 30 项；缺表项 → 判定规则异常 | 需求 §7 方案一 |
| 部分判定 | 批次停留在 `pending_judge` 直至全部组件判定完成，才允许转移；允许多次增量判定 | 状态机语义 |
| 人工确认 API | `POST /api/tasks/{taskId}/confirm`（确认/修改/拒绝）；低置信字段进队列 | 需求 §4.1 FR-1.5/1.6 |
| 复检数据 | 复检可上传新 CSV 或手工录入，生成独立 `Reinspection` 记录，重跑同版本判定 | 需求 §3.5 |
| 报告格式 | Markdown 渲染为 HTML 页面 + 可下载 .md；版本号自增不覆盖 | 需求 §4.5 FR-5.5 |
| 认证 | P0 不做登录，操作人通过请求头 `X-Operator` 标识 | 单用户演示场景 |

这些默认项不影响 Phase 0 共享契约的启动。

---

## 9. 实现细节补充（builder-check 后）

> 以下条目是 spec 初稿未覆盖、但 builder 计划时需要锁定的实现约束。均为「我拍板」的默认值，如有异议在 spec 评审时提出。

### 9.1 模型服务接入

P0 使用**可插拔 LLM 客户端**，通过 `LLMClient` Protocol 定义 `chat(prompt, schema) -> dict`。默认实现调用 OpenAI-compatible API（环境变量 `ZHL_LLM_API_KEY` + `ZHL_LLM_BASE_URL` + `ZHL_LLM_MODEL` 配置）。测试与演示时使用 `MockLLMClient` 返回固定 JSON 输出（测试夹具提供）。**不绑定具体供应商**，便于切换。

### 9.2 LangGraph 节点契约

架构文档的 `Plugin` Protocol 保持原样：

```python
class Plugin(Protocol):
    def name(self) -> str: ...
    def health_check(self) -> HealthStatus: ...
    def invoke(self, payload: dict, ctx: TaskContext) -> PluginResult: ...
```

`TaskContext` 携带 `task_id`, `evidence_writer`, `operator`。**Plugin 不自行写库**，通过 `ctx.evidence_writer` 写入。Skill 层做编排，调用 Plugin 完成确定性操作。

P0 阶段 Skill/Plugin 被 LangGraph 节点封装：每个节点是一个纯函数（或 `add_node` 注册的 callable），接收 `BatchWorkflowState`，返回状态更新字典。节点内部调用 Skill/Plugin，不直接写业务判断。LangGraph `interrupt()` 对应人工确认点，`Command(resume=...)` 恢复执行。Checkpointer 用 PostgreSQL 后端（`langgraph-checkpoint-postgres`），`thread_id = batch_id`。

### 9.3 规则版本管理

- **创建**：`POST /api/rules` 创建草稿（status = draft）
- **发布**：`POST /api/rules/{id}/publish` 不可逆，发布后触发器拒绝任何 UPDATE
- **停用**：`POST /api/rules/{id}/disable` 可逆（不影响历史结论）
- **种子数据**：`tests/fixtures/rules_seed.json` 提供 2 个型号（JH-550W 默认 + 一个测试用型号），初始化时幂等导入

### 9.4 ISO 抽样表

- **数据源**：预置 `iso2859_1_aql.json`（GB/T 2828.1 等效），字段：`[inspection_type, inspection_level, aql, lot_size_min, lot_size_max] → [n, ac, re]`
- **缺失处理**：查不到组合 → 判定为规则异常，禁止按默认值放行（与架构 §10 一致）
- **样本冻结**：抽样时生成分层随机样本（固定 seed = batch_id），存入 `samples` 表，`sample_conclusions` 关联冻结样本清单

### 9.5 证据文件存储

- **路径**：`data/evidence/`（相对于项目根目录），按 `evidence_type/{yyyy}/{mm}/{evidence_id}/{filename}` 组织
- **运行时定位**：配置 `ZHL_EVIDENCE_ROOT` 环境变量，默认 `data/evidence`
- **哈希**：入库时计算 SHA-256，存 `evidence.content_hash` 列
- **不入库**：大文件不存 DB，DB 只存元数据 + 哈希 + 文件路径

### 9.6 批次部分判定语义

- 批次状态 `pending_judge` 表示「至少一个组件已摄入待确认」
- 判定触发（`POST /api/batches/{id}/judge`）要求批次内**全部**组件 `intake_status = confirmed`，否则返回 400 列出未确认的 SN
- 判定后批次状态根据结果整体转移（全部 Pass → pending_release，任一 Fail → isolated），不支持部分批次

### 9.7 人工确认 API

- `POST /api/intake/{task_id}/confirm` 接收修改后的字段值（可选），写入 `module_inspections`，`intake_status = confirmed`
- 未修改直接确认 → `human_diff = []`；修改了 → 记录修改前后值
- 拒绝：`POST /api/intake/{task_id}/reject` 标记 `intake_status = rejected`，保留抽取证据

### 9.8 认证与角色

P0 **不做认证**，所有操作视为单一质量工程师身份（`operator = "quality-engineer"`）。若后续需要多角色，加 JWT 中间件即可，domain 层不受影响。

### 9.9 静态页面范围

P0 实现 6 个最小可用页面（单 HTML + 原生 JS fetch，无构建链）：

1. 批次列表（状态筛选 + 进度）
2. 批次详情（判定证据 + 抽样结论 + 状态时间线）
3. 异常列表 + 详情（假设排序 + 影响范围 + CAPA）
4. 放行断言展示（逐条结果 + 阻断原因）
5. 报告查看（版本列表 + 内容）
6. 任务轨迹查看（Agent 全留痕）

页面为演示与验收用，非生产级 UI。API 为唯一数据源，页面只是 API 的渲染层。

### 9.10 报告格式

- 结构化：JSON（含全部断言结果 + 证据引用）
- 可读：Markdown 摘要（批次信息、判定统计、异常摘要、放行断言）
- 触发：`POST /api/batches/{id}/report` 生成新版本，不覆盖历史
- 模板：`templates/report_release.md` + `templates/report_incident.md`
