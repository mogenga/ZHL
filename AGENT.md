# AGENT.md · 质慧链开发规范（共享）

> 本文件约束所有开发 Agent。任何 Agent 在本仓库工作前必须先读完整文件并遵守。
> 规范与 `docs/superpowers/specs/2026-10-10-zhl-system-spec.md` 互补：spec 定「做什么」，AGENT.md 定「怎么做」。

---

## 1. 过程留痕（必须执行）

### 1.1 留痕位置

每位开发 Agent 在 `dev-notes/{开发人员ID}.md` 维护自己的过程记录。
开发人员 ID 命名：`{姓名或代号}-{YYYYMMDD}`，如 `mogen-20261010`。

### 1.2 何时记录

以下**每个节点**完成后必须追加一段，不许收尾时一次性补记：

| 节点 | 触发条件 | 记录内容 |
| --- | --- | --- |
| brainstorm 定稿 | spec 文档提交或评审通过 | 定稿时间 + spec 路径 + 关键决策 |
| 计划评审通过 | 开发计划（plan）被审阅批准 | 计划路径 + 评审结论 |
| 每个任务完成 | 单模块/单 API/单 Skill/单 Plugin 交付 | 任务名 + 产出物路径 + 自测结果 |
| code review 结论 | PR/分支合并前 | 评审人 + 通过/驳回 + 主要意见 |
| finish | 工作流（A 或 B）全部完成 | 交付物清单 + 测试结果 + 已知遗留 |

### 1.3 每段四要素

每段记录必须包含：

1. **关键原话**：触发本阶段的用户指令或自我决策
2. **关键产出**：spec/plan 路径、评审结论、代码模块路径
3. **拒绝与纠偏**：被用户或 review 驳回/修正了什么
4. **翻车与返工**：踩过的坑、重做的内容、原因

### 1.4 示例格式

```markdown
## 2026-10-10 14:30 · 任务完成 · Phase 0 判定引擎

- 关键原话：用户要求"判定引擎必须是纯函数，不读库不取时间不调模型"
- 关键产出：`app/domain/judge.py`，含 `judge()` 与 `assert_release()` 纯函数
- 拒绝与纠偏：初稿在 `judge()` 里读了 `datetime.now()` 用于日志，被 review 驳回，已移除
- 翻车与返工：浮点比较初版用 `==`，导致边界值 533.5 误判为 Fail；改为容差区间后通过
```

---

## 2. 依赖与库使用（Context7 优先）

### 2.1 强制规则

**涉及具体库、框架、API 的用法，一律先查 Context7 MCP 再动手，不凭记忆写。**

适用清单（不限于）：

- Python 标准库关键模块（`asyncio`, `contextlib`, `json` 等）
- FastAPI（路由、依赖注入、生命周期、异常处理）
- SQLAlchemy 2.0（Session、ORM 映射、事务、关系、迁移）
- Pydantic v2（模型、验证器、序列化、JSON Schema）
- LangGraph（StateGraph、interrupt/resume、Checkpointer、thread_id 语义、子图挂载、`get_state_history`、故障重放）
- LangChain（若有 Agent 编排接入）
- Langfuse / 观测平台（若引入追踪）
- PostgreSQL 特有语法（JSONB、EXCLUDE、行级锁、触发器）
- pytest / httpx（测试与异步 HTTP 客户端）
- uv（Python 环境管理，见 §10）

### 2.2 查完必须留痕

查 Context7 后，在 dev-notes 对应任务段落里注明：

```
- 参考：Context7 > FastAPI > "How to handle file uploads" (doc vX.Y)
```

### 2.3 禁止事项

- 禁止凭记忆写 API 签名（哪怕 90% 确定）
- 禁止使用已废弃的 API（Context7 会标注 deprecation）
- 禁止引入 spec 技术栈之外的新库（如需引入，先改 spec 再改代码）

---

## 3. 代码规范

### 3.1 语言与风格

