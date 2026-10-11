# dev-notes/zhl-agent

> 此文件为共享过程留痕，按开发者分段追加。规则见 `AGENT.md`。

## 2026-10-10 00:00 · zhl-agent · brainstorm 定稿

- **我发的关键原话**：
  - "调用 superpowers skill，依旧流程，先走 brainstorming，编写 spec 文档"
  - "参照功能需求文档和架构设计文档，进一步细化，如有不合理的地方可进行修正调整"
  - "完成 spec 文档的编写，确认系统需求、架构设计"
  - "进行模块拆分，分出并行的两部分工作，同时开发"
  - "生成 AGENT.md 文档，开发人员共享，限定开发规范"
  - 针对模块切法追问："按业务价值切"
- **关键产出**：
  - 本文件（dev-notes 落地）
  - `docs/superpowers/specs/2026-10-10-zhl-system-spec.md`（撰写中）
  - 模块拆分：A = 数据入场 + 规则判定；B = 异常闭环 + 报告归档；共享层 = 数据库 schema、状态机、断言、证据库（先行）
- **我拒绝或纠偏了什么**：
  - 用户拒绝技术层切法（FastAPI 侧 vs Postgres 侧），坚持业务价值切法
- **翻车与返工**：
  - 无。brainstorm 阶段一次过。

---

## 2026-10-10 00:15 · zhl-agent · spec 初稿完成

- **关键原话**：用户确认"按业务价值切"
- **关键产出**：`docs/superpowers/specs/2026-10-10-zhl-system-spec.md`（初稿 1-8 章）
- **拒绝与纠偏**：用户拒绝技术层切法
- **翻车与返工**：subagent_fork 超时，改用 subagent_spawn 后台完成 builder-check

---

## 2026-10-10 00:20 · zhl-agent · builder-check 完成 + spec 补充 + AGENT.md

- **关键原话**：builder-check 返回 10+ 个 open questions
- **关键产出**：
  - spec §9 补充 10 项实现细节（模型服务、AGH 契约、规则版本管理、ISO 抽样表、证据存储、批次部分判定、人工确认 API、认证、静态页面范围、报告格式）
  - `AGENT.md`（9 章共享开发规范）
- **拒绝与纠偏**：无
- **翻车与返工**：subagent_fork 120s 超时，换 spawn 成功

## 2026-10-10 00:30 · zhl-agent · 用户追加修改

- **关键原话**：用户要求"增加 Agent 开发技术栈：LangGraph 负责 Agent Workflow 编排、状态管理、节点执行和 Human-in-the-loop"
- **关键产出**：
  - spec §3.1 技术选型增加 LangGraph + LangGraph Checkpointer 两行
  - spec §3.2 依赖方向加入 LangGraph 图/节点 + Checkpointer 层级
  - spec §3.2.1 新增 LangGraph 工作流设计小节（main_chain_graph / incident_subgraph、interrupt 点、约束）
  - spec §3.3 目录结构新增 `app/workflow/` + `tests/workflow/`
  - spec §9.2 改名为「LangGraph 节点契约」，说明节点封装 + interrupt/resume + Checkpointer
  - AGENT.md §2.1 库清单加 LangGraph（StateGraph / interrupt / Checkpointer / thread_id / 子图 / get_state_history / 故障重放）
  - AGENT.md §3.2 分层依赖加 LangGraph 层级与「domain 不 import LangGraph」约束
  - AGENT.md §7.2 改名为「LangGraph 工作流」，列人工 interrupt 三处 + 确定性路由 + Checkpointer + 轨迹
- **拒绝与纠偏**：无。原 spec §9.2 的「AGH 简化语义」被 LangGraph 节点契约替代
- **翻车与返工**：edit 调用有 3 次 oldText not found（字符细节差异），重读后修正

---

## 2026-10-10 00:35 · zhl-agent · dev-notes 目录规范

