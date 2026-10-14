# 批次工作台（workbench）前端重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `app/static/` 的 9 个平铺 MPA 收敛为单页批次工作台 `workbench.html`，从新建批次到归档全程无跳页，每个状态只呈现唯一合法操作。

**Architecture:** 三栏单页（左=批次/规则库/异常/报告四视图，中=批次头+六节点流水线+当前节点操作面板，右=轨迹+指标）。原生 JS，全局命名空间 `WB`（沿用 `common.js` 的 `ZHL` 模式），按职责拆 4 个脚本文件，全部经 FastAPI `/static` 挂载。状态→界面映射由纯函数驱动，数据只来自 §3 事实表列出的端点。

**Tech Stack:** 原生 HTML/CSS/JS（零构建、零 npm、零 CDN 运行时依赖）；pytest 做 2 项静态守护测试；后端 FastAPI 只读复用。

**Spec:** `docs/superpowers/specs/2026-10-13-frontend-refactor-spec.md`（本计划从该规范论证，执行者须同时阅读两份文档）

## Global Constraints

- 后端零改动：`git diff` 中 `app/` 除 `static/` 外无变更（spec §8.6）
- 禁用原生 `alert(`/`prompt(`/`confirm(`，一律走 toast/modal/行内提示（spec §1.2）
- 用户可见文案全部中文；禁止出现内部英文字段名、枚举值、API 路径（spec §5，术语对照表以 spec §5 为准）
- 不显示「实际数量」「报告签署人」；规则库仅展示版本列表（spec §3.3 禁区）
- `actual_quantity`、`rule not effective yet` 等原文值在代码里仅作为 API 判断键，不得渲染
- 每个状态只渲染 spec §2.1 表中「可用操作」列的操作，未来阶段入口不渲染
- 请求统一 JSON；4xx 解析 `detail`（可为字符串或对象）走 spec §4.9 错误映射表
- 每个任务完成后在 `dev-notes/` 追加留痕（AGENT.md 约定）

## Review Focus

1. **原生对话框混入**：任何 wb 文件出现 `alert(`/`prompt(`/`confirm(` → 守护测试在 T1 建立，T10 扩展扫描范围
2. **状态中文映射漂移**：批次 10 态/异常 7 态缺译 → T10 的 pytest 直接 import `app/infra/models.py` 枚举与 JS 映射表对账
3. **`detail` 为字符串或对象两种形态**：后端各端点混用（如 `batch_not_found` 是字符串、`scope_gap` 是对象）→ T2 的 API 层统一解析，验收时逐类触发核对
4. **入场自动确认跳过人工步骤**（置信度 ≥0.8 直接 confirmed）：前端若假设必经人工确认会死等 → T4 验收步骤显式覆盖
5. **归档终态误写操作**：archived 批次面板仍可点写操作 → T9 验收覆盖（含 409 提示文案）

---

### Task 1: 工作台骨架与视图切换（mock 数据）

**Files:**
- Create: `app/static/workbench.html`（三栏骨架、四视图切换标签、容器节点、charset/viewport/title）
- Create: `app/static/wb.css`（spec §6.1 design tokens：`#f3f5f8/#fff/#e3e8ef/#1c2534/#0b5cad` 及状态色、mono 字体栈）
- Create: `app/static/wb/ui.js`（DOM 构建 `WB.h()`、toast、modal、drawer）
- Create: `app/static/wb/main.js`（视图状态 + 渲染调度 + URL 参数读写 `?batch=`）
- Test: `tests/static/test_workbench_static.py`（新建目录）

**Interfaces:**
- Produces: `WB.h(tag, attrs, ...kids)`（attrs 支持 class/text/on*/其他属性，kids 支持数组嵌套）；`WB.toast(msg)`；`WB.openModal(title, fields[], onOk)`（fields 元素：`{label, type:'text'|'select'|'textarea', options?, value?}`）；`WB.openDrawer(title, bodyLines[])`；`WB.switchView(name)`（name ∈ batch/rules/incidents/reports）；`WB.setBatch(id)`（更新 URL 并触发渲染）
- 消费 mock：本任务内置 `WB.mock` 常量（批次 3 条覆盖三种状态），T2 起替换为真实数据

