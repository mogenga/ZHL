# 规则版本管理 · Task 2 dev-notes

- 新增 `app/domain/rule_version.py`（纯函数状态机，无任何 DB/infra import，符合 §6.1 冻结层"新文件、纯函数"规范）：
  - `RULE_LIFECYCLE: dict[str, frozenset]`（draft→published→disabled 终态）
  - `can_transition(from, to) -> bool`
  - `transition_rule_status(from, to) -> (new_status, error_reason)`
- 新增测试 `tests/domain/test_rule_version.py`（4 测试，TDD RED→GREEN 已验证）
- 全量门禁通过（pytest -x 全绿；ruff/black 已对触碰文件格式化）