- **关键原话**：用户要求"登记新的 dev-notes"
- **关键产出**：`dev-notes/README.md`（目录说明 + 命名规则 + 文件结构 + 禁止事项）
- **拒绝与纠偏**：无。人类开发者按 README 自建各自文件，与 zhl-agent.md 隔离
- **翻车与返工**：无

---

## 2026-10-11 11:30 · zhl-agent · 补齐 uv 环境管理规范

- **关键原话**：用户要求"补齐 uv 环境管理规范"，并指定先读 AGENT.md / README.md / pyproject.toml / 两份 spec + P0 plan 后再动手
- **关键产出**：
  - `pyproject.toml`：`[project.optional-dependencies]` 改为 PEP 735 `[dependency-groups] dev`，新增 `[tool.uv] default-groups=["dev"]`
  - 生成 `uv.lock`（67 包锁定），`uv sync` 建 `.venv`，`uv run ruff/pytest` 验证可用
  - `AGENT.md`：新增 §10「uv 环境管理规范」（生命周期/命令对照/提交门禁），§2.1 库清单加 uv，§3.4 提交门禁改 `uv run ruff/black/pytest`
  - `README.md`：快速开始加 `uv sync` 步骤，全部命令改 `uv run`，技术栈加 uv，目录结构补 `pyproject.toml` + `uv.lock`
  - 总 spec `2026-10-10-zhl-system-spec.md` §7 风险表：「AGH Skill/Plugin 接口不稳定」→「LangGraph 节点/Checkpointer 契约不稳定」（修 LangGraph 替换后残留文案）
  - P0 plan `2026-10-10-phase0-shared-contract.md`：Global Constraints + Task 1/3/6/7 的裸 `pytest/ruff/black` 命令统一改 `uv run`；Task 1 验证步骤补 `uv sync`
- **拒绝与纠偏**：无。pyproject 原用 pip 风格 optional-dependencies，改 uv 原生 dev group 属规范化修正
- **翻车与返工**：
  - edit 工具对 pyproject.toml/README.md 反复 oldText not found（多行块匹配失败），改用 write 重写整个 pyproject.toml + 逐段 read 定位后单独 edit
  - 首次 `uv lock` 在旧 pyproject（含 optional-dependencies）上已生成过一份 lock；改 pyproject 后重跑 `uv lock` 确认可解析
  - Context7 查 uv 两次（项目模式 + dev group/PEP 735），符合 AGENT.md §2「先查后写」

## 2026-10-11 11:45 · zhl-agent · P0 spec/plan 残留命令核对（首轮误报）

- **关键原话**：核对 P0 spec/plan 是否也有裸命令需补 uv
- **关键产出**：grep 命中 P0 plan 8 处 + P0 spec 3 处裸 `pytest/ruff/black` 命令，标为已改 `uv run`
- **拒绝与纠偏**：无
- **翻车与返工**：首轮 batch edit 因参数缺 ignoreCase 报错 schema 不符；重发时上下文超限被截断，实际 P0 plan/spec 的裸命令替换**并未落地**，本段"已改"为误报

## 2026-10-11 12:05 · zhl-agent · P0 spec/plan 残留命令核对（实际落地）

- **关键原话**：用户说"继续"，要求把上轮遗留的 P0 spec/plan 裸命令核对真正清掉
- **关键产出**：
  - P0 plan `2026-10-10-phase0-shared-contract.md`：Global Constraints 加「一律 `uv run` + uv.lock 必提交」条目；Task 1 验证步骤 `python -m pytest` → `uv sync` + `uv run pytest`；Task 2/3/6/7/8 全部 `Run:` 行与 Task 8 门禁、D18 演示行的裸 `pytest/ruff/black` → `uv run` 前缀
  - P0 spec `2026-10-10-phase0-shared-contract-design.md`：D18 演示命令 + §6 完成标准两处门禁/演示行 → `uv run pytest` / `uv run ruff` / `uv run black`
  - 全仓 `Select-String` 复查：可执行 `Run:`/门禁行已无裸 `pytest/ruff/black/python -m`，核查通过（剩余命中均为技术栈描述/交付物名词等说明性文字，非命令）