- [ ] **Step 1: 写守护测试（先失败）**

`tests/static/test_workbench_static.py`：

```python
from pathlib import Path

WB_FILES = ["workbench.html", "wb.css", "wb/ui.js", "wb/main.js"]
ROOT = Path(__file__).resolve().parents[2] / "app" / "static"

def test_no_native_dialogs():
    for f in WB_FILES:
        src = (ROOT / f).read_text(encoding="utf-8")
        for banned in ("alert(", "prompt(", "confirm("):
            assert banned not in src, f"{f} 使用了原生对话框 {banned}"

def test_charset_declared():
    html = (ROOT / "workbench.html").read_text(encoding="utf-8")
    assert 'charset="utf-8"' in html
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/static/ -v`
Expected: FAIL（文件不存在）

- [ ] **Step 3: 实现骨架四文件**

`workbench.html` 引入 `wb.css` + 依次加载 `wb/ui.js`、`wb/main.js`；三栏容器 id：`leftPane / mainPane / rightPane`；四视图切换按钮 `data-view`。视觉对照 `design-demos/design-a-batch-workbench.html`（顶栏/左列/流水线样式搬移，配色取 tokens）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/static/ -v` → PASS

- [ ] **Step 5: 浏览器验收**

启动 `uv run uvicorn app.main:app --port 8000`，访问 `http://localhost:8000/static/workbench.html`：
1. 四视图切换正常，mock 批次列表渲染
2. 点击批次更新 URL 为 `?batch=<id>`，刷新后仍选中该批次
3. 控制台无报错

- [ ] **Step 6: Commit** `git commit -m "feat(workbench): 三栏骨架与视图切换（mock）"`

---

### Task 2: API 层 + 批次列表/规则库接入真实数据

**Files:**
- Create: `app/static/wb/api.js`
- Modify: `app/static/wb/main.js`（移除 mock，接真实列表）
- Modify: `app/static/workbench.html`（加载 `wb/api.js` 于 `ui.js` 之后）

**Interfaces:**
- Produces: `WB.api.request(method, path, body)` → `Promise<{ok, status, data, error}>`；`error` 统一为 `{key, msg, detail}`（key 取 spec §4.9 表的错误标识，`detail` 保留原始结构供清单渲染）；`WB.api.listBatches(status?)`、`WB.api.getBatch(id)`、`WB.api.listRuleVersions()`、`WB.api.listIncidents()`、`WB.api.listTraces(batchId)`
- Produces: `WB.errText(error)` → 中文提示文案（spec §4.9 映射表逐条实现，缺省「操作失败，请重试」）

- [ ] **Step 1: 实现 api.js**

三态封装（加载/成功/失败）沿用 `common.js` 思路：请求前置 loading 标记、非 2xx 解析 `detail`（字符串→`{key:null, msg:<detail>}`；对象→取第一个语义键如 `error`/`reason`/`unconfirmed_sns` 作 key）。超时 15s，网络错误→`{key:'network', msg:'网络异常'}`。

- [ ] **Step 2: 批次视图接入 `GET /api/batches`**

列表项渲染：批次号（mono）、状态中文标签（spec §2.1 表）、`project · model · 计划 N`。分组：进行中 / 已完结，`archived` 单独分组标记「已归档」。搜索框按批次号/项目/型号客户端过滤。请求失败时列表区行内提示（不弹窗）。

- [ ] **Step 3: 规则库视图接入 `GET /api/quality-rule-versions`**

渲染六列事实字段；`available:false` → 空态文案「规则版本数据暂不可用」。

- [ ] **Step 4: 浏览器验收**

