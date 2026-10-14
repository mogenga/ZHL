# 前端重构规范 · 批次工作台（workbench）

> 日期：2026-10-13 · 状态：待评审
> 范围：仅重构 `app/static/`，后端零改动
> 交互与视觉基准：`design-demos/design-a-batch-workbench.html`（已定稿，可微调视觉，不可改结构）

---

## 0. 目标与非目标

**目标**：将 9 个平铺的 MPA 页面（index/intake/batch/incidents/incident/recheck/release/report/trace）收敛为一个以「批次」为主体的单页工作台 `app/static/workbench.html`。用户从新建批次到归档全程无跳页；任一时刻界面只呈现当前批次状态下唯一合法的下一步操作。

**非目标（后端未实现，前端不得虚构）**：
- 规则版本创建/发布/停用（`GET /api/quality-rule-versions` 只读）
- 异常关闭/报废端点（closed/scrapped 两态经 API 不可达）
- 报告自然语言摘要与追问（FR-5.6，P1）
- 阈值推荐、抽样方案三 UI、跨批次关联图（P1）
- 登录与角色权限（系统无任何鉴权；责任人/批准人皆为自由文本）

**反目标（做了即失败）**：
- 界面出现内部英文字段名、枚举值、API 路径
- 界面出现面向开发者的流程解释/操作指引文字
- 跳出工作台的多页跳转（旧页面仅作回退保留，不作为入口）

---

## 1. 信息架构

三栏布局 + 顶栏，参照 demo：

```
┌────────────────────────────────────────────────────────┐
│ 顶栏：品牌 · 「＋ 新建批次」                               │
├──────────┬──────────────────────────────┬──────────────┤
│ 左列 280px│ 中栏（主工作区）               │ 右栏 320px    │
│ 四视图切换│  批次头（批次号/状态/项目/型号/  │ 批次轨迹时间线 │
│ 批次/规则库│   规则版本/计划数量）           │ （来自 traces）│
│ /异常/报告│  六节点流水线（当前节点高亮，    │ 关键指标       │
│ + 搜索    │   历史节点可点击回看只读）      │               │
│          │  当前节点操作面板（含明细表）    │               │
└──────────┴──────────────────────────────┴──────────────┘
```

### 1.1 左列四视图

| 视图 | 内容 | 数据源 |
|---|---|---|
| 批次 | 搜索框；分组：进行中 / 已完结（含已归档单独分组，支持 `GET /api/batches?status=` 过滤）；每项显示批次号、状态中文标签、项目·型号·计划数量 | `GET /api/batches` |
| 规则库 | 版本列表：名称、项目、型号、版本号、状态、生效时间。**仅此六项，无条目明细、无发布操作** | `GET /api/quality-rule-versions`（注意 `available:false, reason:"table_missing"` 时显示空态提示） |
| 异常 | 全部异常列表：异常号、批次、触发原因、严重度、责任人、状态；点击「定位批次」切回批次视图并选中对应批次 | `GET /api/incidents` |
| 报告 | 报告列表：报告号、批次、类型（放行/异常处置）、版本号、生成时间；点击查看报告内容 | `GET /api/batches/{id}/reports`（按当前选中批次；未选中批次时列全部在制批次的报告） |

### 1.2 中栏：批次头 + 流水线 + 节点面板

- **批次头**：批次号、状态中文标签、项目、型号、计划数量、规则版本名称。**不显示实际数量**（无 API 返回）
- **六节点流水线**：数据入场 → 判定 → 异常处置 → 复检 → 放行 → 报告
  - 当前节点高亮；已过节点可点击回看（只读 + 「返回当前节点」按钮）；未来节点禁用
  - 节点与状态的对应关系见 §2
- **当前节点操作面板**：该状态下唯一合法的操作 + 相关明细表（见 §2）
- 所有写操作走表单弹窗（modal），禁止原生 prompt/alert/confirm

### 1.3 右栏：轨迹 + 指标

- 轨迹时间线：`GET /api/batches/{id}/traces`，每条显示时间 + 环节名 + 结果（通过/失败/部分）
- 关键指标：仅使用真实可得字段（判定通过/失败计数、异常数、当前状态停留等）；**不显示实际数量**
- **信息可回看性边界**：入场置信度、抽取字段、低置信组件清单仅存在于 `POST /api/intake` 的即时响应中，无查询端点。提交后在面板内即时展示；刷新页面或切换后回看数据入场节点时，降级显示组件条码与入场状态（来自批次详情），置信度等信息不重建

### 1.4 路由与上下文恢复