- **拒绝与纠偏**：无
- **翻车与返工**：CRLF + 中文文件的 edit 工具多行 oldText 反复匹配失败，改写成临时 Python 脚本（`_fix_uv_cmds.py`/`_fix_p0spec.py`）做可靠替换，跑完即删；PowerShell heredoc 在此 shell 不可用（解析报错），故走脚本文件

---

## 2026-10-11 12:20 · zhl-agent · P0 冻结文档 uv run 前缀保留决定（收口）

- **关键原话**：用户在我列出「P0 plan/spec 已改 uv run 前缀，可选回退项」后回复："选 1：保留。P0 plan + P0 spec 维持 uv run 前缀，不回退。"
- **关键产出**：无文件改动（现状即保留态）；uv 环境管理这条线收口：pyproject.toml（PEP 735 dev group + [tool.uv]）、uv.lock、AGENT.md §10/§3.4/§2.1、README、总 spec §7、P0 plan/spec 命令一致性全部落定
- **拒绝与纠偏**：用户拒绝回退 P0 冻结文档的命令前缀改动，明确保留（理由：与 AGENT.md §10 一致，开发 Agent 照抄无认知落差）
- **翻车与返工**：无。该段不记「按用户要求回退 P0 冻结文档」，因该事件未发生，记录即为噪声

---

## 2026-10-11 13:10 · zhl-agent · PostgreSQL 改用 Docker

- **关键原话**：用户要求"PGSQL 改用 docker"（把本地 PostgreSQL 18.6 服务改为 Docker 容器）
- **关键产出**：
  - `compose.yaml`：PostgreSQL 15 服务（postgres:15，库 zhl，postgres:postgres，映射 5432，挂载 migrations/ 作 initdb 自动建表，healthcheck）
  - `scripts/start-pg.ps1`：无 compose 插件（本机现状）时的容器初始化脚本——拉镜像 + 起容器 + pg_isready 等待 + docker cp 执行 001_init.sql + verify_001.sql，幂等
  - `.env` + `.env.example`：ZHL_DATABASE_URL / ZHL_DB / 凭据集中（.env 已 gitignore，模板可提交）
  - `README.md`：快速开始改 Docker 路径（compose 与 start-pg.ps1 双路），技术栈加 Docker
  - `AGENT.md` §4.0 新增「数据库运行环境（Docker）」：容器命令对照 + PGCLIENTENCODING=UTF8 固化 + 演示库固定 zhl + 纯函数单测不依赖 DB
  - 总 spec §3.1 DB 行：`PostgreSQL 15+` → `PostgreSQL 15（Docker 容器）`
  - 实测：Docker 引擎拉起后 `docker run` 起 zhl-pg，容器内跑 001_init.sql（exit 0）+ verify_001.sql（8 项约束断言全 OK）；`uv run ruff/black/pytest` 全绿
- **拒绝与纠偏**：无。DB 版本仍锁 15（compose 用 postgres:15），与 spec 原选一致，只是运行方式从本机服务改容器
- **翻车与返工**：
  - Docker 引擎管道初始未运行（dockerDesktopLinuxEngine 不存在）→ 启动 Docker Desktop 后 0s 恢复
  - PowerShell 不支持 `docker exec ... -f /dev/stdin < file`（重定向报错）→ 改 docker cp 拷进容器再 psql -f /tmp
  - `docker compose` 子命令本机未安装（仅 docker 29.7.2 核心）→ 用 `docker run` 等价全链路验证，compose.yaml 作为可移植产物保留供 CI/Linux 用

## 2026-10-11 13:10 · zhl-agent · P0 冻结文档遗留待确认