1. 列表显示真实批次（无数据时为空态而非报错）
2. 停掉后端再刷新 → 行内网络错误提示，无原生弹窗、无控制台未捕获异常
3. `detail` 对象/字符串两种错误形态均能显示中文提示（用 404 的不存在批次 URL 手动验证）

- [ ] **Step 5: Commit** `git commit -m "feat(workbench): API 层与批次/规则库真实数据"`

---

### Task 3: 批次详情、轨迹时间线、新建批次

**Files:**
- Modify: `app/static/wb/main.js`
- Modify: `app/static/wb/api.js`（新增 `createBatch`）

**Interfaces:**
- Consumes: T1 的 `WB.openModal`、T2 的 `WB.api`
- Produces: `WB.batchHead(batch, list Item)`（批次头渲染；项目/型号取列表数据关联，spec §3.2）；`WB.ruleName(ruleVersionId)`（由规则版本列表反查，失败→「规则版本信息暂不可用」）；`WB.renderTimeline(traces)`（右栏）

- [ ] **Step 1: 新建批次表单**

字段与校验按 spec §4.1（规则版本下拉仅列已发布；`available:false` 时禁用提交并提示）。成功后 `listBatches()` 回读一次再 `setBatch(新id)`。

- [ ] **Step 2: 批次头 + 右栏**

批次头：批次号、状态中文标签、项目、型号、计划数量、规则版本名称。右栏轨迹：`skill_name` 中文名映射（入场/判定/抽样/异常处置/复检/放行/报告）+ `started_at` + `validation_result` 着色（通过/失败/部分）。关键指标仅用真实字段：判定通过/失败计数（遍历 `inspections[].decision_status`）、未关闭异常数、样本结论。

- [ ] **Step 3: 浏览器验收**

1. 新建批次（规则版本下拉来自真实数据）→ 创建后自动选中，批次头完整
2. 选中已有批次 → 右栏轨迹逐条渲染，时间倒序
3. 刷新恢复选中批次

- [ ] **Step 4: Commit** `git commit -m "feat(workbench): 批次详情/轨迹/新建批次"`

---

### Task 4: 数据入场节点

**Files:**
- Create: `app/static/wb/stages/intake.js`
- Modify: `app/static/wb/api.js`（`submitIntake`、`confirmIntake`）
- Modify: `app/static/wb/main.js`（流水线渲染调度，`pending_intake` 状态路由到 intake 面板）

**Interfaces:**
- Consumes: T3 的批次头/选中态
- Produces: `WB.stages.intake.render(batch, listitem)` → 节点面板 DOM；`WB.stages.intake.stageIndex === 0`

- [ ] **Step 1: 提交送检数据表单（spec §4.2）**

数据来源下拉（IV_CSV/OCR_FORM/MANUAL 显示为：检测数据/表单识别/人工录入）、多行检测数据文本域（每行一条，行内含组件条码）、可选人工覆盖字段。提交后即时展示响应：整体置信度、抽取字段表、低置信组件清单。`status=confirmed` → 提示「已自动确认入场」并刷新批次（此时批次可能已是 `pending_release`/`isolated`，界面跟随新状态，不得停留在等待人工确认）。

- [ ] **Step 2: 确认/拒绝入场**

按钮调 `confirmIntake(taskId, {action:'confirm'|'reject', fields:null})`。拒绝后批次仍为待入场，面板展示各组件 `intake_status=已拒绝`，保留重新提交入口。

- [ ] **Step 3: 错误呈现**

`cross_batch_sn_conflict` → 行内提示「组件条码已存在于其他批次」；`auto_confirm_row_count_mismatch` →「提交数据行数与计划不符」。

- [ ] **Step 4: 浏览器验收（需 DB 与种子数据）**

