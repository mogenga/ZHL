# 规则版本管理 · Task 3 dev-notes

- 扩 `app/api/rule_versions.py`：`CreateRuleVersionRequest/Response` Pydantic 模型 + `POST /api/quality-rule-versions`（幂等：同 project+model+version_no 已存在 → 200 duplicate=True + 既存 ID + event_id=None，不写新 event；新建 → 201 + duplicate=False + 写 "created" 审计事件）。
- 竞态兜底：flush 触发 UNIQUE 约束（IntegrityError）→ rollback + 重查既存行 → 幂等返回。
- 状态码决策：FastAPI 装饰器 status_code 无法按分支动态，故 POST 端点签名加 `response: Response` 参数，按 `duplicate` 分支设 200/201（新建 201、幂等 200），对齐 spec §8 决策 5「200 + duplicate:True」。
- `operator` 字段：spec §2.1 CreateRuleVersionRequest 无 operator，create 事件 `event.operator` 用 `source`（manual/recommended）标记来源；publish/disable 才有独立 operator 字段（Task 4）。
- 新增测试 `tests/api/test_rule_version_mgmt.py`（2 用例，TDD RED→GREEN 已验证）
- 全量门禁通过（pytest -x 全绿；ruff/black 已对触碰文件格式化）
