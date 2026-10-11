# Phase 0 · 共享契约设计（Shared Contract）

日期：2026-10-10
定位：Phase 0 的可执行开发规格。上游为 `2026-10-10-zhl-system-spec.md`（下称「总 spec」）与 AGENT.md；本文把 Phase 0 范围收敛为可直接写 plan 的实现规格。

---

## 1. 意图与为什么

Phase 0 是整个系统的共享契约层：Phase A（数据入场+判定）与 Phase B（异常+报告）并行开发，双方都只读 Phase 0 的产出。Phase 0 必须先行完成并冻结（AGENT.md §6.1），否则并行冲突。

本文回答「Phase 0 具体做什么、怎么做、做到什么程度算完成」。

## 2. 范围

### 2.1 Goals（Phase 0 交付物）

1. **DB Schema**：`migrations/001_init.sql`，一份 DDL 定义**全部 12 张表**（架构 §4.1 列 11 张 + Phase 0 补入的只读配置表 `iso_sampling_table`）+ 约束 + 索引 + 触发器，可独立执行（不依赖应用代码，AGENT.md §4.1）
2. **domain 纯函数**：`app/domain/` 下判定引擎、放行断言、批次/异常状态转移，纯函数可脱离 DB 与模型独立测试
3. **数据模型**：`app/infra/models.py` SQLAlchemy 2.0 模型 + Pydantic v2 schema
4. **证据库接口**：`app/plugins/evidence_store.py` 的 `EvidenceStore` Protocol + 实现骨架
5. **测试夹具**：`tests/fixtures/` 四组验收样例数据 + 规则种子 + ISO 抽样表
6. **全绿单测**：domain 纯函数单测全绿，覆盖率 ≥ 90%

### 2.2 Non-goals（Phase 0 不做）

- 不做 API 路由、页面、LangGraph 图（Phase A/B 与集成阶段）
- 不实现 Skill/Plugin 的业务编排逻辑（只定 Protocol 与骨架）
- 不接入真实模型服务（用 MockLLMClient）
- 不实现 ISO 抽样方案三（O-1 已锁方案一，总 spec §5）

### 2.3 Anti-goals（做错了算失败）

- domain 层 import infra / sqlalchemy / langgraph / 模型服务（AGENT.md 红线）
- DDL 分多份迁移文件、Phase A/B 需再改 DDL
- 用触发器做能用声明式约束表达的事（AGENT.md §4.2 约束优先）
- 夹具数据与四组验收样例对不上

## 3. 约束（全部来自上游，逐条引用）

### 3.1 技术栈（总 spec §3.1 锁定）

Python 3.11+ / FastAPI / PostgreSQL 15+ / SQLAlchemy 2.0 / Pydantic v2 / pytest + httpx。禁止引入 spec 之外的新库（AGENT.md §2.3）。

### 3.2 domain 纯度（总 spec §3.2 + AGENT.md §8）

判定/断言/状态机纯函数：不读库、不取时间、不调随机、不调模型、不 import `sqlalchemy` / `app.infra.*` / `langgraph`。

### 3.3 判定顺序（总 spec §2.3）

① 基础校验（六项电参数+SN+等级+日期非空）→ ② 功率范围 → ③ 参数容差（闭区间）→ ④ 电流分档 → ⑤ 外观。边界值：等于上下限均 Pass。浮点比较用容差区间。

### 3.4 状态机（总 spec §2.5 / 架构 §5.4）

批次 10 态 + 异常 5 态，转移表以数据形式定义，前置条件写死在 domain，模型不可改写。

### 3.5 放行断言（总 spec §2.4）

`assert_release` 逐条返回 6 项检查结果（state / sample_passed / no_open_critical_incident / no_isolated_sn / all_evidence_present / all_capa_verified），不返回单一布尔。

### 3.6 DB 约束（架构 §4.2）

`UNIQUE(batch_id, sn)`；SN 跨批次用复合外键 + EXCLUDE 排他；异常必须经 `incident_sn_links` 关联 SN；规则版本发布后不可改（触发器）；原始检验值不可改（列级权限+触发器）；抽样结论唯一键 `(batch_id, inspection_type, rule_version_id, sample_round)`；证据只追加（撤销 UPDATE/DELETE）。

### 3.7 测试（AGENT.md §3.3）

domain 单测 ≥ 90% 覆盖，纯内存跑（pytest，不依赖 DB 与模型）；插件集成 ≥ 70%。

### 3.8 交付（AGENT.md §5）

一条可复现演示命令（含 DB 初始化 + 种子数据）+ 全量 pytest 结果 + dev-notes 路径（`agnes-20261010`）+ 已知遗留。