1. 对 `pending_intake` 批次提交数据 → 展示置信度/低置信清单
2. 构造高置信数据 → 自动确认、状态跳转正确（Review Focus #4）
3. 重复提交已属其他批次的条码 → 中文冲突提示
4. 刷新页面 → 入场面板降级显示组件条码+入场状态（spec §1.3 边界）

- [ ] **Step 5: Commit** `git commit -m "feat(workbench): 数据入场节点"`

---

### Task 5: 判定节点（回看 + 重新判定）

**Files:**
- Create: `app/static/wb/stages/judge.js`
- Modify: `app/static/wb/api.js`（`runJudge`）

**Interfaces:**
- Consumes: `WB.api.getBatch` 的 `inspections[]`
- Produces: `WB.stages.judge.render(batch, listitem)`；`WB.stages.judge.stageIndex === 1`

- [ ] **Step 1: 判定回看面板**

数据源为批次详情：每组件 `decision_status` 中文（合格/不合格/待处理/规则缺失）+ `decision_evidence` 逐条规则结果（规则名/输入/公式/结论）；`sample_result` 单独一行（抽样结论）。整体结论按 `counts` 口径汇总。

- [ ] **Step 2: 重新判定按钮**

调 `POST /judge`，成功后刷新批次详情重渲染面板（不改状态，spec §4.4）；400 `unconfirmed_sns` → 展示未确认条码清单 + 「请先完成入场确认」。

- [ ] **Step 3: 浏览器验收**

1. 对已判定批次查看逐组件明细
2. 对含未确认组件的批次点重新判定 → 清单提示
3. 控制台无报错

- [ ] **Step 4: Commit** `git commit -m "feat(workbench): 判定节点回看与重算"`

---

### Task 6: 异常处置节点（隔离→归因→指派→整改措施）

**Files:**
- Create: `app/static/wb/stages/incident.js`
- Modify: `app/static/wb/api.js`（`createIncident/isolateIncident/attributeIncident/assignIncident/capaIncident/getIncident`）

**Interfaces:**
- Consumes: 左列异常视图的 `listIncidents`
- Produces: `WB.stages.incident.render(batch, listitem)`；`WB.stages.incident.stageIndex === 2`；`WB.incidentByBatch(batchId)`（批次详情外取本批异常，驱动面板）

- [ ] **Step 1: 异常卡片与顺序约束**

面板列出本批次全部异常（`batch_id` 匹配）。每卡按异常状态只渲染一个主操作（spec §2.2）：`new`→「隔离异常」；`isolated`→「AI 归因」+「指派责任人」；`assigned`→「填报整改措施」；`pending_recheck`→提示转复检。409 `incident_not_isolated`（含 hint）→「请先隔离该异常」。

- [ ] **Step 2: 创建异常表单（isolated 批次且无异常时）**

字段：触发原因下拉（spec §5 五项中文）、严重度（严重/较严重/轻微）、受影响组件条码（多选自本批 inspections）、证据引用（可空）。400 `sn_not_found`/`evidence_not_found` → 展示缺失清单。409 `batch_not_isolated` →「批次隔离后才能创建异常」。

- [ ] **Step 3: AI 归因（spec §2.2）**

触发 `attribute`（decision 交互前先以空决策获取假设？——实现为：先 POST `attribute` decision=defer 展示假设列表，或直接在弹窗中触发并渲染响应 `hypotheses`；采用后者）→ 弹窗展示假设列表（空时展示后端 `hint` 中文兜底「暂无可用假设，请人工填写根因」）→ 三选一按钮：采纳（必填根因 cause）/ 驳回 / 搁置，再以所选 decision 提交。

- [ ] **Step 4: 指派与整改措施表单**

指派：责任人*、处置期限*（日期时间）、临时措施（可空）。整改措施：确认根因*、纠正措施*、已验证复选（勾选时若 400 `capa_verified_requires_pass_reinspection` →「需先有通过的复检记录才能勾选已验证」并回退复选框）。

- [ ] **Step 5: 浏览器验收**