- Python 3.11+，`black` 格式化，`ruff` lint（配置见 `pyproject.toml`）
- 类型注解全覆盖（`domain/` 纯函数必须 100%）
- docstring 用 Google 风格

### 3.2 分层依赖

```
接入层 (FastAPI) → 编排层 (LangGraph 图 + 节点) → 能力层 (Plugin) → domain (纯函数)
                                                         ↓
                                                    数据层 (SQLAlchemy + Postgres)
                                                         ↓
                                                    Checkpointer (LangGraph 状态持久化)
```

- **domain 层不依赖 infra 层，也不 import LangGraph**。判定/断言/状态机纯函数，不 import `sqlalchemy`、不 import `app.infra.*`、不 import `langgraph`
- LangGraph 节点只做编排与 IO，是 Skill/Plugin 的封装壳，不直接写业务判断
- 跨层调用通过 Protocol 接口，不直接依赖具体实现

### 3.3 测试要求

| 层 | 最低要求 | 覆盖率目标 |
| --- | --- | --- |
| domain | 每个纯函数有单测，含边界值、非法路径 | ≥ 90% |
| plugins | 每个 Plugin 有集成测试（DB mock 或 testcontainers） | ≥ 70% |
| skills | 用固定模型输出做回放测试 | ≥ 60% |
| 端到端 | 四组验收样例（spec §6.1）全通过 | — |

**domain 测试必须不依赖数据库与模型服务**，用 `pytest` 纯内存跑，是回归防线。

### 3.4 提交规范

- 一个提交一个逻辑变更，message 说清「为什么改」
- 提交前必须 `uv run ruff check . && uv run black --check . && uv run pytest -x` 通过（命令一律走 `uv run`，见 §10）
- 不提交 `data/evidence/`、`.env`、`dev-notes/` 中非当前 Agent 的文件
- `uv.lock` 随依赖变更一并提交；`.venv/` 不提交（已 gitignore）

---

## 4. 数据库规范

### 4.0 运行环境（Docker）

本项目的 PostgreSQL **一律用 Docker 容器运行**（`postgres:15`），不再依赖本机安装 PG 服务。凭据与连接串集中在 `.env`（不提交；模板 `.env.example`），与 `alembic.ini`、`compose.yaml` 保持一致：`postgres:postgres@localhost:15432/zhl`。

| 操作 | 命令 |
| --- | --- |
| 启动 + 初始化（有 compose：CI/Linux/Mac） | `docker compose -f compose.yaml up -d`（首启挂载 `migrations/` 自动执行 001_init.sql + verify_001.sql） |
| 启动 + 初始化（本机无 compose 插件，Windows） | `.\scripts\start-pg.ps1`（拉镜像 + 起容器 + docker cp 执行 DDL + 约束断言） |
| 停 / 清容器 | `docker stop zhl-pg` / `docker rm -f zhl-pg` |
| 应用连接 | `ZHL_DATABASE_URL`（.env）；SQLAlchemy / langgraph-checkpoint-postgres 同源 |

**规则**：
- DDL 验证（001_init.sql / verify_001.sql）在容器内跑：`docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl -f /tmp/xxx.sql`（先 `docker cp` 拷入；PowerShell 不支持 stdin 重定向 docker exec）
- 中文版 psql 默认 GBK，读 UTF-8 DDL 会报错——一律带 `PGCLIENTENCODING=UTF8`（已在 compose.yaml 环境变量与脚本里固化）
- 演示库固定 `zhl`，勿落到 `postgres` 保留库
- domain 纯函数单测不依赖 DB（SQLite 内存库自检）；只有 DDL 验证与端到端需要容器

### 4.1 迁移管理

- 使用 `alembic`（SQLAlchemy 2.0 配套），迁移文件按 `NNN_description.sql` 编号
- **Phase 0 的 DDL 必须可独立执行**，不依赖应用代码
- 每条迁移在 PR 描述里说明影响范围与回滚方案

### 4.2 约束优先原则