- URL 参数：`workbench.html?batch=<batch_id>`（可扩展 `&incident=`）
- 刷新后按参数恢复选中批次与视图；批次列表点击即更新 URL（history.replaceState）

---

## 2. 状态 → 界面映射（核心）

### 2.1 批次状态机 → 节点与操作

| 批次状态 | 中文标签 | 流水线节点 | 可用操作（唯一合法集） | 明细表内容 |
|---|---|---|---|---|
| `pending_intake` | 待入场 | 数据入场 | 提交送检数据；确认入场 / 拒绝入场 | 送检任务状态、抽取置信度、低置信组件清单、抽取字段对照 |
| `pending_judge` | 待判定 | 判定 | 重新判定（刷新明细，不改变批次状态，见 §4.4） | 组件条码、入场状态、判定状态、逐条规则结果（取自批次详情） |
| `isolated` | 已隔离 | 异常处置 | 若无异常：创建异常；若有 `new` 状态异常：隔离异常 | 异常卡片（见 §2.2） |
| `disposing` | 处置中 | 异常处置 | （异常侧驱动，见 §2.2） | 异常卡片 + 归因假设 |
| `pending_recheck` | 待复检 | 复检 | 录入复检结果（表单见 §4.5） | 复检记录列表 |
| `pending_release` | 待放行 | 放行 | 执行放行断言 → 全部通过后：批准放行 / 拒绝批次 | 七项断言逐项结果（§4.6） |
| `released` | 已放行 | 报告 | 生成放行报告 / 查看报告（生成成功即自动归档，**无独立归档按钮**——后端无归档端点） | 报告列表 |
| `rejected` | 已拒绝 | 报告 | 生成异常处置报告 / 查看报告（生成后自动归档，同上） | 报告列表（仅 incident 类型） |
| `archived` | 已归档 | 报告（终态） | 无（全只读） | 报告列表 |
| `draft` | 草稿 | —（不出现在正常流程） | — | — |

**规则**：
- 每个状态只渲染该状态下合法的操作按钮，未来阶段入口一律不渲染
- 状态推进后：左列表、中面板、右轨迹三处同步刷新
- 入场置信度 ≥0.8 时后端自动确认，批次直接进入 `pending_judge`——前端不得假设必须经过人工确认
- **状态流转的真实路径**：确认入场时后端图同步完成判定与抽样，批次由 `pending_intake` 直接进入 `pending_release`（全部通过）或 `isolated`（有失败）；`pending_judge` 由自动确认入场产生。判定节点面板因此以批次详情的 `inspections[].decision_status / decision_evidence` 为数据源，而不是判定端点响应

### 2.2 异常子状态机 → 操作顺序（严格）

```
new ──隔离(isolate)──▶ isolated ──AI归因(attribute)──▶ … ──指派(assign)──▶ assigned
      ──填报整改措施(capa)──▶ pending_recheck ──录入复检──▶ 通过:批次转待放行 / 失败:双双转已拒绝
```

- **顺序硬约束**：`new` 状态下界面只显示「隔离异常」一个操作；直接指派会返回 409（`incident_not_isolated`，后端带 hint）
- **AI 归因**为异常处置中的独立环节：触发 `attribute` → 展示假设列表（可能为空，后端返回 hint）→ 用户三选一：采纳（填根因）/ 驳回 / 搁置
- 归因不改变状态，可在指派前后任意时机进行；但推荐顺序为 隔离 → 归因 → 指派 → 整改措施
- **整改措施**（capa）表单：确认根因、纠正措施、是否已验证（`verified=true` 需已有通过的复检记录，否则 400）
- 异常面板始终显示：异常号、触发原因、严重度、责任人、处置进度（隔离→归因→指派→整改→复检）

### 2.3 左列状态分组

- 进行中：`pending_intake` ~ `pending_release` + `released`/`rejected` 未归档
- 已完结：`archived`（单独分组/标记，区别于 merely released/rejected）

---

## 3. 数据字段事实表（有什么展示什么，不得虚构）

### 3.1 端点清单