1. `new` 状态卡只有「隔离异常」；隔离后出现归因与指派
2. 归因弹窗三选一闭环；采纳后根因回填卡片
3. 指派后批次转「处置中」；整改措施提交后转「待复检」
4. 左列异常视图点击「定位批次」→ 切换并选中

- [ ] **Step 6: Commit** `git commit -m "feat(workbench): 异常处置全链路"`

---

### Task 7: 复检节点

**Files:**
- Create: `app/static/wb/stages/recheck.js`
- Modify: `app/static/wb/api.js`（`createReinspection/listReinspections`）

**Interfaces:**
- Produces: `WB.stages.recheck.render(batch, listitem)`；`WB.stages.recheck.stageIndex === 3`

- [ ] **Step 1: 复检表单（字段全量，spec §4.5）**

关联组件（下拉选本批 inspections 的 sn）、实测值七项（pmax/temp/isc/voc/vm/im/ff，数值必填）、结论（合格/不合格）、执行人*、执行时间*（默认当前）、范围档位（最小/扩大/整批；后两者出现 `scope_sns` 多行输入）。

- [ ] **Step 2: 范围缺口与结果呈现**

400 `scope_gap` → 表单内展示缺失条码清单；`scope_sns_required` →「请填写复检范围组件条码」。提交成功：复检记录列表渲染（条码/结论/范围/范围明细）。结论=不合格 → 提示「复检未通过，批次已转入已拒绝」并刷新（批次与异常双双 `rejected`）。

- [ ] **Step 3: 浏览器验收**

1. 最小范围直接提交成功
2. 扩大范围漏一个条码 → 清单提示，补齐后成功
3. 录入不合格结论 → 批次转已拒绝，界面跳到报告节点

- [ ] **Step 4: Commit** `git commit -m "feat(workbench): 复检节点"`

---

### Task 8: 放行与人工拒绝

**Files:**
- Create: `app/static/wb/stages/release.js`
- Modify: `app/static/wb/api.js`（`releaseBatch/rejectBatch`）

**Interfaces:**
- Produces: `WB.stages.release.render(batch, listitem)`；`WB.stages.release.stageIndex === 4`；`WB.assertionName(name)`（七项断言中文映射，spec §4.6 表）

- [ ] **Step 1: 单步批准放行（spec §4.6）**

弹窗填批准人* → `POST /release`。`passed=true` →「已批准放行」，刷新转报告节点；`passed=false` → 面板渲染七项断言逐项（中文名 + 通过/未通过 + 原因含后端指名的异常号/条码）+「重新检查」按钮。不得出现两步预检（后端原子端点）。

- [ ] **Step 2: 拒绝批次**

表单：拒绝理由*、拒绝人*。调 `POST /reject`。响应 `auto_archived=true && report_id` → 提示「异常处置报告已生成，批次已自动归档」；仅 `auto_archived=false` →「批次已转入已拒绝」。

- [ ] **Step 3: 浏览器验收**

1. 待放行批次批准 → 断言全过 → 转报告节点
2. 构造未关闭严重异常的批次批准 → 七项断言中「无未关闭严重异常」未通过 + 原因 + 重新检查可重试、状态不变
3. 拒绝 → 自动归档提示，批次出现在已归档组

- [ ] **Step 4: Commit** `git commit -m "feat(workbench): 放行断言与人工拒绝"`

---

### Task 9: 报告节点与归档终态

**Files:**
- Create: `app/static/wb/stages/report.js`
- Modify: `app/static/wb/api.js`（`createReport/listReports/getReport`）
- Modify: `app/static/wb/main.js`（`released/rejected/archived` 路由 + archived 全只读）

**Interfaces:**
- Produces: `WB.stages.report.render(batch, listitem)`；`WB.stages.report.stageIndex === 5`；`WB.renderMarkdown(md)`（内置轻量 markdown 渲染：标题/列表/表格/粗体，转义 HTML）