## 4. 关键决策（含我拍板的项，标记 *）

> 带 * 的是我在 spec 之外自行拍板的默认值，依据上游推导；如有异议请在评审时指出。

| # | 决策 | 依据 |
| --- | --- | --- |
| D1 | DDL 一份文件 `migrations/001_init.sql`，建齐全部 12 表（口径统一为 12，含 `iso_sampling_table`，见 D16），Phase A/B 零 DDL 变更（已确认） | 总 spec §4.0；AGENT.md §6.1 |
| D2 | 测试夹具覆盖四组验收样例 + 规则种子 + ISO 抽样表，Phase B 归因/复检样例同批提供（已确认，O-2 假设历史数据不可得） | 总 spec §5；需求 §11.4 |
| D3 * | 表结构以架构 §4.1 的 11 表 + `iso_sampling_table` 为准（共 12 张）；`module_inspections` 的 `rule_snapshot` / `decision_evidence` 用 JSONB，关键计算项（pmax/isc/voc/vm/im/ff/appearance_result/decision_status）冗余独立列 | 架构 §4.3 |
| D16（确认） | 表数口径统一为 12：架构 §4.1 未列 `iso_sampling_table`（只读配置表），Phase 0 补入；完成标准同步按 12 张表述 | 用户确认 |
| D17（确认） | **EXCLUDE 约束不降级**：SN 跨批次静默覆盖是需求 §2.2 明写核心断点，必须落 DB 层。实现 `btree_gist + sn_uniqueness int8range`（见 D5）；psql 验证若 EXCLUDE 报错，保留 DDL 并加应用层兜底检查（写入 001_init.sql 注释与 `tests/domain` 用例），**不降级为普通 UNIQUE**，交付物不变 | 用户确认 |
| D18（确认） | 演示范围止于 DDL + 单测：Phase 0 不起 FastAPI、不接 uvicorn（API 属 Phase A/B），演示命令为 `psql -f migrations/001_init.sql && uv run pytest tests/ -v` | 用户确认 |
| D19（确认） | alembic 在 Phase 0 仅完成配置初始化（`alembic.ini` + `alembic/` 目录 + env.py，不生成 revision）；触发器/EXCLUDE/权限语句全部手写进 `001_init.sql`，不走 alembic；Phase A/B 增量 DDL（若有）走 alembic revision | 用户确认 |
| D20（确认） | 冻结动作：Phase 0 最后一项任务为「评审 + 冻结」——plan 批准后人工评审通过，dev-notes 记「计划评审通过」节点，对 `app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/` 打 git tag `phase0-frozen`；此后 A/B 改契约必须走 PR + review（AGENT.md §6.1） | 用户确认 |
| D4 * | `incident_sn_links` 单独建表（FK 双向到 quality_incidents 与 module_inspections，UNIQUE(incident_id, sn)），不用 JSONB 数组 | 架构 §4.2「不用自由文本」 |
| D5 * | SN 跨批次 EXCLUDE 约束加在 `module_inspections` 上：冲突语义为「同一 SN 出现在 ≥2 个批次」。实现：001_init.sql 头部 `CREATE EXTENSION IF NOT EXISTS btree_gist`；`module_inspections` 增辅助列 `sn_num bigint`（插入时由 `pg_catalog.hashtext(sn)` 填充，触发器保证一致性）与 `sn_uniqueness int8range`（CHECK 约束 `sn_uniqueness = int8range(sn_num, sn_num + 1, '[]')`）；`EXCLUDE USING gist (sn_uniqueness WITH &&)`。同 SN 的两行 range 完全重叠，第二个批次插入即被拒绝。去留标准：不降级（D17） | 架构 §4.2 |
| D6 * | 规则版本不可变：`quality_rule_versions` 加 BEFORE UPDATE/DELETE 触发器，`status='published'` 时 `RAISE EXCEPTION`；停用走 `POST /api/rules/{id}/disable` 改 status（停用不属 UPDATE 语义）→ 触发器条件仅拦截 published 态的字段值 UPDATE，允许 status 单列变更 | 总 spec §9.3 |
| D7 * | 原始检验值不可改：`module_inspections` 列级 `ALTER TABLE ... NO UPDATE ON (pmax, isc, voc, vm, im, ff, appearance_result)` 给应用角色 + 触发器双保险 | 架构 §4.2 |
| D8 * | 证据只追加：`evidence` 表 `REVOKE UPDATE, DELETE` 给应用角色 | 架构 §4.2 |
| D9 * | 抽样表预置 3 检验类型 × 3 AQL（0.65/1.0/1.5）× 批量区间，约 30 行，字段 `[inspection_type, inspection_level, aql, lot_size_min, lot_size_max] → [n, ac, re]`；缺项查不到即判规则异常（不放行） | 总 spec §9.4；需求 §7 方案一 |
| D10 * | 证据文件目录 `data/evidence/`，运行时定位 `ZHL_EVIDENCE_ROOT`（默认 `data/evidence`），入库算 SHA-256 存 `evidence.content_hash`；大文件不入 DB | 总 spec §9.5 |
| D11 * | `EvidenceStore` Protocol：`save(type, object_ref, content_bytes|path, meta) -> EvidenceRef`；`get(evidence_id) -> EvidencePayload`；`list_by_object(object_ref) -> [EvidenceRef]`；实现骨架 `FileEvidenceStore`（本地目录 + 哈希），Phase A/B 可替换 | 总 spec §4.0；架构 §9.1 |
| D12 * | 规则种子 `tests/fixtures/rules_seed.json`：2 型号（JH-550W 默认 + 1 测试型号），初始化时幂等导入（按 `project_id+model+version_no` 唯一去重） | 总 spec §9.3 |
| D13 * | dev-notes ID 按 `agnes-20261010` 记 | AGENT.md §1.1 |
| D14 * | 项目脚手架（`pyproject.toml`、目录骨架、pytest 配置、alembic 配置）属 Phase 0 前置，随本 Phase 一并完成 | AGENT.md 交付规范 |

