# Phase B 合并回 main · Git 冲突情况说明

> 日期：2026-10-12 · 合并：`feature/phase-b` → `main` · 基线 merge-base：`438c067`
> 状态：**干跑预检发现 4 处冲突，合并已中止，未落盘**。本文档说明每处冲突文件、冲突内容、建议处理方案。
> 门禁基线：合并前 feature/phase-b 侧 `pytest` 139 全绿；main 侧（origin/main `f088e6d`）`pytest` 132 全绿（Phase A 关闭节点记录值）。

## 0. 背景：为何会产生冲突

`feature/phase-b` 从 `438c067`（Phase 0 冻结末端、`438c067` 的 `chore(db): run PostgreSQL 15 via Docker` 之前）切出，**只含 Phase 0 + Phase B 自身工作**。

`origin/main` 已推进 **26 个提交**完成 **Phase A**（`ad8d8a5` → `f088e6d`），其中：
- Phase A 创建了 `app/main.py`（完整 app 工厂：`create_app` + `AppDeps` + 挂 `batches`/`intake` 路由到 `main_chain_graph`）
- Phase A 创建了 `app/plugins/llm_client.py`（`MockLLMClient` 按 `[SOURCE_SN:<sn>]` 标记路由，服务于抽取 Skill）
- Phase A 在 `pyproject.toml` 里把依赖组结构化为 PEP 735 dev-group（含 `pytest`/`ruff`/`black` 等）

Phase B **独立**于 Phase A 也创建了 `app/main.py`（一行入口：`app = create_app_with_trace()`，挂 4 个 Phase B 路由 + 静态目录）和 `app/plugins/llm_client.py`（`MockLLMClient` 按 `incident_id` 正则路由 + `OpenAICompatClient`，服务于归因 Skill）。

→ 两阶段对**同名文件**各写了一套，git 无法自动调和，产生 4 处冲突。

**本地 main 状态**：合并前本地 `main` 落后 `origin/main` 26 提交，已 `git merge --ff-only origin/main` 快进至 `f088e6d`。真正合并基线是 `origin/main`（含 Phase A），而非过时的本地 main。

## 1. 冲突文件总览

| # | 文件 | 冲突类型 | main（Phase A）内容 | feature/phase-b（Phase B）内容 |
|---|---|---|---|---|
| 1 | `app/main.py` | add/add | app 工厂 `create_app`/`AppDeps` + batches/intake 路由 | 一行入口 `create_app_with_trace()` |
| 2 | `app/plugins/llm_client.py` | add/add | `MockLLMClient`（按 `[SOURCE_SN]` 路由，无网络） | `MockLLMClient`（按 `incident_id` 路由）+ `OpenAICompatClient` |
| 3 | `pyproject.toml` | content | PEP 735 dev-group（pytest/ruff/black… 移入 `[dependency-groups]`） | `[project]` 主依赖加 `python-dotenv`（`langgraph` 两侧已有） |
| 4 | `tests/plugins/test_llm_client.py` | add/add | Phase A 抽取/`MockLLMClient` 路由测试 | Phase B 归因/`OpenAICompatClient` 测试 |

另：`uv.lock` 自动合并（无冲突标记）但两侧锁条目不同，合并后**必须 `uv lock`/`uv sync` 重锁**。
`app/infra/models.py`、`app/domain/release.py`、`app/domain/types.py` 为**可自动合并**（两侧改动不重叠：Phase A 改 `_ts` utcnow→now(UTC)，Phase B 加 `scope_type`/`scope_sns`/`capa_verified` 列），合并后需跑全量测试验证。

## 2. 逐冲突说明 + 建议处理方案

### 冲突 1 · `app/main.py`（语义冲突，最高风险）