| 端点 | 方法 | 用途 |
|---|---|---|
| `/api/batches` | POST / GET | 创建批次 / 列表（`?status=` 过滤） |
| `/api/batches/{id}` | GET | 批次详情（含 inspections/sample_result/status_timeline） |
| `/api/batches/{id}/judge` | POST | 触发判定 |
| `/api/intake` | POST | 提交送检数据 |
| `/api/intake/{task_id}/confirm` | POST | 人工确认/拒绝入场 |
| `/api/incidents` | POST / GET | 创建异常 / 列表 |
| `/api/incidents/{id}` | GET | 异常详情 |
| `/api/incidents/{id}/isolate` · `/assign` · `/capa` · `/attribute` | POST | 异常处置动作 |
| `/api/incidents/{id}/reinspections` | POST / GET | 录入复检 / 复检列表 |
| `/api/batches/{id}/release` · `/reject` | POST | 放行断言+批准 / 人工拒绝 |
| `/api/batches/{id}/report` | POST | 生成报告 |
| `/api/batches/{id}/reports[/{report_id}]` | GET | 报告列表 / 详情 |
| `/api/batches/{id}/traces` | GET | 轨迹 |
| `/api/quality-rule-versions` | GET | 规则版本列表 |

### 3.2 响应字段（前端可展示的全部事实）

**批次列表项**：`batch_id, batch_no, project_id, model, status, planned_quantity, created_at`
**批次详情**：`batch_id, batch_no, status, planned_quantity, rule_version_id`、`inspections[]`（`sn / intake_status / decision_status / decision_evidence`）、`sample_result`（可为 null）、`status_timeline[]`（`skill_name / target / accepted / started_at`）。**注意：详情不返回 project_id/model/created_at——批次头的项目与型号取自批次列表数据按 batch_id 关联**（新建批次成功后回读列表一次）；规则版本名称由 `quality-rule-versions` 列表按 `rule_version_id` 反查，无法解析（表缺失/匹配不到）时显示「规则版本信息暂不可用」，不显示原始 UUID
**judge 响应**：`result`（Pass/Fail）、`counts{Pass,Fail}`、`fail_sns[]`、`per_sn{sn:{result, checks[{rule,passed,detail}]}}`
**intake 响应**：`task_id`（=批次ID）、`status`（pending/confirmed）、`extracted_fields`、`confidence`、`low_conf_sns[]`
**intake confirm 响应**：`task_id, intake_status`（confirmed/rejected）
**IncidentOut（13 字段）**：`incident_id, batch_id, trigger_type, severity, status, owner, due_at, containment_action, confirmed_root_cause, corrective_action, capa_verified, root_cause_hypotheses, verification_id`
**attribute 响应**：`incident_id, decision, hypotheses[], confirmed_root_cause, hint`
**ReinspectionOut**：`reinspection_id, incident_id, sn, passed, scope_applied, scope_sns[], result`
**ReleaseResponse**：`passed, batch_id, status, release_decision, checks[{name, passed, reason}]`
**RejectResponse**：`batch_id, status, auto_archived, reason, report_id`
**ReportOut**：`report_id, batch_id, report_type（release/incident）, version, content_md, content_json, source_snapshot_id, agent_task_id, generated_at`
**trace 条目**：`task_id, skill_name, input_ref, plugins_invoked, params_summary, validation_result, failure_reason, human_action, started_at`
**规则版本项**：`rule_version_id, project_id, model, name, status, version_no, effective_at`

### 3.3 禁区（无数据，不得展示）

- 「实际数量」（无端点返回 actual_quantity）
- 报告「签署人」（Report 表无此列；approved_by 只存在于 release 请求，不在报告响应中）
- 规则条目明细（无 API）
- §0 非目标清单中的全部功能

---

## 4. 关键交互规范

### 4.1 新建批次（表单弹窗）

字段：批次号*、项目*、型号*、计划数量*（≥1）、规则版本*（下拉，来自 `/api/quality-rule-versions`，仅列 `status=published` 且已生效版本；`available:false` 时提示「规则版本未初始化」）。成功后创建并直接选中该批次，进入数据入场节点。

### 4.2 提交送检数据（数据入场第一步）

表单：数据来源（下拉：IV_CSV / OCR_FORM / MANUAL）、检测数据（多行文本，每行一个组件的实测值；至少含组件条码）、人工覆盖字段（可选，白名单：pmax/temp/isc/voc/vm/im/ff/外观结论）。调 `POST /api/intake`。响应展示：整体置信度、抽取字段、低置信组件清单。`status=confirmed`（≥0.8 自动确认）时直接提示「已自动确认入场」。

### 4.3 确认 / 拒绝入场

- 确认：`action:"confirm"`
- 拒绝：`action` 传 confirm 以外的值（如 `"reject"`）
- 确认成功后批次进入待判定/待放行/已隔离（由后端图同步决定，前端以刷新后的批次状态为准）
- **拒绝入场的真实行为**：批次状态保持 `pending_intake`（后端不写批次状态），组件 `intake_status=rejected`。界面如实呈现：批次仍显示「待入场」，入场面板列出各组件的入场状态（已拒绝）；允许重新提交送检数据（组件条码已属本批次不会冲突；若换批号提交相同条码会触发跨批次冲突提示，见 §4.9）

