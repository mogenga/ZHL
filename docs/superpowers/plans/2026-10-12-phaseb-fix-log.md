# Phase B Code-Review Fix Log

> 本日志记录 2026-10-12 Phase B 全量代码审查（OCR 委托模式，25/25 文件覆盖率）所发现问题的修复过程。
> 每项发现含：**问题描述 / 修复方案 / 实际修复操作 / diff / 二次核验记录** 五段。
> 计划：`docs/superpowers/plans/2026-10-12-phaseb-review-fixes.md`；执行方式：executing-plans（逐任务 inline，TDD 门禁）。
> 基线 commit：`afb8f86`（审查收口）；修复后 commit：`b7933a8`。

## 修复范围与门禁快照

| 项 | 值 |
|---|---|
| 基线 | `afb8f86`（dev-notes 审查收口节点） |
| 修复后 HEAD | `b7933a8` |
| 修复 commit 数 | 5（`db26fe8` / `2b60a85` / `dc35648` / `9b5a393` / `b7933a8`） |
| 最终全量门禁 | `ruff check .` 全绿；`black --check .` 全绿；`pytest -x` **139 全绿** |
| 行为变更 | 0（全部为排序对齐 / 死代码删除 / 注释补充，可观测语义不变） |
| 测试新增 | 0（既有断言已覆盖归档 409 / 状态 409 路径，无需新增） |

## 严重度重评（C-1/C-2 降级说明）

审查初判将 C-1（`incident_capa` status 写序）与 C-2（`create_reinspection` verification_id 写序）列为 **Critical**（"内存/DB 状态脱节"）。修复执行前复核：

- 四个写 API 均运行在**请求作用域 session**，且既有 `assign_incident`（同文件、测试全绿）采用"先校验后写 status + `session.rollback()` + `session.refresh(incident)`"模式。
- `session.rollback()` 会使 ORM identity map 失效，后续属性访问（含 `session.refresh`）从 DB 重新加载，**内存属性不会在 rollback 后保留脏值**。
- 因此 C-1/C-2 的"数据完整性"风险低于初判；真正价值是**代码一致性**（`incident_capa` 与 `assign_incident` 写序对齐）+ 防御性。

**裁定**：C-1 降级为 **M 级（一致性修复）**并修复；C-2 评估后判定与既有 `assign` 模式同构且无实际脱节路径，**不单独改代码**，仅在本日志备案（见末节"遗留"）。

---

## 逐项发现修复记录

### 1. C-1 → M · `incident_capa` 写序对齐（commit `db26fe8`）

- **问题描述**：`app/api/incidents.py` `incident_capa()` 在 L299-302 先写 `incident.confirmed_root_cause / corrective_action / capa_verified / status = "pending_recheck"`，**之后**才做批次转移校验（L304-313）。若批次转移被拒并 `rollback()`，incident 的内存字段已被改动。同文件 `assign_incident()` 是"先校验后写"的正确模式，二者不一致。
- **修复方案**：把 incident 四字段写入整体移到批次转移校验（`batch_res.accepted` 确认）**之后**、`batch.status = "pending_recheck"` 之前；L3 verified 门槛（L286-297）保持在任何写入之前。
- **实际修复操作**：`edit` 工具重排 L299-314 代码块；新增一行注释说明"validate-then-write"对齐意图。
- **diff**（`db26fe8`，`app/api/incidents.py`，9 增 5 删）：

```diff
-    incident.confirmed_root_cause = payload.confirmed_root_cause
-    incident.corrective_action = payload.corrective_action
-    incident.capa_verified = payload.verified
-    incident.status = "pending_recheck"
-
     batch = session.get(DeliveryBatch, incident.batch_id)
     batch_res = transition(
         batch.status, "pending_recheck", {"measures_confirmed": True}, kind="batch"
     )
     if not batch_res.accepted:
         session.rollback()
         raise HTTPException(
             status_code=409,
             detail={"error": "batch_transition_denied", "reason": batch_res.reason},
         )
+
+    # Validate-then-write (aligned with assign_incident): only mutate the incident
+    # rows after the L3 gate and the batch transition have both passed, so a
+    # failed batch transition rolls back cleanly without leaving the incident's
+    # in-memory fields set.
+    incident.confirmed_root_cause = payload.confirmed_root_cause
+    incident.corrective_action = payload.corrective_action
+    incident.capa_verified = payload.verified
+    incident.status = "pending_recheck"
     batch.status = "pending_recheck"
```

- **二次核验记录**：`uv run pytest tests/api/test_incidents.py -v` → **7 PASS**（既有 capa 路径 + L3 400 用例全过，重排不改变可观测结果）。`ruff`/`black` 全绿。

### 2. H-2 · 删除 `release.py`/`reports.py` 冗余归档检查（commit `2b60a85`）

