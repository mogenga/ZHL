# 规则版本管理功能 · 设计（创建/发布/停用 + 审计留痕）

> 2026-10-14 · 设计稿（brainstorming 产出，待人工批准后进入 writing-plans）
> 范围来自需求文档 §3.1 / §12 + 设计文档 §9.6（管理面属"下一阶段建设内容"）

## 0. 意图与边界

**意图**：补齐 `QualityRuleVersion` 的写路径与变更管控。现状只有只读 `GET /api/quality-rule-versions`（§9.6 明确管理面未建）；本设计落地"录入发布"与"变更管控"两条线。

**Goals**：
- 创建 draft 规则版本（`POST /api/quality-rule-versions`，幂等：`UNIQUE(project_id, model, version_no)` 冲突 → 409）
- 发布 draft → published（`POST /api/quality-rule-versions/{id}/publish`，operator 必填）
- 停用 published → disabled（`POST /api/quality-rule-versions/{id}/disable`，operator 必填）
- 全生命周期审计留痕（新建 `rule_version_events` 表 + API 查询端点）
- 停用后新批次/新判定不能再选用该版本（`create_batch` 规则解析加 status 校验）

**Non-goals**：
- 不做回滚（回到旧口径 = 再发新 `version_no`，与不可变设计一致）
- 不做 AI 推荐实现（§12 P1）；**留口**：`QualityRuleVersion` 不新增列，请求体预留 `source`（默认 `"manual"`）+ 可选 `recommendation_ref` JSONB 字段位，AI 推荐落地时填这两个字段，不动表结构
- 不做真认证/登录（现有 API 无认证层，`operator` 走请求体字符串，与 `rejected_by`/`approved_by` 同模式）

**Anti-goals（做错了什么才算失败）**：
- 修改已发布版本的字段/内容（违反 §8 红线"历史结论不可复算"）——DB 触发器 `fn_rule_version_guard` 已禁止，本设计不绕
- 停用破坏在途批次复算性——快照不可改，停用只影响"未来选用"，不删不改历史
- 把审计事件塞进 `agent_task_traces`（它带 `batch_id`、语义是任务轨迹，规则版本操作无批次上下文）

**约束（已就位的安全基础，本设计依赖不改）**：
- `fn_rule_version_guard`（`001_init.sql:53-82`）：published 仅允许 status 单列变更；published 不可 DELETE
- `UNIQUE(project_id, model, version_no)`（`001_init.sql:49`）：幂等导入
- `RULE_STATUS = ("draft","published","disabled")`（`models.py:69`）

## 1. 数据模型

### 1.1 新增表 `rule_version_events`（迁移 003）

```sql
CREATE TYPE rule_version_action AS ENUM ('created','published','disabled');
CREATE TABLE rule_version_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_version_id UUID NOT NULL REFERENCES quality_rule_versions(rule_version_id),
    action          rule_version_action NOT NULL,  -- 'created' / 'published' / 'disabled'
    operator        text NOT NULL,                  -- 请求体 operator 字符串
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_rule_events_rule ON rule_version_events(rule_version_id, created_at);
```

字段说明：
- `action` 三值与状态机对应：创建落 draft = `created`；draft→published = `published`；published→disabled = `disabled`
- `operator` 记"谁"（无认证，靠调用方填）；审计意义 = 操作人可追溯
- `detail` JSONB 预留扩展位（如 publish 时记 `version_no`/`effective_at`，disable 时记 reason）

**不新增 `QualityRuleVersion` 列**（幂等约束与 `effective_at` 已够）。

### 1.2 状态机（domain 层纯函数）

`app/domain/rule_version.py`（新增，纯函数、无 DB import）：

```python
RULE_LIFECYCLE = {
    "draft":     {"published"},
    "published": {"disabled"},
    "disabled":  set(),   # 终态：停用不可逆（要再启用=发新 version_no）
}
def can_transition(from_status: str, to_status: str) -> bool
def transition_status(from_status: str, to_status: str) -> str | None
    # 非法转移返回 None + 原因，API 层 409
```

- draft→published：允许
- published→disabled：允许
- 其他（draft→disabled、disabled→*、published→published 等）：非法
- 终态 disabled：不可逆（与"回滚=发新版本"决策一致）