## 5. 产出物规格

### 5.1 12 张表的字段清单（SQLAlchemy 模型即权威定义）

> 口径：12 张（D16）。架构 §4.1 列 11 张，`iso_sampling_table` 为 Phase 0 补入的只读配置表（D9/D16）。
>
> 公共约定：
> - 列名 snake_case；主键统一 `*_id` UUID（`gen_random_uuid()`）；`created_at`/`updated_at` 为 `timestamptz` 默认 `now()`
> - JSONB 用于结构化快照与证据内容；关键计算项冗余独立列（D3）
> - `created_at` / `updated_at` 不出现在所有表的字段表中（每张表均有，默认 `now()`），仅在有特殊约束时单独说明
> - 主键列、外键列用 **加粗** 标注

### 5.1.1 `quality_rule_versions`（规则版本）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`rule_version_id`** | UUID | PK, `gen_random_uuid()` | 规则版本 ID |
| `project_id` | text | NOT NULL | 项目 ID |
| `model` | text | NOT NULL | 组件型号 |
| `name` | text | NOT NULL | 规则版本名称 |
| `nameplate_power` | numeric | NOT NULL, `> 0` | 铭牌功率 |
| `power_tolerance` | numeric | NOT NULL, `0..1` | 功率容差（比例） |
| `parameter_standards` | JSONB | NOT NULL | 温度/Isc/Voc/Vm/FF 标准值+容差，项可禁用 |
| `current_bands` | JSONB | NOT NULL | Impp 区间数组（区间不重叠由 CHECK + 触发器保证） |
| `sample_plan` | JSONB | NOT NULL | `{inspection_type, inspection_level, aql}` |
| `effective_at` | timestamptz | NOT NULL | 生效时间 |
| `status` | enum(`draft`, `published`, `disabled`) | NOT NULL, 默认 `draft` | 仅已发布规则可判定 |
| `version_no` | int | NOT NULL | 版本号 |
| `created_at` | timestamptz | 默认 `now()` | |
| `updated_at` | timestamptz | 默认 `now()` | |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| UNIQUE | `(project_id, model, version_no)` | 幂等导入（D12） |
| TRIGGER | BEFORE UPDATE / DELETE：`status = 'published'` 时 `RAISE EXCEPTION`（仅允许 `status` 单列变更） | 规则版本不可变（D6） |

### 5.1.2 `delivery_batches`（交付批次）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`batch_id`** | UUID | PK, `gen_random_uuid()` | 批次 ID |
| `batch_no` | text | NOT NULL, UNIQUE | 批次编号 |
| `project_id` | text | NOT NULL | 项目 ID |
| `model` | text | NOT NULL | 组件型号 |
| `planned_quantity` | int | NOT NULL | 计划数量 |
| `actual_quantity` | int | NOT NULL, 默认 `0` | 已纳入数量 |
| **`rule_version_id`** | UUID | FK → `quality_rule_versions.rule_version_id`, NOT NULL | 本批次绑定的规则版本 |
| `status` | enum(`draft`, `pending_intake`, `pending_judge`, `pending_release`, `isolated`, `disposing`, `pending_recheck`, `rejected`, `released`, `archived`) | NOT NULL, 默认 `draft` | 批次 10 态（总 spec §2.5） |
| `release_decision` | enum(`released`, `rejected`, `undecided`) | NOT NULL, 默认 `undecided` | 放行决定 |
| **`release_evidence_id`** | UUID | FK → `evidence.evidence_id`, NULL | 放行断言与报告的证据 ID |
| `created_at` | timestamptz | 默认 `now()` | |
| `updated_at` | timestamptz | 默认 `now()` | |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| UNIQUE | `batch_no` | 批次编号唯一 |

