# 遗留事项修复计划：SN 组件级隔离 + 守卫加固

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复设计文档 §9.8 第 3 项（组件级 SN 隔离标记缺失，放行断言④恒真）与终审遗留的 3 个 deferred Minor（M-5 close 409 测试覆盖、M-7 close/复检竞态、M-8 capa 归档守卫）。

**Architecture:** SN 隔离标记沿用现有 `intake_status` 列（新增 `isolated` 取值的写入方与恢复方）：判定失败/异常创建时标记受影响 SN，异常关闭（人工签署）时恢复——放行断言④由「恒真」变为真实防线。竞态用行锁（`with_for_update`）串行化，无 DDL/新表。

**Tech Stack:** FastAPI + SQLAlchemy（现状）；pytest。

**Spec:** `docs/质慧链-设计文档.md` §9.8 第 3 项；`docs/质慧链-功能需求文档.md` §8.2 / §5.3 断言④

## Global Constraints

- 状态流转一律经 `transition()`；SN 标记是数据列更新而非状态机边
- 仅 `status="isolated"` 的批次允许标记 SN；已归档批次只读（D8）
- 恢复条件：该 SN 不再被任何未关闭异常关联，且当前值为 `isolated`
- 自动创建路径（§8.1）与人工创建端点共用标记逻辑；跨批次冲突的占用批次**不**标记（其批次非隔离态）
- 每任务完成后 `dev-notes/` 留痕

## Review Focus

1. **标记/恢复对称**：judge-fail 标记的 SN 必须在 close 后恢复为 confirmed，否则批次永远放不出去（测试钉住：close 后断言④真实通过 release）
2. **多异常重叠 SN**：同一 SN 被两只异常关联时，只关一只不得恢复（测试钉住）
3. **占用批次不误标**：跨批次冲突路径不得改动已放行批次的 SN 状态（测试钉住）
4. **隔离后重判语义**：A4 守卫对 isolated 行返回 400 是预期（批次已隔离，重判无意义）——工作台隔离态不提供重判入口，文档留痕

---

### Task 1: SN 组件级隔离标记与恢复

**Files:**
- Modify: `app/plugins/auto_incident.py`（ensure_incident 内：批次为 isolated 态时标记 affected SNs）
- Modify: `app/api/incidents.py`（人工创建端点标记 affected SNs；close 端点恢复）
- Test: `tests/workflow/test_auto_incident.py`、`tests/e2e/test_closure.py`（扩展）

**Interfaces:**
- Produces: `app/plugins/auto_incident.py::mark_sns_isolated(session, batch_id, sns) -> int`（仅 `intake_status=="confirmed"` 的行改 `isolated`，返回标记数；批次非隔离态时不标记）；`restore_sns_after_close(session, incident) -> int`（逐 link 判重叠后恢复，返回恢复数）
- Consumes: 现有 `IncidentSnLink` / `ModuleInspection` / `OPEN_INCIDENT_STATUSES`

- [ ] **Step 1: 写失败测试**

workflow 层：judge-fail 用例断言 `SN-G-00` 的 `intake_status=="isolated"`、其余 SN 仍 `confirmed`。e2e 层：test_closure 主路径 close 后断言 SN 恢复 `confirmed` 且 `POST /release` 返回 `passed=True`（断言④首次真实生效）。

- [ ] **Step 2: 运行确认失败**（Run: `D:\jsai\ZHL\.venv\Scripts\python.exe -m pytest tests/workflow/test_auto_incident.py tests/e2e/test_closure.py -q` → FAIL）

- [ ] **Step 3: 实现** mark/restore 两函数 + 三处接入（auto/人工创建标记、close 恢复）

- [ ] **Step 4: 运行确认通过 + 全量回归**（`pytest tests`）

- [ ] **Step 5: Commit** `feat(incidents): SN 组件级隔离标记与恢复（§9.8 #3）`

### Task 2: 守卫加固（M-7 / M-8）

**Files:**
- Modify: `app/api/incidents.py`（close 端点 incident 行锁；capa pending_recheck 分支挂 `require_not_archived`）
- Modify: `app/api/reinspections.py`（fail 转拒绝路径 incident 行锁）
- Test: `tests/e2e/test_closure.py`（复检 fail 后 close → 409 `incident_not_closeable`）

- [ ] **Step 1: 写失败测试**（复检 fail → 异常/批次 rejected；随后对同异常调 close → 409 incident_not_closeable——行锁保证读到最新态）
- [ ] **Step 2: 确认失败**（当前实现 409 文案一致但读的是旧态，存在竞态——测试先以顺序流钉住语义）
- [ ] **Step 3: 实现**（两处 incident 读取加 `with_for_update()`；capa 分支挂归档守卫）
- [ ] **Step 4: 全量回归**
- [ ] **Step 5: Commit** `fix(incidents): close/复检竞态行锁 + capa 归档守卫`

### Task 3: M-5 测试补盲 + 文档收口

**Files:**
- Modify: `tests/e2e/test_closure.py`（scaffold 用例：owner/capa_fields/reinspection_record/capa_verified/containment_action 多缺失组合）
- Modify: `docs/质慧链-设计文档.md` §9.8 第 3 项标记已修复
- Modify: `dev-notes/zhl-agent.md`

- [ ] **Step 1: 补 scaffold 测试**（直接写库制造多缺失行 → close 409 missing 含 5 项）
- [ ] **Step 2: 全量回归 + 文档收口**
- [ ] **Step 3: Commit** `test/docs: close 409 覆盖补盲 + §9.8 收口`

## 依赖与顺序

T1 → T2 → T3（T2 的行锁与 T1 的恢复点同在 close 端点，串行避免冲突）。