## 2. API 端点（`app/api/rule_versions.py` 扩展）

现有 `GET /api/quality-rule-versions`（列表）保留，新增四个：

| 端点 | 方法 | 请求体 | 行为 | 错误 |
|---|---|---|---|---|
| `/api/quality-rule-versions` | `POST` | `CreateRuleVersionRequest` | 建 draft（幂等：同 project+model+version_no 已存在 → 200 + `duplicate:True` + 已存在 ID，不新建） | 422 校验 |
| `/api/quality-rule-versions/{id}/publish` | `POST` | `{operator, source?}` | draft→published，同事务写 `rule_version_events(published)` | 404 / 409 `cannot_publish_from_{status}` |
| `/api/quality-rule-versions/{id}/disable` | `POST` | `{operator, reason?}` | published→disabled，同事务写 `rule_version_events(disabled)` | 404 / 409 `cannot_disable_from_{status}` |
| `/api/quality-rule-versions/{id}/events` | `GET` | — | 返回该版本全生命周期事件（时间倒序） | 404 |

### 2.1 请求/响应模型（Pydantic v2）

```python
class CreateRuleVersionRequest(BaseModel):
    project_id: str
    model: str
    name: str
    nameplate_power: float          # > 0（DB CHECK 已有）
    power_tolerance: float          # 0..1（DB CHECK 已有）
    parameter_standards: dict       # JSONB
    current_bands: dict             # JSONB
    sample_plan: dict               # JSONB
    effective_at: datetime          # tz-aware
    version_no: int
    source: Literal["manual","recommended"] = "manual"   # AI 留口位
    recommendation_ref: dict | None = None               # AI 留口位（本次不填）

class CreateRuleVersionResponse(BaseModel):
    rule_version_id: uuid.UUID
    status: str
    version_no: int
    duplicate: bool                # True=幂等命中已存在（不新建，返回已存在 ID）
    event_id: uuid.UUID | None     # 新建时的事件 ID

class StatusChangeRequest(BaseModel):
    operator: str
    source: Literal["manual","recommended"] = "manual"
    reason: str | None = None      # disable 用

class StatusChangeResponse(BaseModel):
    rule_version_id: uuid.UUID
    status: str
    event_id: uuid.UUID

class RuleVersionEventResponse(BaseModel):
    event_id: uuid.UUID
    action: str
    operator: str
    detail: dict
    created_at: datetime
```

### 2.2 幂等语义（create）
- `UNIQUE(project_id, model, version_no)` 冲突：捕获 IntegrityError → 查已存在行 → 返回 200 + `duplicate: True` + 已存在 `rule_version_id`（不 500、不 409 硬拒）
- 幂等重复 create 不写新 event（首次才写 `created`）

### 2.3 事务边界（对齐 AGENT.md §4.3 风格）
- create：单事务写 `QualityRuleVersion`(draft) + `rule_version_events(created)`，任一失败整批回滚
- publish/disable：单事务改 status + 写 event；status 变更经 domain `transition_status` 校验（非法 409）；DB 触发器 `fn_rule_version_guard` 兜底（published 字段不可改）
- 读 events：只读

## 3. 停用对"选用"的影响（关键行为变更）

现状 `app/api/batches.py:79` `create_batch` 解析 `rule_version_id` 后只校验"存在"（`s.get` 返回 None → 400 `unknown rule version`），**不校验 status**。本设计加一处校验：

```python
rule = s.get(QualityRuleVersion, rule_uuid)
if rule is None:
    raise 404/400 unknown rule version   # 现状
if rule.status != "published":
    raise 400 {reason: "rule version not published (status={})", "status": rule.status}
```

- **错误码定 400**（对齐既有 `batches.py:83` unknown rule version 的 400 + `detail={"reason":...}` 风格；409 保留给批次状态冲突，不与规则选用混淆）

- 语义：draft/disabled 规则版本不能被新批次选用；只有 published 可用（需求 §3.1 "仅已发布规则可判定"）
- 在途批次不受影响（已落 `rule_snapshot`，复算不重读 status）
- 这是**唯一需要改既有文件（batches.py）的行为变更**，其余新增