### 5.1.3 `module_inspections`（组件检验记录）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`inspection_id`** | UUID | PK, `gen_random_uuid()` | 记录 ID |
| **`batch_id`** | UUID | FK → `delivery_batches.batch_id`, NOT NULL | 所属批次 |
| `sn` | text | NOT NULL | 序列号 |
| `sn_num` | bigint | NOT NULL | D5：`pg_catalog.hashtext(sn)`，插入触发器填充，供 EXCLUDE 使用 |
| `sn_uniqueness` | int8range | NOT NULL, CHECK `= int8range(sn_num, sn_num + 1, '[]')` | D5：EXCLUDE 辅助列 |
| `source_type` | enum(`OCR_FORM`, `IV_CSV`, `MANUAL`, `SIMULATED_DEVICE`) | NOT NULL | 数据来源类型 |
| **`source_file_id`** | UUID | FK → `evidence.evidence_id`, NULL | 原始文件 |
| `captured_at` | timestamptz | NOT NULL | 检测时间 |
| `pmax` | numeric | NOT NULL | 最大功率 |
| `temp` | numeric | NOT NULL | 温度 |
| `isc` | numeric | NOT NULL | 短路电流 |
| `voc` | numeric | NOT NULL | 开路电压 |
| `vm` | numeric | NOT NULL | 最大功率点电压 |
| `im` | numeric | NOT NULL | 最大功率点电流 |
| `ff` | numeric | NOT NULL | 填充因子 |
| `appearance_result` | enum(`pass`, `fail`) | NOT NULL | 外观结论 |
| `intake_status` | enum(`pending`, `confirmed`, `rejected`) | NOT NULL, 默认 `pending` | 摄入状态 |
| `decision_status` | enum(`pending`, `pass`, `fail`, `rule_missing`) | NOT NULL, 默认 `pending` | 判定状态 |
| `rule_snapshot` | JSONB | NULL | 判定时的规则快照 |
| `decision_evidence` | JSONB | NULL | 逐项四元组（输入/阈值/公式/结果） |
| `created_at` | timestamptz | 默认 `now()` | |
| `updated_at` | timestamptz | 默认 `now()` | |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| UNIQUE | `(batch_id, sn)` | 组件在批次内唯一（需求 §3.3） |
| EXCLUDE | `USING gist (sn_uniqueness WITH &&)`，需 `CREATE EXTENSION btree_gist` | SN 跨批次不可重复（D5/D17，不降级） |
| TRIGGER | BEFORE INSERT：自动填充 `sn_num = hashtext(sn)` | D5 |
| 列级权限 | `NO UPDATE ON (pmax, temp, isc, voc, vm, im, ff, appearance_result)` 给应用角色 | 原始检验值不可改（D7） |

### 5.1.4 `quality_incidents`（异常事件）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`incident_id`** | UUID | PK, `gen_random_uuid()` | 异常 ID |
| **`batch_id`** | UUID | FK → `delivery_batches.batch_id`, NOT NULL | 所属批次 |
| `trigger_type` | enum(`rule_fail`, `sample_fail`, `appearance_manual`, `data_consistency`, `rule_missing`) | NOT NULL | 触发类型 |
| `severity` | enum(`critical`, `major`, `minor`) | NOT NULL | 严重度 |
| `evidence_ids` | JSONB | NOT NULL, 默认 `'[]'` | 证据 ID 数组（应用层校验存在性） |
| `owner` | text | NULL | 处置责任人 |
| `due_at` | timestamptz | NULL | 处置时限 |
| `containment_action` | text | NULL | 临时遏制措施 |
| `root_cause_hypotheses` | JSONB | NULL | 假设数组（rank/cause/confidence/evidence/recommended_action） |
| `confirmed_root_cause` | text | NULL | 人工确认的根因 |
| `corrective_action` | text | NULL | 纠正措施 |
| **`verification_id`** | UUID | FK → `reinspections.reinspection_id`, NULL | 对应复检记录 |
| `status` | enum(`new`, `isolated`, `assigned`, `pending_recheck`, `closed`, `rejected`, `scrapped`) | NOT NULL, 默认 `new` | 异常 7 态（含 2 终态） |
| `created_at` | timestamptz | 默认 `now()` | |
| `updated_at` | timestamptz | 默认 `now()` | |

