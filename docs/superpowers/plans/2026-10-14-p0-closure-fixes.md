# P0 缺口修复计划：闭环流转 + 异常自动创建

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通「隔离→整改→复检→关闭→放行」闭环（验收样例三可复现），并让 §8.1 的异常触发条件自动创建异常事件。

**Architecture:** 沿用"异常闭环由 API 分段驱动"的既定设计（设计文档 §9.1）：新增 `close` 端点承载人工最终签署；自动创建异常在 `batch_transition` 节点与 intake 冲突路径上同事务完成，复用现有 `QualityIncident` 模型与状态机，不引入新表。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + LangGraph（现状不动）；pytest（`tests/e2e/` 模式）。

**Spec:** `docs/质慧链-功能需求文档.md` §4.4/§5/§8.1；缺口依据 `docs/质慧链-设计文档.md` §9.8（2026-10-14 审计）

## Global Constraints

- 状态流转一律经 `app/domain/state_machine.py` 的 `transition()`，禁止直接赋值绕过
- 复检通过**不自动关闭异常**：关闭是质量工程师的最终签署动作（§4.4 闭环点）
- 新端点必须过 `require_not_archived` 守卫（409 终态）
- 异常创建必须写 `incident_sn_links`（不用自由文本），evidence_ids 必须真实存在
- 全部写操作同事务 + 轨迹留痕（`trace_store`）
- 每任务完成后 `dev-notes/` 留痕；测试用 `.venv\Scripts\python.exe -m pytest`（勿用系统 python）

## Review Focus

1. **close 的五条件必须逐条真实校验**：任何一条不满足返回 409 并指名缺失项，不得静默放行（测试：缺 CAPA 字段时 close 被拒）
2. **多异常批次**：仅当批次全部异常处于终态才允许批次转待放行（测试：两异常关一只，批次仍在待复检）
3. **自动创建的幂等**：同批次同触发原因的未关闭异常已存在时不重复建（测试：重跑判定不产生第二只异常）
4. **事务边界**：异常创建/关闭与批次状态、轨迹在同一事务，中途失败整体回滚（测试：构造 trace 写入失败后状态不变）
5. **跨批次 SN 冲突的异常挂在已占用批次**上，不创建孤儿异常

---

### Task 1: 复检闭环 —— capa 扩展 + close 端点

**Files:**
- Modify: `app/api/incidents.py`（capa 端点扩展 + 新增 close 端点）
- Modify: `app/api/main.py`（若路由集中注册则同步）
- Test: `tests/e2e/test_closure.py`（新建）

**Interfaces:**
- Produces: `POST /api/incidents/{id}/close`（无请求体）→ `200 {incident_id, incident_status:"closed", batch_id, batch_status}`；409 五条件缺失（`detail={error:"close_conditions_unmet", missing:[...]}`，逐条指名）或 409 `incident_not_closeable`
- Produces: `POST /api/incidents/{id}/capa` 行为扩展——`status=assigned` 时维持原转移（→pending_recheck）；`status=pending_recheck` 时仅更新 `verified/corrective_action/confirmed_root_cause` 字段（复检通过后补勾"已验证"），无状态转移
- 复用: `app/domain/state_machine.py` 的 `close_incident`（L91-116 五条件）与 `transition()`

- [ ] **Step 1: 写失败测试**

`tests/e2e/test_closure.py`：复用现有 e2e fixture 走完 建批→入场→判定→建异常→隔离→指派→capa→复检(pass) 后断言：① `POST capa` 可在 pending_recheck 置 verified=true；② `POST close` 成功且 incident=closed、batch=pending_release；③ 缺 CAPA 字段的批次 close 返回 409 且 `missing` 非空；④ 两只异常关一只后 batch 仍 pending_recheck（Review Focus #2）。

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/e2e/test_closure.py -v`
Expected: FAIL（404 close 路由不存在）

- [ ] **Step 3: 实现**

capa 扩展：`status==pending_recheck` 分支仅更新字段并写轨迹（复检通过才能 verified=true 的 L3 门禁沿用 `incidents.py:387-398`）。close 端点：守卫→状态校验（pending_recheck）→`close_incident` 五条件→incident `transition(closed)`→查批次全部异常终态则 batch `transition(pending_release, {recheck_passed:True, measures_effective:True})`→全程同事务 + trace。

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/e2e/test_closure.py -v` → PASS；再跑 `tests/e2e` 全量确认无回归