- **冲突内容**：
  - main 版：模块级 `create_app(checkpointer_mode, session_factory, llm, evidence, iso_table, inspect_repo)` + `AppDeps` 容器 + `app = create_app()`；import `app.api.batches`/`app.api.intake`/`app.workflow.checkpointer`/`app.plugins.evidence_store`/`app.plugins.llm_client`。
  - Phase B 版：`from dotenv import load_dotenv` + `app = create_app_with_trace()`（工厂在 `app/api/main.py`）；import `app.api.main.create_app_with_trace`。
  - **二者都以 `uvicorn app.main:app` 为入口、都叫 `app`，但工厂与路由集完全不同**。
- **为什么不能 `--ours`/`--theirs` 直接选边**：
  - 选 main 版（ours）→ 丢掉 Phase B 的 `create_app_with_trace` + 4 个 Phase B 路由 + 静态目录 + `.env` 加载，Phase B 功能在 main 上消失。
  - 选 Phase B 版（theirs）→ 丢掉 Phase A 的 `create_app`/`AppDeps` + batches/intake 路由 + `main_chain_graph` 装配，Phase A 功能在 main 上消失。
  - **两个版本都是各自阶段的有效入口，都不是对方的超集。**
- **建议处理方案（取并集，不取边）**：
  1. 以 Phase B 版的 `load_dotenv()` + 入口为骨架（保留 fail-fast `.env` 加载）。
  2. 把 Phase A 的 `create_app`/`AppDeps` 工厂**保留**（Phase A 测试 `tests/api/test_batches*.py`/`test_intake*.py`/e2e 都 import 它）。
  3. 将 Phase B 的 4 路由（incidents/reinspections/release/reports）+ trace 端点**挂载进 Phase A 的 `create_app` 工厂**（统一一个 `app`），或保留两个工厂、`uvicorn app.main:app` 指向合并后的工厂。
  4. 落地后跑两侧全量测试（Phase A 132 + Phase B 139，合并后应无重叠丢失），确认 Phase A e2e（sample1-4）+ Phase B e2e（样例3/4 闭环）全绿。
  - **这是一次架构决策（两阶段入口如何合一），需用户拍板合并策略后再动手**；不能由执行侧单方面定。

### 冲突 2 · `app/plugins/llm_client.py`（语义冲突）

- **冲突内容**：两侧各写了一套 `LLMClient` Protocol + `MockLLMClient`：
  - main 版：`MockLLMClient(fixture_dir)` 读 `mock_llm_outputs.json`（P0 冻结），按 `[SOURCE_SN:<sn>]` 正则路由到 `extraction` block（服务抽取 Skill）。**无 `OpenAICompatClient`、无网络、无 `ZHL_LLM_*` env**。
  - Phase B 版：`MockLLMClient(fixture_path)` 读 `mock_llm_attribution.json`（PB 新增 L1a），按 `incident_id` 正则路由；**另加 `OpenAICompatClient`（httpx 同步调 `/chat/completions`，读 `ZHL_LLM_*` env，空 key 构造即 ValueError）**。
- **为什么不能选边**：选 main 版 → 丢 Phase B 归因 Mock 路由（按 `incident_id`）+ `OpenAICompatClient`，Phase B 归因 Skill / 子图测试失败；选 Phase B 版 → 丢 Phase A `[SOURCE_SN]` 抽取路由，Phase A 抽取 Skill / e2e 失败。
- **建议处理方案（取并集）**：合并为一个 `llm_client.py`，同时容纳**两种 Mock 路由**（`MockLLMClient` 按标记类型区分：`[SOURCE_SN]` → extraction block；`incident_id` → attribution block）+ 保留 `OpenAICompatClient`（P1 生产路径，Phase A 标为 P1-only 不冲突）。**注意冻结夹具边界**：Phase A 的 `mock_llm_outputs.json` 与 Phase B 的 `mock_llm_attribution.json` 是**两个不同文件**，合并后都保留、互不覆盖（Phase B 未改 P0 冻结件，合规）。需用户确认"双 Mock 路由共存于一个 `LLMClient` Protocol"的合并方式。