**表级约束**：无额外表级约束；异常必须经 `incident_sn_links` 关联 SN（D4，不用自由文本）。

### 5.1.5 `incident_sn_links`（异常与 SN 关联）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`incident_id`** | UUID | FK → `quality_incidents.incident_id`, NOT NULL, ON DELETE CASCADE | 异常 ID |
| **`inspection_id`** | UUID | FK → `module_inspections.inspection_id`, NOT NULL, ON DELETE CASCADE | 检验记录 ID |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| PK | `(incident_id, inspection_id)` 复合主键 | |
| UNIQUE | `(incident_id, inspection_id)`（与 PK 等价，显式声明） | D4 |

### 5.1.6 `reinspections`（复检记录）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`reinspection_id`** | UUID | PK, `gen_random_uuid()` | 复检 ID |
| **`incident_id`** | UUID | FK → `quality_incidents.incident_id`, NOT NULL | 关联异常 |
| **`inspection_id`** | UUID | FK → `module_inspections.inspection_id`, NOT NULL | 被复检的初检 |
| **`rule_version_id`** | UUID | FK → `quality_rule_versions.rule_version_id`, NOT NULL | 复检所用规则版本 |
| **`source_file_id`** | UUID | FK → `evidence.evidence_id`, NULL | 复检源文件 |
| `pmax` | numeric | NOT NULL | 复检时最大功率 |
| `temp` | numeric | NOT NULL | 复检时温度 |
| `isc` | numeric | NOT NULL | 复检时短路电流 |
| `voc` | numeric | NOT NULL | 复检时开路电压 |
| `vm` | numeric | NOT NULL | 复检时最大功率点电压 |
| `im` | numeric | NOT NULL | 复检时最大功率点电流 |
| `ff` | numeric | NOT NULL | 复检时填充因子 |
| `result` | enum(`pass`, `fail`) | NOT NULL | 复检结果 |
| `executor` | text | NOT NULL | 执行人 |
| `executed_at` | timestamptz | NOT NULL | 执行时间 |
| **`evidence_id`** | UUID | FK → `evidence.evidence_id`, NULL | 复检证据 |
| `created_at` | timestamptz | 默认 `now()` | |
| `updated_at` | timestamptz | 默认 `now()` | |

**表级约束**：`reinspections` 对 `module_inspections` 只读引用，不覆盖初检（需求 §3.5）。

### 5.1.7 `samples`（抽样样本清单）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`sample_id`** | UUID | PK, `gen_random_uuid()` | 样本 ID |
| **`batch_id`** | UUID | FK → `delivery_batches.batch_id`, NOT NULL | 所属批次 |
| `inspection_type` | text | NOT NULL | 检验类型 |
| **`rule_version_id`** | UUID | FK → `quality_rule_versions.rule_version_id`, NOT NULL | 规则版本 |
| `sample_round` | int | NOT NULL, 默认 `1` | 抽样轮次 |
| `sn` | text | NOT NULL | 被抽组件 SN |
| `selected_at` | timestamptz | NOT NULL | 选中时间 |
| `seed` | text | NOT NULL | 随机种子（= `batch_id` 字符串） |
| `created_at` | timestamptz | 默认 `now()` | |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| UNIQUE | `(batch_id, inspection_type, rule_version_id, sample_round, sn)` | 同一批次同轮次不重复抽同一 SN |

### 5.1.8 `sample_conclusions`（抽样结论）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`conclusion_id`** | UUID | PK, `gen_random_uuid()` | 结论 ID |
| **`batch_id`** | UUID | FK → `delivery_batches.batch_id`, NOT NULL | 所属批次 |
| `inspection_type` | text | NOT NULL | 检验类型 |
| **`rule_version_id`** | UUID | FK → `quality_rule_versions.rule_version_id`, NOT NULL | 规则版本 |
| `sample_round` | int | NOT NULL, 默认 `1` | 抽样轮次 |
| `n` | int | NOT NULL | 样本量 |
| `ac` | int | NOT NULL | 接收数 |
| `re` | int | NOT NULL | 拒收数 |
| `result` | enum(`pass`, `fail`, `rule_missing`) | NOT NULL | 抽样结论 |
| **`evidence_id`** | UUID | FK → `evidence.evidence_id`, NULL | 抽样证据 |
| `created_at` | timestamptz | 默认 `now()` | |
| `updated_at` | timestamptz | 默认 `now()` | |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| UNIQUE | `(batch_id, inspection_type, rule_version_id, sample_round)` | 抽样结论唯一（架构 §4.2） |