## 4. 前端（最小，可选）
`index.html` 工作台已有"规则版本下拉"（发现用）。本设计**不改前端**（管理面可后续加"新建/发布/停用"按钮）；API 先行，前端留待需要时。需求 §402 仅要求 GET 查询，本次不超范围。

## 5. 测试（TDD，对齐现有 `tests/api` 模式）

`tests/api/test_rule_version_mgmt.py`（新建）：
- create：正常建 draft + event 落库；幂等重复 → `duplicate:True`；version_no 冲突
- publish：draft→published + event；非法（published→published、disabled→published）409
- disable：published→disabled + event；非法（draft→disabled、disabled→disabled）409
- events 列表：时间倒序、字段完整
- **停用选用边界**：建一个 disabled 规则 → `create_batch` 选它 → 409/400 拒绝；published 规则正常
- **AI 留口**：create 带 `source="recommended"` + `recommendation_ref` → 成功落库（仅验证字段位可用，不验证推荐逻辑）

domain 单测 `tests/domain/test_rule_version.py`：
- `can_transition` 全组合（draft→published / published→disabled / 其余 False）

门禁：`uv run ruff check . && uv run black --check . && uv run pytest -x`；迁移 003 可独立执行。

## 6. 交付物清单

| 文件 | 动作 |
|---|---|
| `migrations/003_rule_version_events.sql` | 新增（表 + enum + 索引 + 授权） |
| `app/infra/models.py` | 新增 `RuleVersionEvent` 模型 + `RULE_VERSION_ACTION` enum |
| `app/domain/rule_version.py` | 新增（纯函数状态机） |
| `app/api/rule_versions.py` | 扩展（4 端点 + 请求/响应模型） |
| `app/api/batches.py` | 改（create_batch 加 rule status 校验）|
| `tests/api/test_rule_version_mgmt.py` | 新增 |
| `tests/domain/test_rule_version.py` | 新增 |
| `app/api/main.py` | 不动（rule_versions router 已注册）|

> 冻结层风险说明（builder check 结论）：
> - `migrations/003_rule_version_events.sql`（新）：纯新增表 + 授权（对齐 `001_init.sql:320` GRANT 块给 `zhl_app`）
> - `app/infra/models.py`（加 `RuleVersionEvent` 模型 + `RULE_VERSION_ACTION` tuple）：**纯新增**，不动既有类/列，不破坏 Phase 0 契约 → 低风险，正常提交 + dev-notes 标注
> - `app/domain/rule_version.py`（新文件）：纯函数，无 DB import → 扩冻结层但零既有改动，正常提交 + 标注
> - `app/api/batches.py`（改既有）：**唯一真改既有文件**——只在 `create_batch` 规则解析处加 2 行 status 校验，不改其他逻辑；既有 `test_batches.py` 全量回归须保持绿
> 按 AGENT.md §6.1 冻结层规范，前三者提交时 dev-notes 标注"扩冻结层、纯新增/纯函数"；batches.py 改动走常规提交（API 层非冻结）。

## 7. 留口说明（AI 推荐，本次不实现）
- `source` 字段（`manual`/`recommended`）+ `recommendation_ref` JSONB：create/publish 请求体已预留
- §12 落地时：模型输出建议值 → 人工确认 → 以 `source="recommended"` + `recommendation_ref` 调 create/publish，复用同一套管理面端点，无需改表结构

## 8. 待人工确认项（我做的判断，可推翻）
1. **停用终态不可逆**（disabled 不能回 published；再启用=发新 version_no）——需求未明说，我按"不可变+无回滚"推的
2. **审计落新表 `rule_version_events`** 而非复用 `agent_task_traces`——我倾向新表（无批次上下文）
3. **停用边界**= 只挡新批次选用（400），不动在途快照
4. **operator 无认证**（请求体字符串）——与 `rejected_by` 一致
5. **create 幂等返回 200 + `duplicate:True` + 已存在 ID**（非 409）——幂等导入语义（`001_init.sql:49` UNIQUE 注释"幂等导入"）；重复调用不写新 event

以上 5 项若无异议，进入 writing-plans 拆任务。
