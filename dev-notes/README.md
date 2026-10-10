# dev-notes/ · 目录说明

> 规范来源：`AGENT.md` §1 过程留痕

## 命名规则

每位开发 Agent（人类或 AI）在开始工作前创建自己的文件：

```
dev-notes/{开发人员ID}.md
```

- **AI Agent**：`dev-notes/zhl-agent.md`（已由 Agent 维护）
- **人类开发者**：`dev-notes/{姓名或拼音}-{YYYYMMDD}.md`，例如 `mogen-20261010.md`
- 同一人跨天工作可在原文件追加，不需每天新建

## 文件结构

每个文件按时间顺序追加段落，每段对应一个 AGENT.md §1.2 节点（brainstorm 定稿 / 计划评审 / 任务完成 / code review / finish）。

每段四要素（AGENT.md §1.3）：
1. 关键原话（触发本阶段的用户指令或自我决策）
2. 关键产出（spec/plan/代码路径）
3. 拒绝与纠偏（被驳回/修正的内容）
4. 翻车与返工（踩坑、重做原因）

## 禁止事项

- 不许收尾时一次性补记所有段落
- 不许在他人文件中追加自己的记录（`dev-notes/` 下每个文件归属唯一负责人）