### 5.1.9 `iso_sampling_table`（ISO 抽样表，只读配置）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| `row_id` | int | PK, 自增 | 行号 |
| `inspection_type` | text | NOT NULL | 检验类型 |
| `inspection_level` | text | NOT NULL | 检验水平 |
| `aql` | numeric | NOT NULL | AQL 值 |
| `lot_size_min` | int | NOT NULL | 批量区间下限 |
| `lot_size_max` | int | NOT NULL | 批量区间上限 |
| `n` | int | NOT NULL | 样本量 |
| `ac` | int | NOT NULL | 接收数 |
| `re` | int | NOT NULL | 拒收数 |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| CHECK | `ac < re` | 接收数必须小于拒收数 |
| UNIQUE | `(inspection_type, inspection_level, aql, lot_size_min, lot_size_max)` | 同一组合唯一 |
| 权限 | `REVOKE UPDATE, DELETE` 给应用角色；仅 `INSERT`（初始化）+ `SELECT` | 只读配置表（D9） |

### 5.1.10 `evidence`（证据库）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`evidence_id`** | UUID | PK, `gen_random_uuid()` | 证据 ID |
| `evidence_type` | enum(`source_file`, `extraction_result`, `human_confirmation`, `decision_snapshot`, `task_trace`, `report_snapshot`) | NOT NULL | 证据类型 |
| `object_ref` | JSONB | NOT NULL | 关联对象 `{kind, id}`；`kind` 枚举：`batch`\|`inspection`\|`incident`\|`reinspection`\|`rule`\|`report`\|`trace`\|`sample` |
| `content_hash` | text | NOT NULL | SHA-256 哈希 |
| `file_path` | text | NULL | 文件路径（相对 `ZHL_EVIDENCE_ROOT`） |
| `content` | JSONB | NULL | 小体积内容内联 |
| `generated_at` | timestamptz | NOT NULL, 默认 `now()` | 生成时间 |
| `generated_by` | text | NOT NULL, 默认 `'system'` | 生成者 |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| 权限 | `REVOKE UPDATE, DELETE` 给应用角色 | 证据只追加（D8） |

### 5.1.11 `agent_task_traces`（Agent 任务轨迹）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`task_id`** | UUID | PK, `gen_random_uuid()` | 任务 ID |
| **`batch_id`** | UUID | FK → `delivery_batches.batch_id`, NULL | 关联批次 |
| `input_ref` | JSONB | NOT NULL | 输入引用 |
| `skill_name` | text | NOT NULL | Skill 名称 |
| `plugins_invoked` | JSONB | NOT NULL, 默认 `'[]'` | 调用的 Plugin 列表 |
| `params_summary` | JSONB | NOT NULL, 默认 `'{}'` | 工具参数摘要 |
| `model_version` | text | NULL | 模型版本 |
| `structured_output` | JSONB | NULL | 结构化输出 |
| `validation_result` | enum(`pass`, `fail`, `partial`) | NOT NULL, 默认 `partial` | 验证结果 |
| `failure_reason` | text | NULL | 失败原因 |
| `human_action` | JSONB | NULL | 人工操作 |
| `started_at` | timestamptz | NOT NULL | 开始时间 |
| `finished_at` | timestamptz | NULL | 结束时间 |

**表级约束**：无额外表级约束。

### 5.1.12 `reports`（报告版本）

| 列名 | 类型 | 约束 / 默认 | 说明 |
|------|------|-------------|------|
| **`report_id`** | UUID | PK, `gen_random_uuid()` | 报告 ID |
| **`batch_id`** | UUID | FK → `delivery_batches.batch_id`, NOT NULL | 关联批次 |
| `report_type` | enum(`release`, `incident`) | NOT NULL | 报告类型（未放行批次只能生成处置报告） |
| `version` | int | NOT NULL, 默认 `1` | 版本号（自增不覆盖） |
| **`source_snapshot_id`** | UUID | FK → `evidence.evidence_id`, NOT NULL | 源快照引用 |
| **`agent_task_id`** | UUID | FK → `agent_task_traces.task_id`, NULL | 生成任务的轨迹 ID |
| `content_md` | text | NOT NULL | Markdown 内容 |
| `content_json` | JSONB | NOT NULL | 结构化 JSON |
| `generated_at` | timestamptz | NOT NULL, 默认 `now()` | 生成时间 |

**表级约束**

| 类型 | 定义 | 说明 |
|------|------|------|
| UNIQUE | `(batch_id, report_type, version)` | 版本化不覆盖 |

### 5.2 domain 纯函数签名（`app/domain/`）

