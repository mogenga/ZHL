# 质慧链 · 光伏检验数字化平台

交付一批光伏组件之前，系统回答一个问题：**这批能不能放行？如果不能，哪些组件受影响、为什么、怎么复检、什么时候能恢复放行？**

## 快速开始

前置：uv（Python 环境）、Docker（PostgreSQL 15）。凭据/连接串在 `.env`（从 `.env.example` 复制，不提交）。

```bash
# 0. 同步 Python 环境（.venv + 按 uv.lock 装依赖）
uv sync

# 1. 启动 PostgreSQL 15（Docker）
#    方式一（有 compose，CI/Linux/Mac）：
#      docker compose -f compose.yaml up -d && 首启自动建表
#    方式二（本机无 compose 插件，PowerShell 脚本）：
.\scripts\start-pg.ps1          # 拉镜像 + 启容器 + 跑 001_init.sql + verify_001.sql
#    或手劭等效：
#      docker run -d --name zhl-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=zhl -p 15432:5432 postgres:15
#      docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl -v ON_ERROR_STOP=1 -f migrations/001_init.sql

# 2. 运行应用
uv run uvicorn app.main:app --reload --port 8000

# 3. 运行全部测试（domain 纯函数不依赖 DB，可直接跑）
uv run pytest

# 4. 端到端验收（四组样例）
uv run pytest tests/e2e/ -v
```

访问 `http://localhost:8000` 查看内置页面。

## 目录结构

```
app/
├── api/          # FastAPI 路由
├── workflow/     # LangGraph 图与节点
├── skills/       # Skill（编排语义）
├── plugins/      # Plugin（能力层）
├── domain/       # 纯函数（判定/断言/状态机）
├── infra/        # SQLAlchemy 模型 + 仓库 + Checkpointer
├── governance/   # 证据/轨迹/报告
└── static/       # 内置页面
tests/
├── domain/
├── plugins/
├── workflow/
└── e2e/
docs/             # 功能需求 + 架构设计 + spec
dev-notes/        # 开发过程留痕（规则见 AGENT.md §1）
pyproject.toml    # 依赖 + ruff/black/pytest/uv 配置
uv.lock           # 依赖锁（必须提交，勿手改）
compose.yaml      # PostgreSQL 15 Docker 服务（CI/Linux 用；本机用 scripts/start-pg.ps1）
scripts/          # start-pg.ps1（无 compose 插件时的容器初始化脚本）
.env              # 本地凭据（不提交；模板 .env.example）
```

## 文档索引

| 文档 | 说明 |
| --- | --- |
| `docs/质慧链-功能需求文档.md` | 业务逻辑与验收的唯一依据 |
| `docs/质慧链-架构设计.md` | 系统架构与分层设计 |
| `docs/superpowers/specs/2026-10-10-zhl-system-spec.md` | 可执行开发规格（含模块并行拆分） |
| `AGENT.md` | 共享开发规范（所有开发人员必读） |

## 开发规范

开始任何开发工作前，先完整阅读 `AGENT.md`。核心要求：

1. **过程留痕**：每个节点（brainstorm 定稿、计划评审、任务完成、code review、finish）在 `dev-notes/{你的ID}.md` 追加记录，不许收尾时一次性补记
2. **Context7 优先**：涉及 FastAPI / SQLAlchemy / LangGraph / Pydantic 等库的用法，先查 Context7 MCP 再动手
3. **完结交付**：功能演示命令 + 测试结果 + dev-notes 路径

## 技术栈

- Python 3.11+ · FastAPI · PostgreSQL 15（Docker 容器，compose.yaml / scripts）· SQLAlchemy 2.0 · Pydantic v2
- LangGraph（Agent 工作流编排 + 状态管理 + Human-in-the-loop）
- pytest · httpx
- uv（Python 环境与依赖管理）· Docker（PostgreSQL）