- **关键原话**：本次 DB 改 Docker 后，P0 plan/spec 里的演示命令 `psql $ZHL_DB -f migrations/001_init.sql`（原假设本地 PG 客户端）已不再适用
- **关键产出**：列为待用户决策项（是否同步改 P0 冻结文档的演示命令为 docker 形式），未擅改 P0 冻结文档
- **拒绝与纠偏**：沿用前轮「P0 冻结文档改动需用户确认」原则
- **翻车与返工**：无


---

## 2026-10-11 13:40 · zhl-agent · 依赖版本下界 + alembic.ini 去硬编码 URL

- **关键原话**：用户报「pyproject.toml 依赖无版本下界、alembic.ini 硬编码 sqlalchemy.url」，要求读文件定位、确认风险后修正
- **风险确认**：
  - pyproject.toml 7 个运行时 + 5 个 dev 依赖全部裸名（无 >=），spec §3.1 锁「SQLAlchemy 2.0 / Pydantic v2」但 pyproject 未落约束；app/infra/models.py 用 DeclarativeBase+mapped_column（SQLAlchemy 2.0 专属），re-lock / 无 lock 场景可解析到不兼容大版本
  - alembic.ini 第 4 行写死 postgresql+psycopg://postgres:postgres@localhost:5432/zhl，env.py 只读 ini 不读 .env（grep 证实 app/ 无一处读 ZHL_DATABASE_URL），明文凭据入库 + 与 .env/compose 双源漂移
- **关键产出**：
  - pyproject.toml：全部依赖加 >= 下界，锚定 spec 大版本（sqlalchemy[asyncio]>=2.0、pydantic>=2.0、psycopg[binary]>=3.0、langgraph-checkpoint-postgres>=3.1、fastapi>=0.143、alembic>=1.20、httpx>=0.28；dev：pytest>=9.1、pytest-cov>=7.1、ruff>=0.17、black>=26.10、aiosqlite>=0.22）
  - alembic.ini：sqlalchemy.url 置空占位，移除硬编码连接串/凭据
  - alembic/env.py：加 ZHL_DATABASE_URL 优先 + 手动解析项目根 .env 回落 + 空则清晰 RuntimeError（不引第三方 dotenv）；uv run black alembic/env.py 格式化收口
  - 实测：uv lock 重解析 67 包版本未推高（sqlalchemy 2.1.4 / pydantic 2.14.0 / fastapi 0.143.0）；全仓扫描除 .env/.env.example 外无 postgresql+psycopg://postgres... 入库；uv run alembic current 用 ZHL_DATABASE_URL 连上容器库；ruff/black/pytest 全绿
- **拒绝与纠偏**：无（用户明确要求修正，按风险确认→最小改动落地）
- **翻车与返工**：
  - 验证脚本 _verify_fixes.py 误报：extras 的 [ ] 干扰包名解析 + (.*?) 跨行捕获到 [loggers]，非真实问题，改目视确认
  - 本地 D:\PostgreSQL\18 服务占 5432 致容器端口绑定失败、alembic 连到旧库报 auth failed → 临时停本地 PG18 释放 5432 完成验证（后续轮次改 15432 彻底避开）
  - CRLF+中文的 alembic/env.py 用 edit 多行替换反复失败 → 走临时 Python 脚本 uv run python 整文件重写

## 2026-10-11 14:05 · zhl-agent · PG 容器改用 15432 端口避开本地服务冲突

- **关键原话**：用户要求「换个不常用端口，避免服务冲突」（本地 PG18 占 5432）
- **关键产出**：
  - 容器 host 端口 5432 → 15432（容器内仍 5432）：.env/.env.example（POSTGRES_PORT=15432、ZHL_DATABASE_URL localhost:15432）、compose.yaml（"15432:5432" + 注释）、scripts/start-pg.ps1（-p 15432:5432 + 文案）、README.md、AGENT.md §4.0 全部对齐
  - 全仓扫描确认除 15432 外无残留 :5432/5432:5432
  - 实测：重启 zhl-pg 绑定 15432:5432、pg_isready 就绪、001_init.sql exit 0、uv run alembic current（15432）连通容器库；本地 PG18（5432）无需再停，两者共存