### 4.4 判定节点

判定在入场确认时由后端自动完成。判定节点面板为**回看视图**，数据源是 `GET /api/batches/{id}` 的 `inspections[]`（每组件 `decision_status` + `decision_evidence` 逐条规则结果）与 `sample_result`。「重新判定」按钮调 `POST /api/batches/{id}/judge` 刷新明细（注意：该端点只重算不推进状态机；存在未确认组件时 400，展示 `unconfirmed_sns` 清单）。

### 4.5 录入复检（表单字段必须完整）

POST `/api/incidents/{id}/reinspections` 请求体全部字段：
- 关联检测记录（inspection 选择，来自批次 inspections）
- 实测值七项：`pmax, temp, isc, voc, vm, im, ff`（数值，必填）
- 结论 `result`（合格/不合格）
- 执行人 `executor`、执行时间 `executed_at`
- 复检范围 `scope_type`：最小（服务端自动取异常全量组件）/ 扩大 / 整批——后两者必须填 `scope_sns` 且覆盖最小集
- **范围缺口**：400 `scope_gap` 时在表单内展示缺失组件条码清单
- 结论=不合格 → 异常与批次双双转已拒绝（界面提示并跳转报告节点）

### 4.6 放行（单步交互）

`POST /api/batches/{id}/release` 是原子端点：请求体仅 `approved_by`；断言全通过时同一请求完成放行，任一失败则不改状态、返回完整 `checks[]` 可重试。因此界面为单步：

1. 用户填写批准人 → 点击「批准放行」
2. 响应 `passed=true`：提示「已批准放行」，批次转入报告节点
3. 响应 `passed=false`：面板展示七项断言逐项结果与失败原因（含后端指名的异常号/组件条码/证据），提供「重新检查」按钮重试；此时**不得**显示「批准放行已成功」类误导文案

断言名称中文化：

| 断言 name | 界面中文名 |
|---|---|
| `state` | 批次状态就绪 |
| `sample_passed` | 抽样通过 |
| `no_open_critical_incident` | 无未关闭严重异常 |
| `no_isolated_sn` | 无隔离组件 |
| `all_evidence_present` | 证据齐全 |
| `all_capa_verified` | 整改措施已验证 |
| `reinspection_scope_covered` | 复检范围覆盖完整 |

### 4.7 拒绝批次

表单必须填：拒绝理由*、拒绝人*。调 `POST /api/batches/{id}/reject`。成功后提示「异常处置报告已生成，批次已自动归档」（响应含 `auto_archived` 与 `report_id`，如实展示）。

### 4.8 报告

- 生成：选择类型（放行/异常处置）；**已拒绝批次只能选异常处置类型**（否则 409）
- 幂等：重复生成返回 200 既有版本 → 提示「已返回既有版本」，不升版
- 自动归档：released/rejected 批次生成对应类型报告后同事务归档
- 查看：渲染 `content_md`（markdown）+ 元信息（类型、版本号、生成时间）

### 4.9 错误呈现

所有 4xx 在界面内以行内提示（表单下方或面板顶部）呈现，映射为中文业务提示：

| 错误标识 | 触发 | 界面提示（示例） |
|---|---|---|
| `batch archived, terminal state`（409） | 归档后任何写操作 | 「批次已归档，内容只读」 |
| `batch_not_isolated`（409） | 非 isolated 批次创建异常 | 「判定失败后批次隔离才能创建异常」 |
| `incident_not_isolated` + hint（409） | new 状态直接指派 | 「请先隔离该异常」 |
| `incident_not_new`（409） | 非 new 状态隔离 | 「该异常已隔离」 |
| `sn_not_found`（400, missing[]） | 异常组件不在批内 | 展示缺失条码清单 |
| `evidence_not_found`（400, missing[]） | 证据不存在 | 展示缺失证据 |
| `scope_sns_required`（400） | 扩大/整批未填范围 | 「请填写复检范围组件条码」 |
| `scope_gap`（400, missing[]） | 范围未覆盖最小集 | 展示缺失条码清单 |
| `unknown_scope_type`（400） | 范围类型非法 | 「复检范围类型不正确」 |
| `inspection_not_found`（400） | 检测记录不存在 | 「请重新选择组件」 |
| `unconfirmed_sns`（400, []） | 有未确认组件就触发判定 | 展示未确认条码清单，「请先完成入场确认」 |
| `rule not effective yet`（400） | 规则未生效 | 「规则版本尚未生效，无法判定」 |
| `cross_batch_sn_conflict`（400） | 组件条码已属其他批次 | 「组件条码已存在于其他批次」 |
| `batch_not_pending_release`（409） | 非待放行状态执行放行 | 「批次当前状态不可放行」 |
| `rejected_batch_only_incident_report`（409） | 拒绝批次生成放行报告 | 「已拒绝批次只能生成异常处置报告」 |
| `capa_verified_requires_pass_reinspection`（400） | 整改验证无通过复检 | 「需先有通过的复检记录才能勾选已验证」 |
| 404 / 422 | 资源不存在 / 参数畸形 | 「数据不存在或已被删除」/「参数不正确」 |

