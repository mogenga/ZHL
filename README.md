# 质慧链 · 光伏检验数字化平台

交付一批光伏组件之前，系统回答一个问题：**这批能不能放行？如果不能，哪些组件受影响、为什么、怎么复检、什么时候能恢复放行？**

一句话讲完主链：**Agent 把数据读进来，规则引擎算出合格不合格，Agent 找出为什么，规则引擎卡住放行，人签字，Agent 出报告。** AI 给建议，规则做裁决，人工担责任。

```text
① 数据入场 → ② 规则判定 → ③ AI 归因 → ④ 复检放行 → ⑤ 报告归档
                  │                        │
                  └── 失败 → 异常隔离 ←── 复检不过 ┘
```

## 快速开始

前置：uv（Python 环境）、Docker（PostgreSQL 15）。凭据/连接串在 `.env`（从 `.env.example` 复制，不提交）。

```powershell
# 0. 同步 Python 环境（.venv + 按 uv.lock 装依赖）
uv sync

# 1. 启动 PostgreSQL 15（Docker）
#    方式一（有 compose，CI/Linux/Mac）：
docker compose -f compose.yaml up -d
#    方式二（本机无 compose 插件，PowerShell 脚本）：
.\scripts\start-pg.ps1          # 拉镜像 + 启容器 + 跑 001_init.sql + verify_001.sql

# 2. 灌入一条已发布的演示规则（幂等，可重复执行；末尾打印 rule_version_id）
.\scripts\seed-demo.ps1

# 3. 运行应用
uv run uvicorn app.main:app --reload --port 8000

# 4. 运行全部测试（394 条：domain 纯函数不依赖 DB，可直接跑）
uv run pytest

# 5. 端到端验收（四组样例）
uv run pytest tests/e2e/ -v

# 测试红线：必须用项目 .venv 的 Python（含 langgraph 等依赖）。
# 系统 python / anaconda 直接 `python -m pytest` 会缺 langgraph 导致 collection ERROR。
#   Windows: .\.venv\Scripts\pytest.exe tests
#   (或装好 uv 后: uv run pytest)
```

访问 `http://localhost:8000` —— 根路径 302 到内置工作台 `workbench.html`。

**前端工作台**（原生 JS，零依赖，`app/static/wb/`）：左栏四视图（批次/规则库/异常/报告）+ 中栏六节点流水线 + 右栏 Agent 轨迹。五段主链全部可操作：建批 → 送检（原始单据 AI 抽取或逐列粘贴）→ 判定 → 归因三选一 → 复检/关闭 → 放行/拒绝 → 报告。界面上所有逐步操作与可粘贴数据见 `docs/test/01-full-chain-test-data.md`。

**JS 回归测试**（Node 原生，无需浏览器）：

```bash
node tests/static/test_wb_release_recheck.js   # 放行预检/结论重算（含 P0 哨兵）
node tests/static/test_wb_intake_wait.js       # 送检提交等待动画/超时改轮询/超时后合成结果
node tests/static/test_wb_incident_dup.js      # 异常处置面板
node tests/static/test_wb_api_norm.js          # API 错误归一化
node tests/static/test_wb_global.js            # 全局命名空间
```

## LLM 配置（真模型 / Mock 一键切换）

| 模式 | 配置 | 行为 |
| --- | --- | --- |
| **真模型**（默认） | `.env` 配 `ZHL_LLM_API_KEY` / `ZHL_LLM_BASE_URL` / `ZHL_LLM_MODEL` | 抽取与归因现场调模型（单次 40~90s，前端有提示与 120s 超时） |
| **Mock（确定性）** | 设 `ZHL_LLM_MODE=mock` | 走 fixture，置信度由 SN 哈希决定（`SN-LOW-*` → 0.62 演示低置信人工确认），毫秒级、可复现 |
| **自动降级** | 真模型网络失败时 | `ResilientLLMClient` 回落到 fixture，轨迹 `model_version` 如实记 `mock-degraded`，绝不冒认模型 |

LLM 调用点全项目只有 3 处（都在"需要判断力"的环节，其余全部是确定性代码）：原始单据解析（FR-1.1/1.2，模型提议 → `validate_rows` FR-1.4 校验器裁决）、逐组件字段抽取（FR-1.3）、AI 归因假设（FR-3.x，D2 证据闸门 + D3 置信度只排序不裁决）。判定公式、状态机、放行断言、报告渲染全部是纯函数，模型不越权。

## 核心机制速览