- **拒绝与纠偏**：端口选 15432（避开 5433/5439 等常见邻近端口与防火墙保留段），本地 PG18 服务保持不动
- **翻车与返工**：compose.yaml 第 9 行注释残留 localhost:5432，首轮漏改，补改后复查通过

## 2026-10-11 15:00 · zhl-agent · Phase A brainstorm + spec 初稿

- **关键原话**：用户要求「针对Phase-A进行开发，调用superpowers skill，遵循流程，先运行brainstorming，编写该Phase的spec文档，待人工复核后进行下一流程」
- **关键产出**：`docs/superpowers/specs/2026-10-11-phase-a-intake-judge-design.md`（Phase A 设计文档，含 6 节点 LangGraph 图、API 路由、Plugin 实现、证据落库、测试规格、完成标准；用户拍板 R1–R6 六项决策：LangGraph 图做主线段 / thread_id=batch_id + PG Checkpointer / 节点确定性封装 / MockLLMClient 读固定 JSON 夹具无 LLM 环境变量 / 批次创建放 Phase A / Phase A 不做 3–5 段）
- **拒绝与纠偏**：无（brainstorm 阶段用户拍板，spec 按拍板项落稿）
- **翻车与返工**：subagent_fork 超时（120s），改 subagent_spawn 后台；spawn 后 child 迟迟 running 不返回，cancel 失败后改由 builder 自己读 spec 做 check，识别出 Q1（API ↔ 图触发模型）/ Q2（task_id 语义）/ Q3（多组件 confidence 聚合）三项需用户拍板
- **自测**：spec 文档 11 节结构完整；P0 冻结项（app/domain / infra/models / migrations / tests/fixtures）未被修改；subagent 已取消

## 2026-10-11 15:40 · zhl-agent · Phase A spec 人工复核通过 + 计划产出（writing-plans）

- **关键原话**：用户「确认」（Q1–Q3 + B1–B5 采纳建议默认）→ spec 更新后「通过」（人工复核通过）→ 进入 writing-plans
- **关键产出**：spec `docs/superpowers/specs/2026-10-11-phase-a-intake-judge-design.md` 定稿（Q1 API↔图触发模型 / Q2 task_id=thread_id=str(batch_id) / Q3 多组件 confidence=min 聚合整批一次确认，全部落入 §5.2/§5.4/§5.6/§9）；plan `docs/superpowers/plans/2026-10-11-phase-a-intake-judge.md`（9 个任务：依赖+脚手架 / 仓库层 / LLM 客户端 / 证据落库 / 抽取 Skill / 判定 Skill+抽样 Plugin / LangGraph 图+状态+Checkpointer+图回放 / API 路由+app.main / 验收演示收口；含 Global Constraints + Review Focus 5 项 + 任务依赖表；每任务完成后停止等待人工核验）
- **拒绝与纠偏**：无。Q1/Q2/Q3 均采纳建议默认并经用户「确认」；B1–B5 拍板项一并复核无异议
- **翻车与返工**：无

## 2026-10-11 15:50 · Phase A Task 1 完成 · 依赖 + 包骨架

