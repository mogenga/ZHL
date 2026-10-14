# 规则版本管理 · Task 5 dev-notes

- `app/api/batches.py` `create_batch` 加 2 行规则状态守卫：rule 存在但 `status != "published"`（draft/disabled）→ 400 `{"rule_version_id", "reason": "rule version not published", "status"}`。既知规则未知仍走 400 unknown（行为不变）。
- 新增测试 `tests/api/test_batches_rule_status.py`（4 用例）：draft 400 / disabled 400 / published 201 回归 / **在途批次复算不受停用影响**（judge 读 rule_snapshot 与 ModuleInspection 判定快照，不重读 rule.status）。
- 在途不受影响测试通过 `app.include_router(_rv.router)` 把 rule-versions router 挂到 Phase A app + `dependency_overrides[_rv.get_session]` 同 SQLite session，完整走 publish→judge→disable→re-judge 路径。
- 既有测试 `tests/workflow/test_auto_incident.py` 的 seed rule 缺 status 字段（默认 draft），POST /api/batches 现被新守卫拦截 → 补 `status="published"`（一行，语义=测试原本就意图用 published 规则）。
- 全量门禁通过（pytest -x 全绿；ruff/black 已对触碰文件格式化）。