能用声明式约束（UNIQUE / FK / CHECK / EXCLUDE）表达的，不写触发器。
触发器仅用于：
- 规则版本不可变（发布后禁止 UPDATE/DELETE）
- 原始检验值不可改（列级权限回收）
- 证据只追加（撤销 UPDATE/DELETE）

### 4.3 事务边界

| 操作 | 事务范围 |
| --- | --- |
| 摄入一批组件 | 单事务写全部记录 + 证据，任一失败整批回滚 |
| 判定一个批次 | 单事务写全部组件判定 + 抽样结论 + 批次状态 |
| 状态转移 | 单事务校验前置条件 + 更新状态 + 写轨迹 |
| 放行 | 单事务执行断言 + 写放行证据 + 更新状态 |
| 报告生成 | 单事务写报告版本 + 快照引用 |

**模型调用在事务外完成**。拿到结构化结果后再开事务落库。

---

## 5. 交付规范（完结必须满足）

### 5.1 交付物清单

每个工作流（Phase A / Phase B / Phase 0）完结时，必须提供：

1. **功能演示命令**：一条可执行命令（如 `pytest tests/e2e/ -v` 或 `uvicorn app.main:app --reload`）
2. **测试结果**：`pytest` 全量输出（pass/fail/skip 计数 + 失败明细）
3. **dev-notes 路径**：本工作流的 `dev-notes/{ID}.md`
4. **已知遗留**：未解决问题列表 + 影响范围

### 5.2 演示要求

- 演示命令必须**从零可复现**（含 DB 初始化 + 种子数据）
- 不允许依赖手工操作
- 输出包含四组验收样例的结果（正常/边界/异常/一致性）

---

## 6. 共享契约（并行开发护栏）

### 6.1 Phase 0 锁定项（Phase A/B 只读）

Phase 0 完成后，以下文件/目录进入**冻结**状态，Phase A/B 不得修改：

- `app/domain/` — 判定引擎、状态机、断言
- `app/infra/models.py` — SQLAlchemy 模型
- `migrations/` — DDL
- `tests/fixtures/` — 验收样例 + 规则种子

Phase A/B 若发现契约需要调整，**必须走 PR + review**，不允许直接改。

### 6.2 接口契约

Phase A 与 Phase B 通过以下结构交换数据（Phase 0 定义，双方遵守）：

```python
# 判定输出
Decision(result, checks, rule_snapshot, batch_id, sn)

# 异常触发
IncidentTrigger(trigger_type, batch_id, affected_sns, evidence_ids)

# 放行断言
AssertionResult(passed, checks: list[AssertionCheck])

# 复检结果
ReinspectionResult(reinspection_id, incident_id, sn, passed, evidence_ids)
```

---

## 7. 工具与协作

### 7.1 Context7 MCP

- 查询库/框架文档的唯一入口
- 查询结果必须在 dev-notes 留痕
- 不凭记忆写代码

### 7.2 LangGraph 工作流

- 节点：LangGraph `add_node` 封装的 callable，接收 `BatchWorkflowState`，返回状态更新字典
- 人工 interrupt：仅三处（低置信字段确认、归因假设采纳、放行前最终批准）
- 确定性路径（断言失败 / SN 冲突 / 规则缺失）：直接路由，不 interrupt
- Checkpointer：PostgreSQL 后端（`langgraph-checkpoint-postgres`），`thread_id = batch_id`
- 故障续跑：节点失败时从最后成功检查点续跑，已成功节点不重放
- 轨迹留痕：`get_state_history` 即任务轨迹，满足架构 §7.2 可复演
- 不直接写库：节点通过 `TaskContext.evidence_writer` 写入证据，与 Plugin 契约一致

### 7.3 代码评审

- 每个 PR 至少 1 人 review
- review 必须检查：分层依赖方向、domain 纯度、测试覆盖、dev-notes 完整性
- 驳回后必须重新走 review 流程

---

## 8. 禁止事项（红线）