- **关键原话**：「完成Phase-a的Task1，调用superpowers skill，遵循流程，开发完成后等待人工核验」「始终遵循AGENT.md的开发规范」「遵循~\docs\superpowers文件夹的spec文件和plan文件」
- **关键产出**：uv add langgraph（langgraph>=1.2.14 进 pyproject + uv.lock，langgraph-checkpoint-postgres>=3.1 P0 已有无需重复 add）+ 6 个 __init__.py 包骨架（pp/api/、pp/workflow/、pp/skills/ + 	ests/api/、	ests/workflow/、	ests/skills/）。提交 6e9670
- **拒绝与纠偏**：无。plan Task 1 四步全部按原样执行
- **翻车与返工**：uv 不在 PATH，定位到 $env:USERPROFILE\.local\bin\uv.exe，后续一律 & "C:\Users\21448\.local\bin\uv.exe" 调用；uv.lock diff 较大（149+/153-）已核对：新增 langgraph 及其依赖链（langchain-core / langgraph-sdk 等），fastapi/pydantic/sqlalchemy 版本未推高（0.143.0 / 2.14.0 / 2.1.4 与 P0 lock 一致）
- **自测**：uv run python -c "from langgraph.graph import StateGraph; from langgraph.checkpoint.memory import InMemorySaver; from langgraph.types import interrupt, Command; print('ok')" → ok；uv run pytest -q → 72/72 pass（P0 无回归）；uv run ruff check . 全绿；uv run black --check . 31 files unchanged

## 2026-10-11 16:09 · Phase A Task 2 完成 · 仓库层 app/infra/repositories.py

- **关键原话**：「完成Phase-a的Task2，调用superpowers skill，遵循流程，开发完成后等待人工核验」「始终遵循AGENT.md的开发规范」「遵循~\docs\superpowers文件夹的spec文件和plan文件」
- **关键产出**：pp/infra/repositories.py（BatchRepository / InspectionRepository（含 EXCLUDE 应用层兜底 CrossBatchSnConflict）/ SampleRepository（freeze_samples 确定性抽样 + save_conclusion + get_latest_conclusion）/ get_session() 连接串读 ZHL_DATABASE_URL 回落 .env，与 alembic/env.py 同法，无硬编码凭据）+ 	ests/infra/test_repositories.py（SQLite 内存库 + JH-550W 规则种子，6 用例：draft 批次 / 同批次 SN 唯一 / 跨批次同 SN 兜底（Review Focus #4）/ confirm 改值 / freeze 确定性重放 / conclusion 唯一键）。提交 832f215 + c0d989a
- **Ruling（偏离 plan 原签名，已补进 plan Task 2 章节并单提交 c0d989a）**：
  - T2-1：freeze_samples 补 iso_row: IsoRow 参数（plan 原签名只有 seed/selected_at，缺抽样量 n/ac/re，无法驱动 domain.freeze_sample）
  - T2-2：save_conclusion 补 sample_round 参数（唯一键 uq_conclusion_unique 含 sample_round，不传默认 1，与「复检多轮」语义冲突）
- **拒绝与纠偏**：无。plan Task 2 五步按原样执行，仅签名缺口按 Ruling 补齐
- **翻车与返工**：
  - freeze_samples 首版去重逻辑查 InspectionRepository.get_batch_sns（本批次已有 inspection 的 SN），导致第二次调用把刚插入的 Sample 又当成已存在、全部跳过、返回空列表 → 改为查 Sample 表自身已持久化行去重，测试断言也改为查 Sample 表总数（10 条，重放不重复）
  - 测试断言初版写成「两次调用的 created 列表一致」，但语义上第二次调用本就是 0 条新增（去重生效），改为查持久化总量验证确定性
- **自测**：tests/infra/test_repositories.py 6/6 pass；全仓 pytest 无回归；ruff check 全绿（--fix 后）；black 全绿；P0 冻结项（app/domain、infra/models、migrations、fixtures）零 diff
## 2026-10-11 16:58 · Phase A Task 3 完成 · MockLLMClient + LLMClient Protocol