```python
# judge.py
def judge(inspection: ModuleInspectionIn, rule_snapshot: RuleSnapshotIn) -> Decision
def check_required_fields(inspection) -> RuleCheck
def check_power_range(inspection, rule_snapshot) -> RuleCheck
def check_parameter_tolerances(inspection, rule_snapshot) -> RuleCheck
def check_current_band(inspection, rule_snapshot) -> RuleCheck
def check_appearance(inspection) -> RuleCheck
def tolerance_interval(std: float, tol: float) -> tuple[float, float]
# release.py
def assert_release(batch: BatchView, sample: SampleView, incidents: [IncidentView], isolated_sns: [str], evidence: [EvidenceView], capa: [CapaView]) -> AssertionResult
def check_state(...) -> AssertionCheck
def check_sample_passed(...) -> AssertionCheck
def check_no_open_critical_incident(...) -> AssertionCheck
def check_no_isolated_sn(...) -> AssertionCheck
def check_all_evidence_present(...) -> AssertionCheck
def check_all_capa_verified(...) -> AssertionCheck
# state_machine.py
BATCH_TRANSITIONS: dict[tuple[str, str], frozenset]  # 转移表数据
INCIDENT_TRANSITIONS: dict[tuple[str, str], frozenset]
def transition(current: str, target: str, preconditions: dict, kind: str) -> TransitionResult
def close_incident(incident: IncidentView, reinspection: ReinspectionView, capa_verified: bool) -> CloseResult
# sampling.py（供 Phase A 调用，Phase 0 定接口）
def iso_lookup(inspection_type, level, aql, lot_size, table: [IsoRow]) -> IsoResult | None
def freeze_sample(batch_id, lot_size, iso_row, sns: [str], seed: str) -> [SampleEntry]  # 分层随机，固定 seed
```

> 纯函数入参用 dataclass（`ModuleInspectionIn` / `RuleSnapshotIn` / `Decision` 等），不 import ORM。`Decision` / `AssertionResult` / `IncidentTrigger` / `ReinspectionResult` 结构遵循总 spec §6.2 接口契约（AGENT.md §6.2）。

### 5.3 数据模型（`app/infra/models.py`）

SQLAlchemy 2.0 Declarative，映射 §5.1 全部 12 表。枚举用 `sqlalchemy.Enum`。`rule_snapshot` / `decision_evidence` / `root_cause_hypotheses` / `object_ref` / `structured_output` 等用 `JSONB`。不引入 Alembic 之外迁移工具。

Pydantic v2 schema 同目录（`app/infra/schemas.py`）：请求/响应模型，复用 domain 的 dataclass 做类型约束（Protocol 层）。

### 5.4 证据库骨架（`app/plugins/evidence_store.py`）

```python
class EvidenceStore(Protocol):
    def save(self, etype: EvidenceType, object_ref: dict, *, content: bytes | None = None, path: str | None = None, meta: dict | None = None) -> EvidenceRef: ...
    def get(self, evidence_id: str) -> EvidencePayload: ...
    def list_by_object(self, object_ref: dict) -> list[EvidenceRef]: ...

class FileEvidenceStore:  # 骨架：本地目录 + SHA-256，写 evidence 表
    def __init__(self, root: Path, db: Session): ...
```
Phase 0 只实现协议 + `FileEvidenceStore` 的 `save`/哈希逻辑骨架（不接 DB 写入，留给 Phase A）。

### 5.5 测试夹具（`tests/fixtures/`）

| 文件 | 内容 | 对应验收样例 |
| --- | --- | --- |
| `acceptance_normal.json` | 1 批次 10 组件全 Pass + 规则快照 | 样例一 正常放行 |
| `acceptance_boundary.json` | Pmax=功率下限 Pass + 对照组 下限−0.1 Fail | 样例二 边界值 |
| `acceptance_incident.json` | Impp 超档 + 历史同类 + 复检数据 | 样例三 异常隔离复检 |
| `acceptance_consistency.json` | 同 SN 跨批次 | 样例四 数据一致性 |
| `rules_seed.json` | 2 型号规则（JH-550W 默认 + 测试型号） | 总 spec §9.3 |
| `iso2859_1_aql.json` | 3 类型 × 3 AQL × 批量区间 ≈ 30 行 | 总 spec §9.4 |
| `mock_llm_outputs.json` | 归因/抽取的固定模型输出（MockLLMClient 用） | 总 spec §9.1 |

夹具加载器 `tests/fixtures/loader.py`：`load_acceptance(name) -> dict`，提供 batch/inspections/rules/incidents 等命名空间。

### 5.6 项目脚手架（D14 / D19）