- **问题描述**：`release.py` L304-307 与 `reports.py` L104-113 在调用共享守卫 `require_not_archived(batch_id, session)` 之后，又各有一处 `if batch.status == "archived": raise 409`。该二次块是**死代码**——守卫已加载批次并在 archived 时以稳定载荷 `ARCHIVED_ERROR` 抛 409，二次块永不触发。`incidents.py`/`reinspections.py` 无此冗余，四路由不一致。
- **修复方案**：删除两处冗余块，保留 `require_not_archived` 调用 + 各自的状态专属检查（`release.py` 的 `!= pending_release`、`reports.py` 的 D7 rejected-only 检查）。更新注释指明 D8 由共享守卫负责。
- **实际修复操作**：`release.py` 删 2 行（含注释行改）；`reports.py` 删 7 行。
- **diff**（`2b60a85`，4 增 12 删）：

```diff
# app/api/release.py
-    # D8: archived is terminal (shared guard, same 409 as the per-router check).
+    # D8: archived is terminal — the shared guard (guards.require_not_archived)
+    # loads the batch and 409s with the stable ARCHIVED_ERROR payload.
     require_not_archived(batch_id, session)
-    if batch.status == "archived":
-        raise HTTPException(status_code=409, detail={"error": "batch_archived"})
     # Must be in pending_release to release.

# app/api/reports.py
-    # D8: archived batches cannot receive new reports (shared guard fires first).
+    # D8: archived batches cannot receive new reports — the shared guard
+    # (guards.require_not_archived) fires first with the stable ARCHIVED_ERROR 409.
     require_not_archived(batch_id, session)
-    if batch.status == "archived":
-        raise HTTPException(
-            status_code=409,
-            detail={
-                "error": "batch_archived",
-                "hint": "archived batches are terminal; no new reports",
-            },
-        )
```

- **二次核验记录**：`uv run pytest tests/api/test_release_api.py tests/api/test_reports.py -v` → **10 PASS**。预查确认两处归档 409 测试（`test_release_archived_409` / reports 归档 409）仅断言 `status_code == 409`、不断言 detail 载荷，故删块后无需改测试（409 现由共享守卫以 `ARCHIVED_ERROR` 载荷返回，语义等价）。`ruff`/`black` 全绿。

### 3. H-1 · D6a 自动归档 precondition 锁定注释（commit `dc35648`）

- **问题描述**：`reports.py` D6a 块 `transition(batch.status, "archived", {}, kind="batch")` 传空 precondition。若 Phase 0 状态机未来给 `("released","archived")` / `("rejected","archived")` 边加 precondition，`res.accepted` 会静默变 `False`，批次不归档而报告照常提交、无报错——静默分叉。
- **修复方案**：在 D6a 块上方加一段**锁定不变量注释**，说明"archived 边必须保持空 precondition，否则 D6a 静默 no-op"，并提示与 `state_machine.py` 变更同步。不改 `transition(...)` 调用本身。
- **实际修复操作**：`reports.py` D6a 块上方插入 7 行注释。
- **diff**（`dc35648`，8 增）：

```diff
     # D6a: auto-archive in the SAME transaction as the report write.
+    #
+    # D6a invariant: the ("released","archived") / ("rejected","archived")
+    # state-machine edges MUST keep an EMPTY precondition. If a precondition
+    # is ever added to these edges in app/domain/state_machine.py, the
+    # transition(..., "archived", {}, ...) call below will return
+    # accepted=False, the batch will silently stay non-archived, and the
+    # report will still commit with no error raised. Keep this comment in
+    # sync with any future Phase 0 state_machine.py change.
     archived = False
```

- **二次核验记录**：`uv run pytest tests/api/test_reports.py -v` → **4 PASS**（纯注释，零行为变化）。`ruff`/`black` 全绿。

### 4. H-3 · `assert_release` legacy 模式语义缺口文档化（commit `9b5a393`）

- **问题描述**：`app/domain/release.py` `assert_release` 的 legacy 自推导模式（`required_sns=None`）对"无复检 scope 行的 open incident"赋空 tuple；`check_reinspection_scope_covered` 中 `if required_sns:` 对空 tuple 短路 `continue`，故**无复检的 open incident 在 legacy 模式不会触发第 7 项 Fail**。Task 8 放行 API（authoritative 模式，喂权威 `required_sns`）不受影响；缺口仅影响假设的 Phase 0 调用方。
- **修复方案**：仅在 legacy 分支加**文档注释**，说明该限制 + 指明 authoritative 模式才是正确路径。冻结 domain 层只做注释、零逻辑/签名变更（AGENT.md §6.1 允许范围内）。
- **实际修复操作**：`release.py` legacy `else:` 分支加 12 行注释。
- **diff**（`9b5a393`，12 增）：