- **证据链**（append-only，hash-keyed）：原始文件、抽取 JSON、人工确认、判定快照、轨迹、报告快照六类证据全程留痕，报告内嵌 Agent 任务轨迹（FR-5.4）。
- **两处 graph 内 Human-in-the-loop**：低置信入场确认（`main_chain.human_confirm`，confidence < 0.8 挂起等人工）；归因三选一（`incident_subgraph.human_adopt`，跨请求 HITL —— `POST /attribute` 生成假设并停驻，人看完后另一次 `POST /adopt` 恢复）。采纳/驳回/修改三选一，采纳取后端存储的假设原文，`root_cause_source` 落轨迹可区分「照抄/改过/人工」。
- **放行是七项断言，不是人工感觉**：`assert_release` 逐条验（状态/抽样/严重异常/隔离 SN/证据/CAPA/复检覆盖），失败逐条指名原因、批次状态不动、可修后重试。签署走 `POST /release` 必须带真人 `approved_by`；面板预检走只读 `GET /release-preview`（同一装配，绝不写库）。
- **闭环自动回写**：复检通过自动置位「整改措施已验证」；关闭异常时若第 1 轮抽样 fail，自动补写第 2 轮结论（`sample_round=2`），抽样 fail 的批次不会永久卡死在断言②。历史批次可用 `POST /api/batches/{id}/recheck` 幂等补算。

## 目录结构

```
app/
├── api/          # FastAPI 路由（batches/intake/incidents/reinspections/release/reports/rule_versions）
├── workflow/     # LangGraph 图与节点（主链 6 节点 + 异常子图 5 节点）
├── skills/       # Skill（编排语义：extract/judge/report/release_inputs/remediation_conclusion…）
├── plugins/      # Plugin（能力层：llm_client/raw_source/attribution/evidence_store/…）
├── domain/       # 纯函数（判定/抽样/断言/状态机，不读库不取时不调模型）
├── infra/        # SQLAlchemy 模型 + 仓库
└── static/       # 内置工作台（workbench.html + wb/ 原生 JS）
tests/
├── domain/       # 纯函数单测（不依赖 DB/模型）
├── api/          # 路由集成测试（SQLite StaticPool + dependency_overrides）
├── e2e/          # 四组验收样例 + 演示路径
├── plugins/ skills/ workflow/ infra/
└── static/       # 前端 JS 回归（Node vm + DOM shim）
docs/
├── 质慧链-功能需求文档.md   # 业务逻辑与验收的唯一依据
├── 质慧链-架构设计.md
└── test/          # 两份测试文档（逐步操作 + 输入输出矩阵，全部实测核对）
dev-notes/        # 开发过程留痕（规则见 AGENT.md §1）
migrations/       # 001_init.sql（DDL + 种子，可独立执行）
scripts/          # start-pg.ps1 / seed-demo.ps1（容器初始化 + 演示种子）
pyproject.toml    # 依赖 + ruff/black/pytest/uv 配置
uv.lock           # 依赖锁（必须提交，勿手改）
compose.yaml      # PostgreSQL 15 Docker 服务（CI/Linux 用；本机用 scripts/start-pg.ps1）
.env              # 本地凭据（不提交；模板 .env.example）
```

## 文档索引

| 文档 | 说明 |
| --- | --- |
| `docs/质慧链-功能需求文档.md` | 业务逻辑与验收的唯一依据（五段主链 / 三个 AI 落点 / 状态机 / FR 编号） |
| `docs/质慧链-架构设计.md` | 系统架构与分层设计 |
| `docs/test/01-full-chain-test-data.md` | **全链路逐步验收脚本**（C1~C13，点哪里→填什么→应看到什么，逐字可粘贴） |
| `docs/test/02-targeted-test-data.md` | 针对性用例矩阵（正常/边界/失败 191 条，含判定闭区间、状态机边表、ISO 查表实测值） |
| `docs/superpowers/specs/2026-10-10-zhl-system-spec.md` | 可执行开发规格（含模块并行拆分） |
| `AGENT.md` | 共享开发规范（所有开发人员必读） |

## 开发规范

开始任何开发工作前，先完整阅读 `AGENT.md`。核心要求：

1. **过程留痕**：每个节点（brainstorm 定稿、计划评审、任务完成、code review、finish）在 `dev-notes/{你的ID}.md` 追加记录，不许收尾时一次性补记
2. **Context7 优先**：涉及 FastAPI / SQLAlchemy / LangGraph / Pydantic 等库的用法，先查 Context7 MCP 再动手
3. **提交前门禁**：`uv run ruff check . && uv run black --check . && uv run pytest` 全绿
4. **完结交付**：功能演示命令 + 测试结果 + dev-notes 路径

## 技术栈

- Python 3.11+ · FastAPI · PostgreSQL 15（Docker 容器）· SQLAlchemy 2.0 · Pydantic v2
- LangGraph（Agent 工作流编排 + 状态管理 + Human-in-the-loop，PostgreSQL checkpointer）
- 前端：原生 JS（零框架，`WB` 全局命名空间，Node vm DOM shim 可测）
- 测试：pytest · httpx · Node 原生（前端回归）
- 工具链：uv（环境与依赖）· ruff · black · Docker