`pyproject.toml`（Python 3.11+, 依赖：fastapi / sqlalchemy[asyncio] / alembic / pydantic[2] / pytest / httpx / psycopg[binary] / langgraph-checkpoint-postgres；lint: ruff + black 配置）、目录骨架（总 spec §3.3）、`pytest.ini`（testpaths、asyncio 配置）、`alembic.ini` + `alembic/`（D19：仅初始化配置与 `env.py`，**不生成 revision**；触发器/EXCLUDE/权限全部手写 `001_init.sql`，Phase A/B 增量 DDL 才走 alembic revision）。

### 5.7 冻结流程（D20）

Phase 0 最后一项交付：
1. 人工评审通过本 spec + plan（dev-notes 记「计划评审通过」节点）
2. 对冻结目录 `app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/` 打 git tag `phase0-frozen`
3. 此后 Phase A/B 修改冻结项必须走 PR + review（AGENT.md §6.1）；dev-notes「code review 结论」节点记评审人 + 通过/驳回 + 主要意见

## 6. 完成标准（Phase 0 退出条件）

- [ ] `migrations/001_init.sql` 可独立执行于 PostgreSQL 15+（`psql -f` 无错），含全部 12 表 + 约束 + 索引 + 触发器 + `btree_gist` 扩展
- [ ] `app/domain/` 纯函数单测全绿，domain 覆盖率 ≥ 90%（pytest-cov 报告）
- [ ] `app/infra/models.py` 12 表映射通过 SQLAlchemy 自检（`Base.metadata.create_all` 在 SQLite 内存库不报错，校验 schema 一致性）
- [ ] `tests/fixtures/` 六类夹具齐全，四组验收样例可被 domain 纯函数直接消费（judge/断言/状态机 跑通预期结果）
- [ ] `EvidenceStore` Protocol + `FileEvidenceStore.save` 骨架可单测
- [ ] 全量 `uv run pytest -x` 通过；`uv run ruff check . && uv run black --check .` 通过
- [ ] dev-notes `agnes-20261010.md` 完整（每节点四要素）
- [ ] 演示命令（AGENT.md §5.2，D18 止于 DDL + 单测，不起 uvicorn）：`psql $ZHL_DB -f migrations/001_init.sql && uv run pytest tests/ -v` 可复现（含 DB 初始化 + 规则种子 + ISO 表种子导入脚本）
- [ ] 冻结动作完成（D20）：git tag `phase0-frozen` 已打，dev-notes 记「计划评审通过」

## 7. 已确认决策与留待 builder 的项

已确认（D16–D20）：12 表口径 / EXCLUDE 不降级 / 演示止于 DDL+pytest / alembic 仅初始化 / 冻结打 tag。详见 §4。

留待 builder（不改变交付物，plan 阶段可定）：
- 触发器具体 SQL 写法（含 `sn_num` 插入触发器、published 拦截触发器的实现细节）
- `btree_gist` 扩展在 001_init.sql 头部 `CREATE EXTENSION IF NOT EXISTS btree_gist`
- 证据 `object_ref` 的 kind 枚举取值（*：`batch|inspection|incident|reinspection|rule|report|trace|sample`）
- EXCLUDE 验证失败时的应用层兜底检查位置（*：`app/infra/repositories.py` 插入前检查，Phase 0 提供接口签名，Phase A 填充实现）

## 8. 与 AGENT.md 的对应

| AGENT.md 节点 | 本 Phase 落点 |
| --- | --- |
| §1 brainstorm 定稿 | 本文档提交 + 人工评审通过 |
| §1 计划评审 | 后续 writing-plans 产出 plan，人工批准 |
| §3.3 domain 测试 | §5.5 夹具 + §6 覆盖率 90% |
| §4.1 DDL 独立执行 | §5.1 + §6 第 1 条 |
| §5 交付 | §6 完成标准 |
| §6.1 冻结项 | `app/domain/`、`app/infra/models.py`、`migrations/`、`tests/fixtures/` 本 Phase 定稿后冻结 |

## 9. 已知风险

| 风险 | 缓解 |
| --- | --- |
| EXCLUDE 约束 SN 跨批次语义易写错 | 不降级（D17）：psql 验证失败时保留 DDL，加应用层兜底检查（`module_inspections` 插入前查同 SN 跨批次）并写入 001_init.sql 注释，交付物不变 |
| 触发器与 alembic 版本化冲突 | D19：001_init.sql 手写全部触发器/EXCLUDE/权限语句；alembic 仅初始化配置不生成 revision；Phase A/B 增量 DDL 走 alembic |
| 覆盖率 90% 目标高 | domain 纯函数本身小，分支有限；边界值用例齐全即可达成 |
| 冻结后 Phase A/B 仍需微调契约 | D20：打 tag `phase0-frozen` 后一律走 PR + review，dev-notes 记评审结论 |