---

## 5. 文案规范

- 用户可见文案全部中文业务用语；禁止出现内部英文字段名、枚举值、API 路径
- 术语对照（必须遵守）：

| 内部值 | 界面文案 |
|---|---|
| `rule_fail` / `sample_fail` / `appearance_manual` / `data_consistency` / `rule_missing` | 规则失败 / 抽样失败 / 外观人工触发 / 数据一致性 / 规则缺失 |
| `critical` / `major` / `minor` | 严重 / 较严重 / 轻微 |
| `minimal` / `expanded` / `full_lot` | 最小范围 / 扩大范围 / 整批 |
| `release` / `incident`（报告类型） | 放行报告 / 异常处置报告 |
| CAPA | 整改措施 |
| scope | 复检范围 |
| assign | 指派责任人 |
| `pass` / `fail` / `pending` | 合格 / 不合格 / 待处理 |
| `adopt` / `reject` / `defer`（归因） | 采纳 / 驳回 / 搁置 |
| 批次状态 10 态 | 待入场/待判定/已隔离/处置中/待复检/待放行/已放行/已拒绝/已归档/草稿 |
| 异常状态 7 态 | 新建/已隔离/已指派/待复检/已关闭/已驳回/已报废 |

- 界面上不得出现任何面向开发者的说明性文字（流程解释、操作指引、字段枚举）；首次打开即可靠界面结构本身完成任务；需要指导时收纳在「？」帮助入口
- ID 类标识（批次号、组件条码 SN、报告号）保留原始格式，属于正常业务单据号

---

## 6. 视觉与技术规范

### 6.1 视觉（取自 demo 的 design tokens）

- 字体：Noto Sans SC（界面）+ IBM Plex Mono（数字/编号），本地回退 PingFang SC / Microsoft YaHei / monospace
- 色板：底 `#f3f5f8`，面板 `#fff`，线 `#e3e8ef`，墨 `#1c2534`；主色 `#0b5cad`（蓝）；状态色：成功 `#0e8a5f`、警示 `#b26a00`、危险 `#c0392b` 及各自浅底
- 状态标签用色需同时以文字承载含义（无障碍，不单靠颜色）
- 弹窗、抽屉、toast 交互样式沿用 demo

### 6.2 技术（实现者已定，可被推翻）

- **原生 JS + 单文件模块**（`workbench.html` + `workbench.js` + 复用/重写 `common.js` 的 fetch 三态封装：加载/成功/失败）——不引入构建工具、npm、CDN 运行时依赖；Google Fonts link 允许（需本地回退）
- 保持 FastAPI 静态挂载（`/static`）与现有 `main.py` 路由不变；新入口 `workbench.html`
- 旧 9 页保留可回退，总验收后再决定删除或 302 重定向
- 请求一律 JSON；`Content-Type: application/json`；4xx 解析 `detail` 字段（可为字符串或对象）渲染 §4.9 的映射

---

## 7. 过程约定

- 三阶段门禁：规范（本文档）→ 任务计划（`docs/superpowers/plans/<日期>-frontend-refactor.md`，用 writing-plans 流程）→ 逐任务实施；每个阶段/任务完成后停下等待确认
- 遵循仓库 `AGENT.md`：过程留痕写入 `dev-notes/`，不许收尾补记
- 每个实施任务须附验收步骤（浏览器人工验证点 + 控制台无报错）

## 8. 总验收清单

1. 从新建批次到归档全流程可走通，全程无跳页
2. 任一时刻界面上只有当前状态的合法操作；异常先隔离后指派
3. 放行走七项断言逐项确认；拒绝必填理由并提示自动归档
4. 界面无内部字段/英文枚举/API 路径；无面向开发者的说明性文字
5. 刷新后 URL 参数恢复当前批次；控制台无报错；中文无乱码
6. 后端代码零改动（`git diff` 中 `app/` 除 `static/` 外无变更）