- **关键原话**：完成Phase-a的Task3，遵循superpowers流程，开发完成后等待人工核验；始终遵循AGENT.md开发规范；遵循docs/superpowers的spec和plan文件
- **关键产出**：app/plugins/llm_client.py（LLMClient Protocol + MockLLMClient：eager加载 tests/fixtures/mock_llm_outputs.json；chat 按 B1 解析 [SOURCE_SN:<sn>] 标记命中 extraction 区块，无标记/无匹配抛 KeyError）+ tests/plugins/test_llm_client.py（6用例）。提交 e93754d
- **拒绝与纠偏**：无，plan Task 3 四步按原样执行
- **翻车与返工**：额外补2个自加用例（无标记也抛 KeyError；Protocol isinstance 检查）；ruff F541 修复4处无占位符f-string（测试prompt为纯字面量）
- **纯度验证**：grep requests/httpx/os.environ/ZHL_LLM 命中仅限 docstring，无网络/env 调用（R4）
- **自测**：test_llm_client.py 6/6 pass；全仓 pytest 84/84（P0 72 + Task2 6 + 本任务 6）无回归；ruff/black 全绿
## 2026-10-11 17:15 · Phase A Task 4 完成 · 证据 DB 写入

- 关键原话：完成Phase-a的Task4，遵循superpowers流程与AGENT.md开发规范，遵循docs/superpowers的spec和plan文件
- 关键产出：app/plugins/evidence_store.py save 补 DB 分支（db 为活 Session 时 check-then-insert evidence 行，append-only 幂等：make_evidence_id 确定性 PK 命中即跳过，不 UPDATE/DELETE；now 注入参数保持 generated_at 可测缺省 datetime.now(UTC)；content JSONB 列存 None，字节留磁盘以 content_hash+file_path 引用）+ tests/plugins/test_evidence_store_db.py 4用例。提交 d66c929
- 拒绝与纠偏：无，plan Task 4 四步按原样执行
- 翻车与返工：test 断言初版 .hex[:36] 多余语义，简化为 _to_uuid 直接比较；ruff 清 5 处（unused import / 引号注解 / E501）
- 自测：test_evidence_store_db.py 4/4；P0 test_evidence_store.py 4/4 无回归；全仓 pytest 88/88；ruff/black 全绿
## 2026-10-11 17:20 · Phase A Task 5 完成 · 抽取 Skill

- 关键原话：完成Phase-a的Task5，遵循superpowers流程与AGENT.md开发规范，遵循docs/superpowers的spec和plan文件
- 关键产出：app/skills/extract.py（extract_inspection_data：拼 prompt 嵌 [SOURCE_SN:sn] 标记（Task 3 B1）→ llm.chat（事务外，AGENT.md §4.3）→ evidence.save('extraction_result') → 返回 fields/confidence/appearance_findings，无业务判断）+ tests/skills/test_extract.py（4用例：confidence==0.81 / 确定性重放 / 证据写入 content_hash 一致性 / min 聚合输入侧）。提交 146e89b
- 拒绝与纠偏：无，plan Task 5 四步按原样执行
- 翻车与返工：test 文件 black 重排一次（多行调用换行）
- 自测：tests/skills/test_extract.py 4/4；全仓 pytest 无回归；ruff 全绿；覆盖率 app/skills/extract.py 100%、app/plugins 94%+、总 92.39% ≥ 90% 门禁

## 2026-10-11 17:40 · Phase A Task 6 完成 · 判定 Skill + 抽样 Plugin
- 关键原话：完成Phase-a的Task6，遵循superpowers流程与AGENT.md开发规范，遵循docs/superpowers的spec和plan文件
- 关键产出：app/skills/judge.py（judge_batch 单事务批次判定，A4 未确认SN守卫，B5 per-sn dict，decision_snapshot 证据）；app/plugins/sampling.py（run_sampling：iso_lookup 命中/缺项 gate（D9 无静默默认放行）、确定性冻结、幂等再冻结、抽样结论）；tests/skills/test_judge.py（5 用例）；tests/plugins/test_sampling_plugin.py（3 用例）
- 验证：uv run pytest tests/ → 100 passed；ruff + black 全绿；覆盖率 TOTAL 93.33%（judge 96% / sampling 95%）
- 提交：2f22371
- 翻车与返工：① 初版 test_judge 的测试 6 在全量套里失败——根因是测试 5 对模块级 _NORMAL 夹具做浅拷贝后原地改 pmax，污染了后续所有消费 _NORMAL 的测试（顺序依赖）；深拷贝修复。② SQLite in-memory 默认 pool 每连接一个独立库：commit 后新 session 看不到数据；单测引擎改 StaticPool 单连接。③ AQL 表 n=13 但可冻 SN 仅 10：冻结截断、计划参数保留表值 n=13，测试断言随之修正。④ 幂等再冻结返回 selected 稳定值（重算 domain 选择）而非空表
- 人工复核点：判 定/抽样的分层边界（Skill 不写状态机，Plugin 不写业务判断）

