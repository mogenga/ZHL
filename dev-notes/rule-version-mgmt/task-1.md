# 规则版本管理 · Task 1 dev-notes

- 冻结层 `app/infra/models.py` 纯新增：`RULE_VERSION_ACTION` tuple + `RuleVersionEvent` 模型 + 2 个 `__all__` 条目，不动任何既有类/列。
- 新增迁移 `migrations/003_rule_version_events.sql`：`rule_version_action` 枚举 + `rule_version_events` 表 + 索引 + GRANT，对齐 001 风格（CREATE TYPE 先于 CREATE TABLE）。
- 新增测试 `tests/infra/test_rule_version_event_model.py`（SQLite roundtrip）。
- 待执行（需 shell 权限）：`uv run pytest tests/infra/test_rule_version_event_model.py -v`（GREEN）、迁移 003 对 Docker PG 执行、全量门禁、git commit。
