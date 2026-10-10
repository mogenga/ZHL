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