## 2026-10-11 19:05 · Phase A Task 7 完成 · LangGraph 主链图（六节点 + interrupt + 回放测试）
- 关键原话：完成Phase-a的Task7，遵循superpowers流程与AGENT.md开发规范，遵循docs/superpowers的spec和plan文件
- 关键产出：app/workflow/state.py（BatchWorkflowState TypedDict：batch_id/rule_version_id/batch_status/inspections/extraction/confidence/low_conf_sns/intake_status/decision/sample/transition_result/evidence_ids）；app/workflow/checkpointer.py（get_checkpointer(mode)：memory→InMemorySaver（R2 单测/回放），pg→PostgresSaver.from_conn_string(ZHL_DATABASE_URL) 上下文管理器（B4 调用方持有），缺 ZHL_DATABASE_URL 抛 LookupError，未知 mode 抛 ValueError）；app/workflow/main_chain.py（build_main_chain_graph：intake→extract→human_confirm[interrupt]→judge→sample→batch_transition 六节点；CONFIDENCE_THRESHOLD=0.8 边界自动确认；_aql 从 rule.sample_plan 读 P0 数据、_lot_size 从 batch.planned_quantity 读，deps 可选覆盖；batch_transition 调 domain transition() 推进 pending_judge→pending_release/isolated + 写 AgentTaskTrace 一行 + task_trace 证据；conditional 路由 human_confirm 后 confirmed→judge / rejected→END）；tests/workflow/test_main_chain.py（7 用例）；tests/workflow/test_checkpointer.py（3 用例）
- 验证：uv run pytest tests/ → 110 passed（原 100 + 10）；ruff + black 全绿；覆盖率 TOTAL 93.74%（main_chain 96% / state 100% / checkpointer 87%）
- 提交：见 git log（workflow 三文件 + 两个测试文件）
- 翻车与返工：① 首版 sample node 用 deps.get("lot_size", len(sns))，但 _HIT_COMBOS 仅 lot=10 命中 [9,15] 区间——3/5 SN 的测试 lot 落在 [2,8] 行被 _RULE_MISSING_TABLE 剥离后 iso_lookup 返回 None → rule_missing；修：graph 从 batch.planned_quantity 读 lot size（_lot_size 辅助），测试统一 _LOT=10。② 首版 _aql 读 deps.get("aql") 默认 1.0，但 rule.sample_plan 是 P0 冻结数据（AGENT.md §3.4），graph 应从 rule 读；修：_aql 辅助从 QualityRuleVersion.sample_plan 读，deps 仅作覆盖。③ batch_transition 从 state.get("batch_status") 读当前状态，首版 intake_node 未写 batch_status → 回退到 "pending_intake"（无 pending_intake→pending_release 转移）；修：intake_node 返回 batch_status="pending_judge"（intake 确认后批次进入待判定态）。④ 幂等：sample node 对已有 sample rows 的批次不重复写，靠 freeze_sample 确定性 + run_sampling 幂等分支。
- 人工复核点：① 六节点职责边界（节点是确定性包装器，业务判断在 Skill/Plugin/domain）② interrupt 只发生在 human_confirm（R3 单点）③ 回放语义（resume 后上游 intake/extract 不重放，LLM 调用次数不变）④ rule_missing → isolated（D9 无静默默认放行）⑤ PG checkpointer 在生产才启用，单测全用 InMemorySaver