- [ ] **Step 1: 报告生成与查看（spec §4.8）**

生成表单选类型（放行报告/异常处置报告；已拒绝批次只有后者可选，选放行报告被禁用而非提交后报错）。201→「报告已生成」；200→「已返回既有版本」。查看：抽屉渲染 `content_md`（markdown）+ 元信息（类型/版本号/生成时间）。生成成功后刷新——批次已自动归档，面板转只读。

- [ ] **Step 2: 归档终态**

`archived` 状态：流水线全部只读，无操作按钮（Review Focus #5）。任何残留写操作请求返回 409 `batch archived, terminal state` →「批次已归档，内容只读」。

- [ ] **Step 3: 左列「报告」视图接入真实数据**

`GET /api/batches/{id}/reports` 渲染报告列表（报告号/批次/类型/版本号/生成时间），点击行打开 T1 的抽屉复用 `renderMarkdown`。选中批次时列该批次报告；未选中时列全部在制批次（遍历 `listBatches()` 中 `lane<5` 的批次逐个请求，规模小可接受）。

- [ ] **Step 4: 浏览器验收**

1. 已放行批次生成放行报告 → 提示后批次转已归档、界面只读
2. 重复生成 →「已返回既有版本」
3. 对已拒绝批次生成异常处置报告成功；放行报告选项被禁用
4. 报告抽屉 markdown 正常渲染、无 XSS（输入含 `<script>` 的字段后查看）

- [ ] **Step 5: Commit** `git commit -m "feat(workbench): 报告节点与归档终态"`

---

### Task 10: 收尾守护与总验收

**Files:**
- Modify: `tests/static/test_workbench_static.py`
- Modify: `app/static/workbench.html`（首页入口：`/static/index.html` 顶部加跳转链接，旧页暂不删，spec §6.2）
- Modify: `dev-notes/<你的ID>.md`（全程留痕核对）

**Interfaces:**
- Consumes: 全部前序任务

- [ ] **Step 1: 扩展守护测试（先写失败再实现映射表补齐）**

```python
def test_status_labels_cover_all_enums():
    from app.infra.models import BATCH_STATUS, INCIDENT_STATUS
    js = (ROOT / "wb" / "main.js").read_text(encoding="utf-8")
    for st in BATCH_STATUS:  # 批次 10 态
        assert f"'{st.value}':" in js or f'"{st.value}":' in js, st.value
    inc = (ROOT / "wb" / "stages" / "incident.js").read_text(encoding="utf-8")
    for st in INCIDENT_STATUS:  # 异常 7 态
        assert f"'{st.value}':" in inc or f'"{st.value}":' in inc, st.value
```

（Review Focus #2 落地：枚举与 JS 中文映射对账。）

- [ ] **Step 2: 全量静态扫描**

`WB_FILES` 扩为全部 `wb/**/*.js` + `workbench.html`；追加断言：不含 `动线`、`TODO`、内部字段名直接渲染（抽查 `actual_quantity` 不出现在展示文案函数中）。

- [ ] **Step 3: 总验收清单逐项执行（spec §8）**

1. 新建→入场→判定→异常→复检→放行→报告→归档全流程无跳页
2. 逐状态核对「只有合法操作」
3. 界面无英文枚举/API 路径/说明性文字
4. 刷新恢复上下文；控制台无报错
5. `git diff -- app ':!app/static'` 为空（后端零改动）

- [ ] **Step 4: dev-notes 留痕 + Commit** `git commit -m "chore(workbench): 守护测试与总验收"`

---

## 任务依赖与并行性

```
T1 → T2 → T3 → {T4, T5 可并行} → T6 → T7 → T8 → T9 → T10
```
T4/T5 只依赖 T3 的接口，可并行；T6–T9 串行（状态推进环环相扣，面板互为前置场景）；T2 的 `WB.api` 是 T3–T9 的共同依赖，接口变更须先同步。