| 禁止 | 原因 |
| --- | --- |
| 在 domain 层 import infra 或模型服务 | 破坏纯函数，不可复算 |
| 模型直接改写批次状态 | 状态机必须确定性 |
| 修改已发布的规则版本 | 历史结论不可复算 |
| 证据被修改/删除 | 证据链断链 |
| 跳过 Phase 0 直接进 Phase A/B | 共享契约未锁定，并行冲突 |
| 收尾时一次性补 dev-notes | 过程留痕失去意义 |
| 凭记忆写库/API 用法 | 必须 Context7 先查 |

---

## 9. 工作流分工

| 工作流 | 内容 | 依赖 | 交付物 |
| --- | --- | --- | --- |
| Phase 0 · 共享契约 | DB schema + domain 纯函数 + 模型 + 证据库 + 测试夹具 | 无 | 可独立执行的 DDL + 全绿单测 |
| Phase A · 数据+判定 | intake API + 抽取 Skill + 判定 Skill + 抽样 Plugin + 批次详情 | Phase 0 | 批次推进到待判定/待放行 |
| Phase B · 异常+报告 | 异常 API + 归因 Skill + 复检 API + 放行 API + 报告 Skill | Phase 0（判定输出可 mock） | 批次推进到已放行/已拒绝 |
| 集成 | Phase A + B 全链路 + 四组验收样例 | Phase A + B | 端到端演示命令 + 全量测试 |

**并行边界**：Phase A 和 Phase B 同时开发，各自独立 PR。集成阶段串行。

---

## 10. uv 环境管理规范

本项目用 **uv**（Astral，Rust 实现）管理 Python 环境与依赖，替代裸 `pip`/`python -m venv`。所有 Python 命令一律走 `uv`，确保全团队环境一致、可复现。

### 10.1 环境生命周期

| 操作 | 命令 | 说明 |
| --- | --- | --- |
| 初始化 / 同步环境 | `uv sync` | 按 `uv.lock` 创建/更新 `.venv`，装主依赖 + dev group |
| 锁定依赖 | `uv lock` | `pyproject.toml` 变更后重新生成 `uv.lock` |
| 加依赖 | `uv add <pkg>`（生产）/ `uv add --dev <pkg>`（dev group） | 自动更新 `pyproject.toml` + `uv.lock` |
| 移除依赖 | `uv remove <pkg>` | 同上 |
| 跑任意命令 | `uv run <cmd>` | 在 `.venv` 内执行，自动校验 lock 与同步状态 |
| 查看依赖树 | `uv tree` | 排查版本冲突 |

**规则**：
- **新增/删除任何依赖必须走 `uv add` / `uv remove`**，直接编辑 `pyproject.toml` 后须紧跟 `uv lock` 验证可解析
- **不手动改 `.venv/`**（不用 `uv pip install` 或裸 `pip` 往项目环境装包）
- `.venv/` 已被 `.gitignore` 排除，**`uv.lock` 必须提交**（环境可复现的唯一保证）
- dev 依赖统一放 `[dependency-groups] dev`（PEP 735，`[tool.uv] default-groups = ["dev"]` 保证 `uv sync` 默认装入），不再用 `[project.optional-dependencies]`

### 10.2 命令对照（替换所有裸命令）

| 旧（禁止） | 新（规范） |
| --- | --- |
| `pytest` / `python -m pytest` | `uv run pytest` |
| `ruff check .` | `uv run ruff check .` |
| `black --check .` | `uv run black --check .` |
| `uvicorn app.main:app` | `uv run uvicorn app.main:app --reload` |
| `pip install X` | `uv add X`（进 pyproject+lock）；一次性工具用 `uvx <tool>` |

### 10.3 提交前门禁（AGENT.md §3.4 更新）

提交前必须全绿：

```bash
uv run ruff check . && uv run black --check . && uv run pytest -x
```

（`uv run` 会先校验 `uv.lock` 与 `.venv` 同步，不同步会自动同步或报错。）