### 冲突 3 · `pyproject.toml`（content 冲突，低风险）

- **冲突内容**：
  - main 版：`[project] dependencies` 精简 + 新增 PEP 735 `[dependency-groups]`（`dev` 组含 `pytest`/`pytest-cov`/`ruff`/`black`/`aiosqlite`…）。`langgraph>=1.2.14` 已在。
  - Phase B 版：`[project] dependencies` 加 `langgraph>=1.2.14`（与 main 同）+ `python-dotenv>=1.2.4`（Phase B 为 `.env` 加载新加）。
- **建议处理方案**：保留 main 版的 PEP 735 dev-group 结构（更完整，Phase A 测试靠它），在其 `[project] dependencies` 中**补 `python-dotenv>=1.2.4`**（Phase B 需要）；`langgraph` 两侧一致去重。落地后 `uv lock` 重锁。

### 冲突 4 · `tests/plugins/test_llm_client.py`（add/add，须随冲突 2 定）

- **冲突内容**：main 版测 Phase A `[SOURCE_SN]` Mock 路由；Phase B 版测 `incident_id` 路由 + `OpenAICompatClient` 空 key ValueError。
- **建议处理方案**：与冲突 2 一致——**合并为两个测试文件共存**（如 `test_llm_client_phase_a.py` + `test_llm_client_phase_b.py`，或单文件内分组），避免互相覆盖。命名/分组需用户定。

## 3. 合并后必做的验证（门禁）

无论取哪种合并策略，落地后必须：
1. `uv lock` + `uv sync`（`uv.lock` 两侧条目不同，重锁）。
2. `uv run ruff check . && uv run black --check .`（合并代码风格对齐）。
3. `uv run pytest -x` 全量（Phase A e2e sample1-4 + Phase B e2e 样例3/4 + 两侧 API/domain/plugins 测试，合计应 ≥ 271 且无丢失）。
4. 冻结契约核验：`git diff phase0-frozen..HEAD -- app/domain/ app/infra/models.py migrations/ tests/fixtures/` 中，**P0 冻结件（除经 PR 放行的 models 加列 + utcnow 修复）不得被合并破坏**；`mock_llm_outputs.json` 零改动。
5. 入口核验：`uv run uvicorn app.main:app` 能起、`/docs` 200、Phase A + Phase B 路由都在（冲突 1 方案落地后）。

## 4. 结论与待决项

**冲突根因**：`feature/phase-b` 基于 Phase 0 末端切出，未含 Phase A；而 Phase A 已并入 origin/main。两阶段对 `app/main.py` 与 `llm_client.py` 各写一套同名不同语义的实现，git 无法自动调和。

**待用户拍板（合并策略）**——本文档**不替用户选**，以下为可选路线：

- **路线 A（推荐，取并集）**：手工合并 4 冲突文件为"两阶段共存"的并集（`app/main.py` 合一工厂挂两组路由；`llm_client.py` 双 Mock 路由 + `OpenAICompatClient`；`pyproject` 补 dotenv；测试分文件）。工作量大，但保留两阶段全部功能。
- **路线 B（先补基线再合）**：把 `feature/phase-b` rebase 到 `origin/main` 之上再合并，让 Phase B 代码天然看见 Phase A，冲突在 rebase 期间逐个解决。适合后续还要在 main 上叠加更多阶段。
- **路线 C（分阶段入口）**：保留两个 app 入口（`app/main.py` 走 Phase A 工厂；Phase B 入口另置），不强行合一。最小改动，但 `uvicorn app.main:app` 只能挂一个，需拆入口。

**当前状态**：合并已 `git merge --abort`，工作树干净停在 `main = f088e6d`。请指示路线（A / B / C 或其他），我再执行合并 + 全量验证；或在选定的路线下，由我先把 4 处冲突的具体合并 diff 草拟出来给你审阅。