- [ ] **Step 5: Commit** `git commit -m "feat(incidents): close 端点打通复检后闭环流转"`

---

### Task 2: 异常事件自动创建（§8.1）

**Files:**
- Modify: `app/workflow/main_chain.py`（`batch_transition` 节点 isolated 分支，L279-306）
- Modify: `app/api/intake.py`（跨批次 SN 冲突路径，L133-142）
- Create: `app/services/auto_incident.py`（自动创建的共用函数，供两处调用）
- Test: `tests/e2e/test_auto_incident.py`（新建）

**Interfaces:**
- Produces: `app/services/auto_incident.py::ensure_incident(session, batch_id, trigger_type, severity, affected_sns, evidence_ids) -> QualityIncident | None`——幂等（同批次同 trigger_type 存在未关闭异常则返回既有），写 sn_links，入参 evidence 必须已存在
- 触发映射：判定 Fail→`rule_fail`（affected=fail_sns，证据=判定快照）；抽样 Fail→`sample_fail`（affected=全批 SN）；规则缺失→`rule_missing`（affected=全批 SN）；跨批次冲突→`data_consistency`（severity=critical，affected=[冲突 SN]，挂**已占用批次**）

- [ ] **Step 1: 写失败测试**

`tests/e2e/test_auto_incident.py`：① 判定含 Fail → 批次 isolated 且自动存在一只 `rule_fail` 异常（含 sn_links）；② 抽样 Fail → `sample_fail` 异常；③ 型号无已发布规则 → `rule_missing` 异常；④ 跨批次提交同 SN → 400 且被占用批次出现 `data_consistency` 异常；⑤ 同批次重跑判定不产生第二只未关闭异常（幂等，Review Focus #3）。

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/e2e/test_auto_incident.py -v` → FAIL

- [ ] **Step 3: 实现**

`ensure_incident`：先查重（batch+trigger+未关闭）→ 创建 `QualityIncident(status="new")` + `incident_sn_links`（逐 SN 校验归属本批）→ 关联证据（判定快照/轨迹 evidence_id）→ trace。`batch_transition` 在置 isolated 的同一事务内调用；`intake.py` 冲突分支在 400 前对占用批次调用（独立事务，冲突本身回滚不影响该异常落库）。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `.venv\Scripts\python.exe -m pytest tests/e2e -v` → 全 PASS（含 T1 的 closure 测试——注意自动创建的异常是 `new` 态，闭环测试需先 isolate，与 R3 一致）

- [ ] **Step 5: Commit** `git commit -m "feat(incidents): §8.1 异常事件自动创建"`

---

### Task 3: 工作台前端适配

**Files:**
- Modify: `app/static/wb/stages/reinspection.js`（复检通过后出现「确认关闭异常」操作，调 close）
- Modify: `app/static/wb/stages/incident.js`（capa 表单在待复检态支持补勾"已验证"；自动创建的异常自动出现在卡片列表）
- Modify: `app/static/wb/api.js`（`closeIncident`、错误映射补 `close_conditions_unmet`→展示缺失项清单）
- Modify: `app/static/workbench.html`（版本号 bump）

**Interfaces:**
- Consumes: T1 的 close 端点、T2 的自动异常

- [ ] **Step 1: 实现三处交互**（close 按钮、capa 补勾、错误映射；close 409 时在面板内逐条列出缺失条件）
- [ ] **Step 2: 浏览器验收**：跑通样例三全流程（建批→入场→判定失败→异常自动出现→隔离→归因→指派→整改→复检通过→补勾验证→关闭→放行→报告→归档），全程无跳页；`.venv\Scripts\python.exe -m pytest tests/static -v` PASS
- [ ] **Step 3: Commit** `git commit -m "feat(workbench): 闭环与自动异常的前端适配"`

---

### Task 4: 设计文档收口

- [ ] **Step 1:** `docs/质慧链-设计文档.md` §9.8 三项缺口标记为已修复（保留条目、注明修复日期与提交号）；§9.9 保留为待办
- [ ] **Step 2:** `dev-notes/` 留痕；`git commit -m "docs: §9.8 缺口修复收口"`

## 依赖与顺序

```
T1 ─┐
    ├─ T3 → T4
T2 ─┘
```
T1/T2 相互独立可并行（不同文件、不同测试文件）；T3 依赖两者；T4 收口。