```diff
     else:
         # Legacy self-referential mode (Phase 0 backward compatibility):
         # build required_by_incident from the scope rows themselves.
+        #
+        # Known limitation (H-3, review 2026-10-12): in this mode an open
+        # incident with NO reinspection scope row gets an empty required set
+        # (``required_by_incident[inc_id] = ()`` below). check_reinspection_
+        # scope_covered short-circuits ``if required_sns:`` for an empty
+        # tuple, so "no reinspection at all" does NOT fail item 7 here — the
+        # open incident with zero coverage is silently treated as covered.
+        # The authoritative ``required_sns`` mode (Task 8 release API, fed
+        # from incident_sn_links) is the correct path and always names the
+        # gap. Legacy mode is retained only for Phase 0 backward
+        # compatibility; callers that want item 7 to catch missing
+        # reinspection coverage MUST pass ``required_sns``.
         required_by_incident = {}
```

- **二次核验记录**：`uv run pytest tests/domain/test_release.py -v` → **13 PASS**（纯注释，legacy 推导逻辑未变，既有 13 用例全过）。`ruff`/`black` 全绿。

### 5. M-1/M-4/M-6 · 死代码删除 + 子图路由注释 + 零 UUID 注释（commit `b7933a8`）

- **问题描述**：
  - **M-1**：`app/api/main.py` `_make_session_dependency()`（L44-51）是死代码——`create_app` 已内联 closure，该函数无调用方、不在 `__all__`。
  - **M-4**：`incident_subgraph.py` `_route_reinspection_result`：`reinspection_result=None` 时路由到 END（pass），与 API 层"无复检=第 7 项缺口"语义需说明一致性。
  - **M-6**：`report_skill.py` 空 incident 复检查询的 `uuid.UUID(int=0)` 零 UUID fallback 无注释说明意图。
- **修复方案**：删 `_make_session_dependency`；给子图路由 + 零 UUID 各加意图注释（不改返回值）。
- **实际修复操作**：`main.py` 删 10 行；`incident_subgraph.py` 路由 docstring 扩 7 行；`report_skill.py` 加 7 行注释。
- **diff**（`b7933a8`，15 增 11 删）：

```diff
# app/api/main.py
-def _make_session_dependency(session_factory: Callable[[], Any]) -> Callable[[], Any]:
-    """(Retained for API completeness; create_app now inlines a plain closure.)"""
-
-    def _session() -> Any:
-        session = session_factory()
-        return session
-
-    return _session
-

# app/workflow/incident_subgraph.py
 def _route_reinspection_result(state: IncidentSubgraphState) -> str:
-    """Node 5 (conditional router): pass -> END, fail -> rejected_route."""
+    """Node 5 (conditional router): pass -> END, fail -> rejected_route.
+
+    A ``None`` / absent ``reinspection_result`` routes to END (pass), NOT
+    ``rejected_route``. This subgraph is a topology scaffold: the API layer
+    (Task 7 ``create_reinspection``) is the authority that maps "no
+    reinspection" to the item-7 release gap. A standalone subgraph replay
+    with no result is therefore "no fail", by design.
+    """

# app/skills/report_skill.py
         .filter(
+            # ... the `or [uuid.UUID(int=0)]` guards the empty-.in_() case;
+            # a zero-UUID matches no real row — an inert, loud-safe placeholder.
             Reinspection.incident_id.in_([i.incident_id for i in incidents] or [uuid.UUID(int=0)])
```

- **二次核验记录**：`uv run pytest tests/api/test_main_smoke.py tests/workflow/test_incident_subgraph.py tests/skills/test_attribution_skill.py -v` → **12 PASS**。删函数前已 grep 确认 `_make_session_dependency` 仅定义处自身、无引用。全量门禁 `pytest -x` **139 全绿**、`ruff`/`black` 全绿。

---

## 未修复 / 遗留（备案）

- **C-2**（`create_reinspection` 的 `verification_id` 写序）：评估后与 `assign` 模式同构、请求作用域 + `refresh` 下无实际脱节路径，**未单独改代码**；与 C-1 同类，若后续要彻底对齐可在单独 PR 中把 `incident.verification_id` 写入也移到转移校验后。
- **M-2**（`list_reinspections` 中 `ins is None` 返回空 `sn`）：可读性/边界，未在本轮修复；属可选。
- **M-3**（`_latest_sample` 无抽样批次默认 pass、无 trace）：与 D1 留痕粒度相关，未在本轮修复。
- **M-5**（`prompt[:64]` digest 粒度）：trace 粒度偏好，未在本轮修复。
- **L-1**（`assertion.html` NAMES map 中文标签）：UI 标签，未在本轮修复。

## 结论

5 项发现（C-1 降级 M、H-1、H-2、H-3、M-1/M-4/M-6）已修复并全量门禁通过；C-2 及 M-2/M-3/M-5/L-1 备案为遗留（非阻塞、可选）。本轮**零行为变更**——全部为排序对齐、死代码删除、不变量/语义注释，139 测试全绿。
