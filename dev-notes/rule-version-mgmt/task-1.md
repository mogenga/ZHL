# 规则版本管理 · Task 1 dev-notes

- 冻结层 `app/infra/models.py` 纯新增：`RULE_VERSION_ACTION` tuple + `RuleVersionEvent` 模型 + 2 个 `__all__` 条目，不动任何既有类/列。
- 新增迁移 `migrations/003_rule_version_events.sql`：`rule_version_action` 枚举 + `rule_version_events` 表 + 索引 + GRANT，对齐 001 风格（CREATE TYPE 先于 CREATE TABLE）。
- 新增测试 `tests/infra/test_rule_version_event_model.py`（SQLite roundtrip）。
- 既有表数校验更新：`tests/infra/test_models.py` 12→13、`tests/infra/test_models_002.py` 12→13（纯 schema-consistency 断言，非业务）。
- 迁移 003 已在 zhl-pg 执行（BEGIN/COMMIT 通过，表已建，空表）。
- TDD：RED（模型未加前 ImportError）→ GREEN（2 passed）→ 全量 `pytest -x` 100% 通过（338 passed，11 个既有 black/ruff 漂移与本次改动无关，stash 基线验证）。
- 提交：baa6698。
