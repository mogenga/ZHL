# 规则版本管理 · Task 4 dev-notes

- 扩 `app/api/rule_versions.py`：`StatusChangeRequest/Response` + `RuleVersionEventResponse` Pydantic 模型 + 三端点：
  - `POST /{id}/publish`（draft→published；非法转移 409 + detail{error,reason,from}）
  - `POST /{id}/disable`（published→disabled 终态；409 同上）
  - `GET /{id}/events`（按 created_at desc + event_id desc 倒序返回审计事件；rule 不存在 404）
- 复用 Task 2 `transition_rule_status` 纯函数做状态合法性判定；`_write_event`/`_load_rule_or_404` 辅助（Task 3 已引入，Task 4 复用）。
- 状态变更同步更新 `rule.status` + `updated_at` + 写审计事件，同一事务 commit。
- 新增测试 9 用例（publish 3 + disable 3 + events 2 + unknown-rule 404 ×2），TDD RED→GREEN（11 passed）。
- 全量门禁通过。
