# 质慧链 · 全链路连通测试用例（逐步操作 + 完整数据）

日期：2026-10-15
定位：**基于现有实现的逐步验收脚本**。每条链路给出「点哪里 → 填什么（逐字可粘贴）→ 应看到什么反馈 → 终态是什么」。
所有数据取自实现里真实存在的规则、ISO 表与判定引擎，**每个数字都是在跑起来的系统上实测出来的**，不是设计意图的转述。

姊妹文档：`02-targeted-test-data.md`（纯函数/守卫的「正常·边界·失败」输入→输出矩阵）。

---

## 0. 环境准备（照抄即可）

### 0.1 启动

```bash
# 1) Python 环境
uv sync

# 2) PostgreSQL 15（Docker）
.\scripts\start-pg.ps1              # 拉镜像 + 起容器 + 跑 001_init.sql + verify_001.sql

# 3) 灌入一条 published 规则（幂等，可重复执行）
.\scripts\seed-demo.ps1             # 末尾打印 seederule= <rule_version_id>

# 4) 起服务
uv run uvicorn app.main:app --reload --port 8000
```

浏览器打开 **http://localhost:8000** （`app/static/workbench.html`）。

### 0.2 两种模型模式（务必先确定用哪种）

| 模式 | 怎么开 | 抽取置信度 | 速度 | 用途 |
| --- | --- | --- | --- | --- |
| **真模型**（默认） | `.env` 里配 `ZHL_LLM_API_KEY` / `ZHL_LLM_BASE_URL` / `ZHL_LLM_MODEL`，不设 `ZHL_LLM_MODE` | 模型自己给，**每次不一样** | 单次 40~90 秒 | 答辩演示「系统真在调模型」 |
| **Mock（确定性）** | 环境变量 `ZHL_LLM_MODE=mock`，或在 `.env` 追加一行 `ZHL_LLM_MODE=mock` | 由 SN 哈希决定，**可复算**（见 §0.3） | 毫秒级 | 回归测试、可复现验收 |

> **本文件所有写死的置信度数字都按 Mock 模式给出。** 真模型下置信度不可预测，`confidence >= 0.8` 与 `< 0.8` 的分支要现场看返回的 `confidence` 字段，而不是照抄本文档。
> **AI 归因接口会现场调模型。** 真模型下单次要等约 1 分钟，界面已做「AI 归因中…」提示与 120s 超时；Mock 下秒回。

### 0.3 Mock 模式下的置信度规则（可自行复算）

实现位置 `app/plugins/llm_client.py::_synthetic_confidence`：

- SN **以 `SN-LOW` 开头** → 固定 **0.62**（低于 0.8 阈值，必然走人工确认）
- 其他 SN → `0.85 ~ 0.999` 之间的稳定值（同一 SN 每次一样）

实测样本（Mock 模式）：

| SN | 置信度 | 是否触发人工确认 |
| --- | --- | --- |
| `SN-LOW-001` | **0.620** | ✅ 触发（`low_conf_sns`） |
| `SN-N-001` | 0.891 | ❌ 自动确认 |
| `SN-N-002` | 0.884 | ❌ 自动确认 |
| `SN-N-007` | 0.869 | ❌ 自动确认 |
| `SN-DEMO-001` | 0.948 | ❌ 自动确认 |
| `SN-I-OODB` | 0.944 | ❌ 自动确认 |

**为什么只有结构化通道能演示低置信**：走「原始单据（AI 抽取）」通道时，逐行置信度由 `app/plugins/raw_source.py` 固定给 **0.9**，批次置信度因此恒为 0.9。**要演示低置信人工确认，必须选「已结构化数据（逐列粘贴）」并把 SN 写成 `SN-LOW-001`。**

---

## 1. 链路总览

批次（10 态机，`app/domain/state_machine.py` 实测边表）：

```
draft → pending_intake → pending_judge ─全部 Pass+抽样 Pass─→ pending_release → released → archived
                                      └─任一 Fail / 抽样 Fail / 规则缺失─→ isolated
                                                                              ↓
                                                        disposing → pending_recheck
                                                                     ├─复检通过+关闭─→ pending_release
                                                                     └─复检失败──────→ rejected → archived
任意非归档状态 ──人工拒绝──→ rejected → archived
```

异常（7 态机）：

```
new → isolated → assigned → pending_recheck → closed
                                    └──→ rejected
              isolated / assigned ──→ scrapped
```

**批次-异常联动（实测）**：`close` 只在**该批次全部异常都处于终态**时才把批次推到 `pending_release`；否则批次停在 `pending_recheck`。

**闭环动作都发生在当前节点（2026-10-15 起）**：复检通过 → 后端自动置位「整改措施已验证」→ **在复检节点直接点「确认关闭异常」**。链路只往后推，不回异常处置节点补勾。

| # | 链路 | 触发数据 | 批次终态 | 异常终态 |
| --- | --- | --- | --- | --- |
| C1 | 正常放行 | 10 SN 全 Pass | `archived` | 无 |
| C2 | 功率边界 Pass | `pmax = 533.5` | `pending_release` | 无 |
| C3 | 功率边界 Fail / 越档 Fail | `pmax = 533.4`；`im = 16.5` | `isolated` | `new`（自动） |
| C4 | 完整闭环：归因→复检→关闭→放行 | `im = 16.5` 越档 | `archived` | `closed` |
| C5 | 复检失败 → 拒绝 | 复检 `result=fail` | `archived`（经 `rejected`） | `rejected` |
| C6 | 关闭条件缺失与就地补齐 | 逐项不满足 | 不变 | 不变 |
| C7 | 多异常批次联动 | 同批 2 只异常 | 最后一只关闭后才 `pending_release` | `closed`×2 |
| C8 | 归档只读 | 已 `archived` | `archived` | — |
| C9 | 跨批同 SN 一致性 | 两批同 SN | 第二批 `pending_intake` | `data_consistency` / critical |
| C10 | 人工拒绝 | 任意非归档批次 | `archived` | — |
| C11 | 归因三选一 | 已隔离批次 | 不变 | 不变 |
| C12 | 低置信人工确认 | `SN-LOW-001` | `pending_intake` → 确认后推进 | — |
| C13 | 复检结论重算 `/recheck` | 历史批次 | `pending_release` | — |

---

## 2. 共用种子数据

### 2.1 规则版本

工作台「规则库」tab 里必须有一条**已发布**的规则，否则「新建批次」的规则下拉是空的、提交按钮禁用。用 `scripts/seed-demo.ps1` 灌入后应为：

| 字段 | 值 |
| --- | --- |
| `rule_version_id` | `11111111-1111-1111-1111-111111111111` |
| 名称 | `JH-550W 演示规则（published）` |
| 项目 / 型号 | `P-DEFAULT` / `JH-550W` |
| 铭牌功率 | `550.0` W |
| 功率容差 | `0.03`（±3%） |
| 状态 | `published` |

参数标准（`parameter_standards`）：

| 参数 | 标准值 | 容差 | 闭区间（= 标准值 ×(1±容差)） | 实测边界 Pass |
| --- | --- | --- | --- | --- |
| `pmax` (W) | 550.0 | ±3% | **[533.5, 566.5]** | 533.5 ✅ / 566.5 ✅ |
| `temp` (°C) | 25.0 | ±0.10 | [22.5, 27.5] | 22.5 ✅ / 27.5 ✅ |
| `isc` (A) | 10.5 | ±0.05 | [9.975, 11.025] | — |
| `voc` (V) | 48.0 | ±0.05 | [45.6, 50.4] | — |
| `vm` (V) | 40.0 | ±0.05 | [38.0, 42.0] | — |
| `im` (A) | 13.0 | ±0.05 | [12.35, 13.65] | ⚠️ 见下 |
| `ff` | 0.80 | ±0.02 | [0.784, 0.816] | 0.784 ✅ / 0.816 ✅ |

> **⚠️ 电流分档与容差是两把独立的尺子（最容易踩的坑）**：`im` 先被容差校验（必须落在 `[12.35, 13.65]`），再被电流分档校验（必须落在 `[12.0,14.0]=A` 或 `[14.0,15.5]=B`）。**两条都必须满足**。所以：
> - `im = 12.0`（A 档下界）→ **Fail**（容差不过，实测已确认）
> - `im = 14.0`（A/B 交界）→ **Fail**（容差不过）
> - `im = 15.5`（B 档上界）→ **Fail**（容差不过）
> - `im = 13.0`（标准值）→ Pass
>
> **要演示「越档」请用 `im = 16.5`**：它同时违反容差（>13.65）与分档（不在任何区间），`current_band` 检查项会明确 Fail。

电流分档定义（`current_bands`）：

| 档位 | 区间 |
| --- | --- |
| A | `[12.0, 14.0]` |
| B | `[14.0, 15.5]` |

抽样方案（`sample_plan`）：`{"inspection_type":"iv","inspection_level":"normal","aql":1.0}`

### 2.2 ISO 抽样表（`migrations/001_init.sql` 种子，`iv` / `normal` / `aql=1.0`）

**`lot_size` 取的是「计划数量」，不是「送检行数」。**

| 计划数量区间 | n | ac | re |
| --- | --- | --- | --- |
| 2 – 8 | 8 | 0 | 1 |
| 9 – 15 | 13 | 0 | 1 |
| 16 – 25 | 20 | 1 | 2 |
| 26 – 50 | 32 | 1 | 2 |
| 51 – 90 | 50 | 2 | 3 |
| 91 – 150 | 80 | 3 | 4 |
| **1** | **查不到 → `rule_missing`** | — | — |
| **151** | **查不到 → `rule_missing`** | — | — |

> **⚠️ 计划数量必须 ≥ 2。** 实测：计划数量 `=1` 时 ISO 表查不到（`lot_size_min` 最小是 2），判定结果虽是 `Pass`、但抽样结论为 `rule_missing`，批次照样 `isolated` 并自动建 `rule_missing` 异常。**每条链路建批次时计划数量请填 ≥ 2。**

### 2.3 送检数据（9 列：`SN,pmax,temp,isc,voc,vm,im,ff,外观`）

> ### ⚠️ 最关键的一条：判定读的是**你提交的列**，不是「AI 抽取结果」里显示的值
>
> 判定引擎（`app/domain/judge.py`）读的是 `module_inspections` 表的**原始列**。而「入场结果」面板里「AI 抽取结果（逐组件）」显示的是**模型的输出**，两者是**两套数**，实测对照如下：
>
> | SN | 提交的 9 列 `pmax` | 面板「AI 抽取结果」显示 | DB 列 `pmax` | 最终判定 |
> | --- | --- | --- | --- | --- |
> | `SN-N-001` | 533.5 | **547.3**（Mock 合成值） | 533.5 | **合格** |
> | `SN-N-002` | 533.4 | **551.94**（Mock 合成值） | 533.4 | **不合格** |
>
> 也就是说：**要演示什么判定结果，就改提交的那一列**；面板上的抽取值是模型的产物，与判定无关。（真模型下它也是模型的产物，同样与判定无关。）看到面板数字和判定对不上时，这不是 bug。

前端「检测数据」框每行 9 列，缺列或非数字会被前端直接拦下（后端再校验一次，返回 400 `incomplete_inspection`）。

> **两条录入通道等价**（2026-10-15 起）：手工粘贴，或「数据入场」→「**从 CSV 导入**」选文件。
> 导入通道在前端解析并**先给预览**（有效行数 / SN 列表 / 逐行问题），确认后再提交，
> 走的是同一个 `inspections[]` 契约。文件要求：9 列同上，表头行可留（形如 `SN,pmax,…` 会被跳过），
> 编码 UTF-8 或 GBK 均可；**文件内有重复 SN 会在预览里拦下**（后端只查"已入库的同批 SN"，
> 载荷内重复会撞唯一约束）。弹窗内提供「下载模板」（`/static/templates/inspection_template.csv`，带 BOM，Excel 双击不乱码）。
>
> **现成的三组 CSV 测试数据**（`docs/test/csv/`，UTF-8 带 BOM，可直接在导入弹窗里选）：
> `csv-01-normal.csv`（5 行全合格）／`csv-02-boundary.csv`（9 行恰在闭区间端点→应合格，5 行各越界一步→应不合格）／
> `csv-03-errors.csv`（4 类格式错误，预览应拦下且**不可提交**）。逐行预期见 §附录「CSV 测试数据」。
>
> **行数与计划数量不一致会提示，但不拦截**：批次「计划数量」是**抽样方案的 lot size 输入**
> （`main_chain._lot_size` 拿它查 ISO 表 → `n/ac/re`），而 CSV 行数才是实际入库并被判定的组件数——
> 两者不校验、不比对。选完文件后若两者不等，预览会显示「计划数量不一致」黄色提示（写明两边数字），
> **仍可提交**（不改判定语义）。填计划数量前请确认它 = 本批实际送检的组件数。

默认列（除被改变的那一列外，全部取标准值）：`temp=25.0, isc=10.5, voc=48.0, vm=40.0, im=13.0, ff=0.80, appearance=pass`

| 用途 | SN | pmax | im | 外观 |
| --- | --- | --- | --- | --- |
| C1 正常 | `SN-N-001` … `SN-N-010` | 550.0 | 13.0 | pass |
| C2 边界 Pass | `SN-B-LOW` | **533.5** | 13.0 | pass |
| C3 边界 Fail（越下限） | `SN-B-BELOW` | **533.4** | 13.0 | pass |
| C3b 越档 Fail | `SN-I-OODB` | 550.0 | **16.5** | pass |
| C4/C5 闭环 | `SN-I-OK` / `SN-I-OODB` | 550.0 | 13.0 / **16.5** | pass |
| C9 跨批冲突 | `SN-SHARED` | 550.0 | 13.0 | pass |
| C12 低置信 | `SN-LOW-001` | 550.0 | 13.0 | pass |

可直接粘贴的完整行（C1 用后 10 行）：

```
SN-N-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-N-002,548.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-N-003,552.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-N-004,550.0,25.0,10.5,48.0,40.0,13.5,0.80,pass
SN-N-005,549.0,25.0,10.5,48.0,40.0,12.5,0.80,pass
SN-N-006,551.0,25.0,10.5,48.0,40.0,13.0,0.81,pass
SN-N-007,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-N-008,547.5,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-N-009,553.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-N-010,550.0,25.0,10.5,48.0,40.0,13.0,0.79,pass
```

其余链路单行：

```
SN-B-LOW,533.5,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-B-BELOW,533.4,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-I-OODB,550.0,25.0,10.5,48.0,40.0,16.5,0.80,pass
SN-SHARED,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-LOW-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
```

**原始单据通道**（C1' 演示 AI 抽取与 FR-1.4 校验拦截）可粘贴：

```
# 设备 UT-2200 导出 / 2026-06-01
SN,pmax,temp,isc,voc,vm,im,ff,appearance
SN-DEMO-001,551.2,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-DEMO-002,549.8,25.0,10.5,48.0,40.0,13.0,0.79,pass
SN-DEMO-003,470.0,25.0,10.5,47.2,40.0,12.4,0.74,fail
SN-DEMO-004,548500.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
```

实测结果：解析出 **3 行有效**，第 4 行被 FR-1.4 校验拦截（`pmax=548500.0` 超出合理范围 `[1.0, 1000.0]`）。`SN-DEMO-003` 功率 470.0 W（< 533.5）且外观 `fail` → 判定 Fail → 批次隔离 + 自动建 `rule_fail` 异常。

---

## 3. 逐步操作流程

### C1 · 正常放行（10 SN 全 Pass）

**目标终态**：批次 `archived`，无异常，报告 v1 已生成。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 左栏「＋ 新建批次」 | 批次号 `B-20261015-001`<br>项目 `P-DEFAULT`<br>型号 `JH-550W`<br>计划数量 `10`<br>规则版本 = 下拉选 `JH-550W 演示规则（published） v1（JH-550W）` | 批次出现在左栏「批次」tab，状态 **待入场**，toast「批次已创建，进入数据入场」 |
| — | *（可选前置）* 左栏「规则库」tab 检查规则是否已发布 | — | 卡的 `JH-550W 演示规则 v1` 状态应为 **已发布**。若是「草稿」，点该卡的「**发布**」→「确认发布」，操作人随意填。**下拉里只列已发布且已生效的规则**，没发布就建不了批次 |
| 2 | 中栏流水线节点「数据入场」→ 点「提交送检数据」 | 数据来源 = `检测数据`<br>录入方式 = **`已结构化数据（逐列粘贴）`**<br>检测数据框粘贴 §2.3 的 10 行 | 弹窗关闭，toast「已自动确认入场，批次转待判定，下一步：查看判定」 |
| 3 | 观察「入场结果」面板 | — | 「已自动确认入场 · 置信度 **85%**」（Mock 模式实测值 = 10 个 SN 里**最低**的那个；真模型看实际值）<br>「AI 抽取结果（逐组件）」逐行列出 10 个 SN 与各自置信度：0.851 ~ 0.958<br>**该卡片跟随批次刷新持续可见**（直到确认/拒绝入场或离开该批次） |
| 4 | 流水线节点「判定」 | 点「执行判定」 | 逐组件明细表：10 行「合格」；抽样结论 **合格**（n=13 / ac=0 / re=1） |
| 5 | 流水线节点「放行」 | 面板自动跑**只读预检**（7 项逐条 ✓/✗）；点「确认放行」→ 批准人填 `ZHL-ENG` → 提交 | 预检 7 项全 ✓ 时显示「前置校验全部通过，可确认放行」；确认后批次 **已放行**，toast「已放行，批次进入已放行」，**左右两栏状态同步刷新** |
| 6 | 批次头「生成报告」 | 报告类型 = `放行报告` | 生成 **v1** 放行报告，批次自动转 **已归档** |
| 7 | 左栏「报告」tab → 点刚生成的报告 | — | 报告正文含：批次信息、判定统计（`decision_counts: {"pass": 10}`）、抽样结论、**7 项放行断言逐条 [PASS]**、放行结论「**通过**」 |

**实测留痕**（走完后 `agent_task_traces` 应有）：

```
judge   / pass
release / pass
report  / pass
report_api / pass
```

证据类型（`evidence.evidence_type`）应有：
`source_file`、`extraction_result`（每 SN 一条，共 10 条）、`human_confirmation`、`decision_snapshot`、`task_trace`、`report_snapshot`。

> **注意**：报告节的「留痕引用 → 轨迹（agent_task_traces）／证据（evidence）」实测显示「（无轨迹引用）」。报告渲染层拿到了空的 `trace_refs` / `evidence_refs`；轨迹与证据本身都真实存在（见上），是报告正文里的引用列表没接上。这不影响放行结论与断言结果的正确性（它们引用的是真实数据）。

---

### C2 · 功率边界 Pass（`pmax = 533.5`）

**目标终态**：批次 `pending_release`，`SN-B-LOW` 判定 **合格**。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 「＋ 新建批次」 | 批次号 `B-20261015-002`<br>项目 `P-DEFAULT` / 型号 `JH-550W`<br>**计划数量 `8`**<br>规则版本同上 | 状态 **待入场** |
| 2 | 「数据入场」→「提交送检数据」 | 录入方式 = `已结构化数据`<br>粘贴 `SN-B-LOW,533.5,25.0,10.5,48.0,40.0,13.0,0.80,pass` | 自动确认入场 |
| 3 | 「判定」节点 | 点「执行判定」 | `SN-B-LOW` **合格**；批次 **待放行** |

实测确认：`pmax=533.5`（= 下限）判 **Pass**，闭区间语义成立。

> **计划数量必须 ≥ 2**：实测计划数量填 `1` 时，即使判定 Pass，抽样仍报 `rule_missing`（ISO 表最小 lot 区间是 2–8），批次照样隔离。

---

### C3 · 功率边界 Fail（`pmax = 533.4`）

**目标终态**：批次 `isolated`，自动建 `rule_fail` / `major` / `new` 异常。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 「＋ 新建批次」 | 批次号 `B-20261015-003`，计划数量 `8`，其余同 C2 | 待入场 |
| 2 | 「数据入场」→「提交送检数据」 | 粘贴 `SN-B-BELOW,533.4,25.0,10.5,48.0,40.0,13.0,0.80,pass` | 自动确认入场 |
| 3 | 「判定」节点 | 点「执行判定」 | `SN-B-BELOW` **不合格**；批次 **已隔离** |
| 4 | 左栏「异常」tab | — | 自动出现 1 条异常：`规则失败` · `较严重` · **新建**，带「自动」标记；受影响组件 `SN-B-BELOW` |
| 5 | 「判定」节点点失败行的「规则结果」列 | — | 展开判定证据：`power_range` → `input.pmax = 533.4`，`computed = {"lower": 533.5, "upper": 566.5}`，`result = "Fail"` |

**同一个引擎的上边界对照组**：`pmax = 566.6` → 同样 `power_range` Fail，`computed = {"lower": 533.5, "upper": 566.5}`。

---

### C3b · 电流越档 Fail（`im = 16.5`）

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 「＋ 新建批次」 | 批次号 `B-20261015-003B`，计划数量 `8` | 待入场 |
| 2 | 「数据入场」→「提交送检数据」 | 粘贴 `SN-I-OODB,550.0,25.0,10.5,48.0,40.0,16.5,0.80,pass` | 自动确认入场 |
| 3 | 「判定」节点 | 点「执行判定」 | `SN-I-OODB` **不合格**；批次 **已隔离** |

失败项是 **两个**：`parameter_tolerances`（`im=16.5` 超出 `[12.35, 13.65]`）与 `current_band`（不在任何声明档位）。这是 C4 闭环的入口数据。

---

### C4 · 完整闭环：归因 → 指派 → 复检 → 关闭 → 放行（**核心链路**）

**目标终态**：批次 `archived`，异常 `closed`，抽样结论出现**第 2 轮 pass**，报告 v1 已生成。

前置：先跑完 C3b，得到 `isolated` 批次 + `new` 异常。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 左栏「异常」tab → 该异常卡片 → 点「**隔离异常**」 | 无 | 异常状态 → **已隔离**；toast「已隔离该异常」 |
| 2 | 同卡片 → 点「**AI 归因**」 | 无 | 按钮位置变成「AI 归因中，正在调用模型，请稍候（约 1 分钟）…」（真模型需等约 1 分钟；Mock 秒回）<br>随后弹出「AI 归因 · xxxxxxxx」窗，列出**带单选按钮**的假设列表 |
| 3 | 在归因弹窗里选一条假设 | 点第 1 条的单选按钮 | 底部文本框自动填入这条假设的原文 |
| 4 | 点主按钮「**采纳选中假设**」 | 无 | 该弹窗关闭，卡片上出现「已采纳假设 #1：…」；toast「已记录归因决定：采纳」<br>卡片「确认根因」一行显示被采纳的假设原文 |
| 5 | 同卡片 → 点「**指派责任人**」 | 责任人 `ZHL-QA`<br>处置期限 `2026-06-10T17:00`（任意今天之后的）<br>临时措施 `隔离该组件并暂停该工位投料`（**建议此时就填**：它是关闭条件之一，虽然指派时选填、可在复检节点补填，但先填省一步） | 异常 → **已指派**；批次 → **处置中** |
| 6 | 同卡片 → 点「**填报整改措施**」 | 确认根因 `电芯失配致 Impp 越档`（若第 4 步已采纳，这里已预填）<br>纠正措施 `更换主电芯并复测`<br>临时措施（若第 5 步已填则预填） | 异常 → **待复检**；批次 → **待复检**<br>表单里**没有**「已验证」勾选项：此时还没有复检记录，该标记由复检通过自动置位（见第 7 步） |
| 7 | 流水线节点「**复检**」→ 点该异常的「录入复检」 | 复检组件 = `SN-I-OODB`<br>结果 = `通过`<br>Pmax `550.0` / Temp `25.0` / Isc `10.5` / Voc `48.0` / Vm `40.0` / Im `13.0` / Ff `0.80`<br>执行人 `ZHL-QA`<br>执行时间 `2026-06-02T10:00`<br>范围 = `最小集` | toast「**复检通过，整改措施已标记为已验证，请确认关闭异常**」<br>`scope_sns` 服务端自动填为该异常的全部受影响 SN（本例 `["SN-I-OODB"]`）<br>卡片头变为「**复检已录入 · 整改措施已验证**」 |
| 8 | **仍在复检节点**，点该卡片的「**确认关闭异常**」 | 无 | 异常 → **已关闭**；批次 → **待放行**；toast「异常已关闭，批次转待放行」；签署完这张卡从复检列表消失 |
| 10 | 流水线节点「**放行**」 | 点「确认放行」→ 批准人 `ZHL-ENG` | 7 项断言全 ✓ → 批次 **已放行** |
| 11 | 批次头「生成报告」 | 报告类型 `放行报告` | 报告 v1；批次 **已归档** |
| 12 | 「报告」tab → 点该报告 | — | 判定统计 `decision_counts: {"pass": 1, "fail": 1}`<br>抽样结论含**两轮**：`sample_round: 2 … "result": "pass"` 与 `sample_round: 1 … "result": "fail"`<br>7 项断言全 `[PASS]`，放行结论「**通过**」 |

**第 3 步的假设内容**（Mock 模式实测；真模型内容不同）：

```
#1  rank=1  confidence=0.82  evidence_ids=[2 条真实证据 id]
    cause = rule_fail：受影响批次存在同源偏差（基于 2 条证据）
    recommended_action = 隔离同批次待检组件并复核该证据涉及的工序
    impact_scope = {"affected_sns": 2, "cross_batch_hits": 0}

#2  confidence=0.55  evidence_ids=[]        ← 故意不带证据
    cause = rule_fail：设备参数漂移（待补充校准记录）
    recommended_action = 调取该设备校准记录复核
```

> **假设 #2 会被 D2 证据闸门丢弃**（`evidence_ids` 为空）。这是设计使然：归因结果里只保留引用了**真实存在**证据的假设。若所有假设都被闸门拦掉，弹窗显示「归因假设均未通过证据校验（D2），请人工填写根因」，主按钮变成「人工填写根因」。

**关键：三选一的行为分别是什么**（FR-3.6 / §11.3）

| 选项 | 点哪里 | 写库结果 | 轨迹留痕 |
| --- | --- | --- | --- |
| **采纳** | 选假设 → 主按钮「采纳选中假设」 | `confirmed_root_cause` = **后端存储的假设原文**（前端改不了它，后端按 `hypothesis_index` 取） | `root_cause_source = "hypothesis"`，带 `hypothesis_index` |
| **修改** | 选假设 → 次按钮「修改后采纳」→ 填「修订后的根因」→ 提交 | `confirmed_root_cause` = **人工修订文本** | `root_cause_source = "modified_hypothesis"`，同时留**原假设原文**与 `hypothesis_index` |
| **驳回** | 次按钮「驳回」 | **不写**任何业务字段 | `decision = "reject"` |

实测「修改」轨迹（`agent_task_traces.params_summary`）：

```json
{"decision": "modify", "cause": "现场确认：焊接虚焊（非隐裂）",
 "root_cause_source": "modified_hypothesis", "hypothesis_index": 0,
 "hypothesis_cause": "rule_fail：受影响批次存在同源偏差（基于 2 条证据）"}
```

**闭环后实测状态**：

| 观察点 | 值 |
| --- | --- |
| 批次 | `archived` |
| 异常 | `closed` |
| `sample_conclusions` | `sample_round=1 result=fail`、**`sample_round=2 result=pass`** |
| 轨迹序列 | `incident_auto` → `judge` → `incident_isolate` → `attribution` → `incident_attribute` → `incident_assign` → `incident_capa` → `reinspection` → `incident_close` → `release` → `report` → `report_api`（**只有一次 `incident_capa`**：复检通过后标记自动置位，不再需要回异常节点补勾） |
| 复检轨迹 | `reinspection` 的 `params_summary` 带 `"capa_verified": true`，用于事后分清该标记是「复检自动落位」还是「人工 `/capa` 声明」 |
| 关闭轨迹 | `incident_close` 的 `structured_output` 带 `remediation_conclusion: {"sample_round": 2, "sample_result": "pass", "written": true}` —— 第 2 轮结论就是在关闭这步落库的 |
| 7 项断言 | `state` / `sample_passed` / `no_open_critical_incident` / `no_isolated_sn` / `all_evidence_present` / `all_capa_verified` / `reinspection_scope_covered` 全部 `True` |

> **第 2 轮结论是闭环的关键。** 第 1 轮抽样是 `fail`，若没有人把「复检已通过并关闭」落成 `sample_round=2`，放行断言②（`sample_passed`）会读「最新一轮 = fail」而**永久阻断**该批次。关闭端点会在批次转入 `pending_release` 的同一事务里自动补写；对历史批次可用 C13 的 `/recheck` 手动补。

---

### C5 · 复检失败 → 拒绝

**目标终态**：批次 `archived`（经 `rejected`），异常 `rejected`。

前置：先按 C3b 得到 `isolated` 批次，再走 C4 的第 1–6 步（隔离 → 指派 → 填报整改措施）。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 「复检」节点 →「录入复检」 | 复检组件 `SN-I-OODB`<br>**结果 = `不通过`**<br>Pmax **`470.0`**（< 533.5，重演同一个不合格）<br>Temp `25.0` / Isc `10.5` / Voc `48.0` / Vm `40.0` / Im `13.0` / Ff `0.80`<br>执行人 `ZHL-QA`<br>执行时间 `2026-06-02T10:00`<br>范围 `最小集` | toast「**复检不通过，异常与批次转拒绝**」<br>异常 → **已拒绝**；批次 → **已拒绝** |
| 2 | 「异常」tab 看该卡片 | — | 状态 **已拒绝**；「确认关闭异常」不再出现 |
| 3 | 流水线节点「放行」→ 点「**拒绝本批**」 | 拒绝原因 `复检仍不合格，同批拒收`<br>操作人 `ZHL-QA` | toast「已拒绝并归档，异常处置报告已生成」<br>批次 → **已归档**；生成 **异常处置报告 v1** |

**实测确认的 409 守卫**：

| 操作 | 结果 |
| --- | --- |
| 复检失败后调 `POST /api/incidents/{id}/close` | `409 {"error": "incident_not_closeable", "status": "rejected"}` |
| 复检失败后调 `POST /api/batches/{id}/reject` | `409 {"error": "batch_reject_not_allowed", "from_status": "rejected", …}`（批次已是 `rejected`，不重复拒绝） |
| 批次 `rejected` 时取放行报告 | `409 {"error": "rejected_batch_only_incident_report", "report_type": "release"}`（D7：只能出处置报告） |

> **⚠️ 「复检失败 → 拒绝」不适用于从「已指派」直接复检**。异常合法边只有 `assigned → pending_recheck` 与 `pending_recheck → rejected`，**没有 `assigned → rejected`**。若跳过「填报整改措施」直接从「已指派」登记 `result=fail`，实测返回 **`409 {"error": "incident_transition_denied", "reason": "no transition incident:assigned -> rejected (skip/invalid)"}`**。必须先走完「填报整改措施」把异常推到 **待复检**，再登记复检。

---

### C6 · 关闭五条件逐项缺失（应逐条指名）

五条件是：① 已关联 SN ② 有原始判定证据 ③ 责任人 + 临时措施 + 确认根因 + 纠正措施 ④ 独立复检记录 ⑤ 复检通过且已验证。

前置：C3b 得到 `isolated` 批次 + `new` 异常（自动创建的异常已带 SN 关联与判定证据）。

| 步 | 当前状态 | 点「确认关闭异常」应看到 |
| --- | --- | --- |
| 1 | 异常 `new` | `409 {"error": "incident_not_closeable", "status": "new"}`（状态就不对，先隔离） |
| 2 | 点「隔离异常」后 `isolated` | `409 {"error": "incident_not_closeable", "status": "isolated"}`（先指派） |
| 3 | 指派后 `assigned`，未填 CAPA | `409 {"error": "incident_not_closeable", "status": "assigned"}`（先填报整改措施） |
| 4 | 填报整改措施后 `pending_recheck`，**临时措施为空** | `409 {"error": "close_conditions_unmet", "missing": ["containment_action"]}`（复检节点「补填整改措施」就地补齐） |
| 5 | 临时措施已填但**还没录复检** | ⚠️ **界面上没有关闭入口** —— 未录复检时复检卡片只给「录入复检」（关闭必然缺 `reinspection_record`）。此状态只能经 API 复现 |
| 6 | 录入通过的复检后 | 复检通过会自动置位「已验证」→ 直接 `200`，异常 → **已关闭**，批次 → **待放行** |

**实测确认**：指派时临时措施留空 → 录完通过的复检后 close 返回 `409 {"missing": ["containment_action"]}`；用复检卡片的「补填整改措施」补上后 close `200`，且**补填不会把已自动置位的 `capa_verified` 洗掉**（`verified` 字段缺省语义是「不改」）。

> **2026-10-15 起界面只保留可达成的前置条件**：未录复检时**不给**关闭入口，而「已验证」由复检通过自动置位——因此 `reinspection_record` 与 `capa_verified` 两行在界面上已不可复现，仍由 `tests/e2e/test_closure.py` 逐条覆盖。界面可复现的是 `capa_fields` / `containment_action` / `original_decision_evidence`，且前两项都能在**复检卡片**用「补填整改措施」就地补齐——被阻断不等于死路。

前端对 `missing` 项的中文映射（`app/static/wb/stages/incident.js::MISSING_CN`）：

| missing 代码 | 界面文案 |
| --- | --- |
| `owner` | 未指派责任人 |
| `sn_not_linked` | 异常未关联受影响组件 |
| `original_decision_evidence` | 缺少原始判定证据 |
| `containment_action` | 未填报临时措施 |
| `capa_fields` | 整改措施未填报完整（确认根因 / 纠正措施） |
| `reinspection_record` | 无独立复检记录 |
| `reinspection_passed` | 复检未通过 |
| `capa_verified` | 整改措施未勾选已验证 |

---

### C7 · 多异常批次联动

**目标终态**：关闭第 1 只后批次**仍**「待复检」；关闭第 2 只后批次才「待放行」。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 「＋ 新建批次」 | 批次号 `B-20261015-007`，计划数量 `8` | 待入场 |
| 2 | 「数据入场」→「提交送检数据」 | 粘贴两行：<br>`SN-N-001,533.4,25.0,10.5,48.0,40.0,13.0,0.80,pass`<br>`SN-N-002,550.0,25.0,10.5,48.0,40.0,16.5,0.80,pass` | 自动确认入场；判定后批次 **已隔离** |
| 3 | 「异常」tab → 点「＋ 再建一条异常」 | 触发原因 = `外观人工触发`<br>严重度 = `轻微`<br>受影响条码（每行一个）：`SN-N-001`<br>证据引用留空 | 本批出现 **2 只**异常（1 只系统自动建的 `规则失败`，1 只手工的 `外观人工触发`） |
| 4 | 对**第 1 只**异常依次：隔离 → 指派 → 填报整改措施 → 复检节点录入 `通过`（组件选该异常对应的 SN，Pmax/Temp/… 全填标准值）→ 复检节点点「确认关闭异常」 | 见 C4 第 1–8 步 | 关闭成功，但**批次仍是「待复检」**（还有一只没关） |
| 5 | 对**第 2 只**异常重复第 4 步 | 同上，组件选它自己对应的 SN | 关闭成功，**批次转「待放行」** |

实测确认：关闭第 1 只 → `incident_status=closed` 但 `batch_status=pending_recheck`；关闭第 2 只（最后一只）→ `batch_status=pending_release`。

> 该联动有自动化用例钉住：`tests/e2e/test_closure.py::test_close_gates_on_all_incidents_terminal`。

---

### C8 · 归档只读

前置：任意一条走完的链路（批次已是 `archived`）。

| 步 | 点击哪里 | 应看到什么 |
| --- | --- | --- |
| 1 | 批次头「生成报告」 | toast「批次已归档，内容只读」，接口 `409 {"error": "batch archived, terminal state"}` |
| 2 | 放行节点「确认放行」 | 同上 409 |
| 3 | 放行节点「拒绝本批」 | 同上 409 |
| 4 | 「异常」卡片上的任何写操作（隔离/指派/填报/关闭） | 同上 409 |
| 5 | 「数据入场」再提交送检数据 | 同上 409 |

实测六个写端点（`report` / `release` / `reject` / `recheck` / `intake` / `confirm`）全部返回 `409 {"error": "batch archived, terminal state"}`。

---

### C9 · 跨批同 SN 一致性

**目标终态**：第二批被 400 拒绝且**保持待入场**；一致性异常挂在**第一批**上。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 「＋ 新建批次」 | 批次号 `B-20261015-009A`，计划数量 `8` | 待入场 |
| 2 | 「数据入场」→「提交送检数据」 | 粘贴 `SN-SHARED,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass` | 自动确认入场；批次推进到 **待放行** |
| 3 | 「＋ 新建批次」 | 批次号 `B-20261015-009B`，计划数量 `8` | 待入场 |
| 4 | 「数据入场」→「提交送检数据」 | 粘贴**同样一行** `SN-SHARED,…` | 红色错误：toast「**组件条码已存在于其他批次**」；批次**仍是待入场** |
| 5 | 左栏「异常」tab | — | 新增 1 条异常：`数据一致性` · **严重** · **新建**，挂在**批次 A** 上 |

实测返回：`400 {"detail": {"reason": "cross_batch_sn_conflict", "error": "SN 'SN-SHARED' already recorded in a different batch"}}`；批次 A 保持 `pending_release`，批次 B 保持 `pending_intake`；自动异常 `('data_consistency', 'critical', 'new')` 挂在 A 的 `batch_id` 上。

---

### C10 · 人工拒绝

**目标终态**：批次 `archived`（经 `rejected`），自动生成异常处置报告。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 建一个落在 `isolated` 的批次（同 C3，`pmax=533.4`） | 批次号 `B-20261015-010`，计划数量 `8` | 已隔离 |
| 2 | 「放行」节点 → 点「**拒绝本批**」 | 拒绝原因 `同批组件存在系统性功率偏差，本批拒收`<br>操作人 `ZHL-QA` | toast「已拒绝并归档，异常处置报告已生成」；批次 → **已归档** |
| 3 | 左栏「报告」tab | — | 出现 1 份 **异常处置报告 v1** |

实测返回：`200 {"status": "archived", "auto_archived": true, "reason": "同批组件存在系统性功率偏差，本批拒收", "report_id": "<uuid>"}`，同事务内生成 `report_type = incident` v1 并自动归档。

---

### C11 · 归因三选一（单独验证，不进入后续流程）

前置：`isolated` 批次 + 已隔离的异常（同 C4 第 1 步）。

| 步 | 操作 | 应看到什么 |
| --- | --- | --- |
| 1 | 点「AI 归因」 | 假设列表带单选按钮，每条显示「置信度 xx% · 证据 n 条」 |
| 2 | 选第 1 条 → 点「**采纳选中假设**」 | 「已采纳假设 #1：<假设原文>」；「确认根因」一行 = 假设原文 |
| 3 | 重新点「AI 归因」→ 选第 1 条 → 点「**修改后采纳**」 | 弹出「修改假设」窗，**底稿是这条假设的原文**；填「修订后的根因」`现场确认：焊接虚焊（非隐裂）`，可另填「修改理由」；提交后「确认根因」= 修订文本 |
| 4 | 重新点「AI 归因」→ 点「**驳回**」 | 「已驳回全部假设，需人工填写根因后才能关闭异常」；「确认根因」**保持原值不变**（驳回不写业务字段） |

实测「修改」后的 `confirmed_root_cause` = `现场确认：焊接虚焊（非隐裂）`，轨迹 `root_cause_source = "modified_hypothesis"`，同时留原假设原文与下标 `0`。

**未先归因就直接点「采纳」**：若该异常没有停驻的归因运行，接口返回 `409 {"error": "no_parked_attribution", …}`，界面 toast「状态冲突，请刷新后重试」。

---

### C12 · 低置信人工确认（**必须选「已结构化数据」通道**）

**目标终态**：批次停在 `pending_intake` 等人工确认；确认后推进，拒绝则保持待入场。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 「＋ 新建批次」 | 批次号 `B-20261015-012`，计划数量 `8` | 待入场 |
| 2 | 「数据入场」→「提交送检数据」 | 数据来源 `检测数据`<br>录入方式 = **`已结构化数据（逐列粘贴）`**<br>粘贴两行：<br>`SN-LOW-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass`<br>`SN-N-002,548.0,25.0,10.5,48.0,40.0,13.0,0.80,pass` | toast「批次待处理：待人工确认入场→判定」；「入场结果」面板显示「**待人工确认 · 置信度 62%**」与「**低置信组件（1）：SN-LOW-001**」；批次仍是 **待入场** |
| 3 | 面板「**确认入场**」 | 无 | toast「已确认入场」；批次推进到 **待放行**（本批全 Pass，计划数量 8） |
| 4 | *（另起一批，验证拒绝分支）* 同第 1–2 步建 `B-20261015-012R`，粘贴 `SN-LOW-001,…` | 点「**拒绝入场**」 | toast「已拒绝入场，批次仍待入场」；**批次保持待入场**，逐 SN 入场状态 = **`rejected`** |

实测：Mock 下 `SN-LOW-001` 置信度 **0.62**，接口返回 `status=pending`、`low_conf_sns=["SN-LOW-001"]`；确认后批次直接到 `pending_release`（全 Pass）。拒绝后批次停在 `pending_intake`，该 SN 的 `intake_status` 变为 `rejected`。

**未确认就点「执行判定」**：`400 {"detail": {"unconfirmed_sns": ["SN-LOW-001", "SN-N-002"]}}`，界面提示「请先完成入场确认」。

---

### C13 · 复检结论重算（`/recheck`，历史批次补写）

**用途**：修复前就已关闭完异常、但结论侧没回写第 2 轮的批次（表现：批次停在 `pending_release`，但放行断言② `sample_passed` 一直失败）。

| 步 | 点击哪里 | 输入什么 | 应看到什么 |
| --- | --- | --- | --- |
| 1 | 流水线节点「放行」 | 无（面板自动跑校验） | 7 项里 `抽样结论通过` 显示 ✗；下方出现按钮「**重算复检结论**」 |
| 2 | 点「**重算复检结论**」 | 无 | 闭环成立 → toast「已补写第 2 轮抽样结论（pass）」并自动重跑校验，7 项转全 ✓<br>闭环未走完 → toast「复检闭环未走完：<未解除的 SN 原因>」<br>本批本就没有抽样结论 → toast「该批次没有抽样结论，无需重算」 |
| 3 | 再点一次 | 无 | toast「第 2 轮抽样结论已在，无需重算」（幂等） |

实测幂等返回：`{"sample_round": 2, "result": "pass", "written": false, "skipped": "already_passed"}`（第 2 轮结论已存在）。

**前置条件**：批次必须是 `pending_release`。其他状态下接口返回 `409 {"error": "batch_not_pending_release", "status": "..."}`；已归档则返回 `409 {"error": "batch archived, terminal state"}`。

---

## 4. 走查顺序

每条链路**独立建批次**，互不污染。建议顺序：

```
C1  正常放行            （先跑通，确认环境 OK）
C2  边界 Pass  vs  C3/C3b 边界 Fail / 越档 Fail   （闭区间语义 + 隔离自动建异常）
C12 低置信人工确认      （auto-confirm 阈值的另一侧）
C4  完整闭环            （核心：归因三选一 → 复检 → 第 2 轮结论 → 放行）
C5  复检失败拒绝  vs  C6 关闭五条件缺失
C7  多异常联动
C8  归档只读  →  C9 跨批冲突  →  C10 人工拒绝
C11 归因三选一（可并入 C4）
C13 /recheck            （对历史批次）
```

自动化回归（与本文件覆盖同一批路径）：

```bash
# 全量
uv run pytest

# 四组验收样例 + 端到端
uv run pytest tests/e2e/ tests/api/test_e2e_acceptance.py -v

# 本文件的链路 ↔ 用例对应
uv run pytest tests/api/test_reinspections.py tests/api/test_incident_attribute.py -v
uv run pytest tests/e2e/test_closure.py tests/e2e/test_demo_path.py -v
uv run pytest tests/api/test_remediation_conclusion_flow.py tests/static/test_wb_release_recheck.js -v

# 查批次状态
docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl \
  -c "SELECT batch_no, status FROM delivery_batches ORDER BY created_at DESC;"
```

---

## 5. 链路 ↔ 夹具/用例对应

| 链路 | 夹具 | 自动化用例 |
| --- | --- | --- |
| C1 | `acceptance_normal.json` | `tests/api/test_e2e_acceptance.py`（样例一） |
| C2 / C3 / C3b | `acceptance_boundary.json` | `tests/domain/test_judge.py`、`test_e2e_acceptance.py`（样例二） |
| C4 | `acceptance_incident.json` | `tests/e2e/test_sample3_incident_closure.py`、`test_closure.py` |
| C5 | `acceptance_incident.json` | `tests/e2e/test_closure.py::test_reinspection_fail_then_close_is_rejected` |
| C6 | `acceptance_incident.json` | `test_closure.py::test_close_names_all_missing_conditions_at_once` |
| C7 | `acceptance_incident.json` | `test_closure.py::test_close_gates_on_all_incidents_terminal` |
| C8 | — | `tests/api/test_archival.py` |
| C9 | `acceptance_consistency.json` | `tests/e2e/test_sample4_consistency.py` |
| C10 | — | `tests/api/test_release_reject.py` |
| C11 | `mock_llm_attribution.json` | `tests/api/test_incident_attribute.py` |
| C12 | `mock_llm_outputs.json` | `tests/api/test_intake.py` |
| C13 | — | `tests/api/test_remediation_conclusion_flow.py`、`tests/skills/test_remediation_conclusion.py` |
| 全链演示（原始单据入口） | — | `tests/e2e/test_demo_path.py` |

---

## 6. 已知限制与易踩坑（实测确认）

| # | 现象 | 原因 | 规避方式 |
| --- | --- | --- | --- |
| 1 | 计划数量填 `1` 时批次被隔离，异常类型是「规则缺失」而非「规则失败」 | ISO 表最小 lot 区间是 2–8，`lot=1` 查不到 | 计划数量填 **≥ 2** |
| 2 | `im = 12.0 / 14.0 / 15.5` 都判 Fail，看着像「分档边界应该过」 | **容差 `[12.35,13.65]` 与分档 `A/B` 是两把独立的尺子**，两条都要满足 | 演示越档用 `im = 16.5`；演示分档 Pass 用 `im = 13.0` |
| 3 | 走「原始单据（AI 抽取）」通道时演示不出低置信 | 该通道逐行置信度固定 `0.9` | 低置信必须用「**已结构化数据**」+ `SN-LOW-*` |
| 4 | 从「已指派」直接登记复检 `result=fail` 返回 409 | 异常状态机**没有 `assigned → rejected` 边** | 先「填报整改措施」把异常推到**待复检**，再登记复检 |
| 5 | Mock 模式置信度 0.85 / 0.62，真模型下完全不一样 | 两条路径的置信度来源不同 | 验收写死数字时切 `ZHL_LLM_MODE=mock` |
| 5b | 复检通过后不需要（也找不到）「补勾已验证」按钮 | 2026-10-15 起复检 `result=pass` 时后端**自动置位** `capa_verified`；异常节点对 `pending_recheck` 只留前向提示 | 复检通过后直接在复检卡片点「确认关闭异常」 |
| 5c | 「临时措施」在指派时没填，之后异常关不掉 | 关闭硬要求 `containment_action` 非空，而指派端点只接受 `isolated` 状态 | 在**复检卡片**点「补填整改措施」就地补齐；或指派时就填 |
| 6 | 真模型下点「AI 归因」要等约 1 分钟 | 现场调用真实模型，单次 40~90 秒 | 界面已有「AI 归因中…」提示与 120s 超时；演示前先跑一次预热 |
| 7 | 自动创建的异常「严重度」恒为「较严重」 | `ensure_incident` 自动路径固定 `major`，不自动升 critical | 需要 critical 请手工建异常并选「严重」 |
| 8 | 报告正文的「留痕引用」显示「（无轨迹引用）」 | 报告渲染层拿到的 `trace_refs`/`evidence_refs` 为空 | 轨迹与证据可在右栏「批次轨迹」与数据库核实；不影响断言结论 |
| 8b | 「AI 抽取结果」里的 `pmax` 和判定结果对不上 | 判定读 **DB 原始列**，面板显示的是**模型输出**，两套数（见 §2.3） | 改提交的那 9 列，不要改面板数字；对不上是预期行为 |
| 8c | `im` 是全项目最容易被真模型改写的一列 | 判定读 DB 列（不会被改），但如果源数据本身 `im` 异常，判定的失败项会同时出现 `parameter_tolerances` 与 `current_band` | 看到两个失败项属正常（§0.2） |
| 11 | 打开放行节点就自动放行、签署人显示 `probe` | **已修**（2026-10-15）：放行面板预检原用签署端点 `POST /release` + `approved_by='probe'`，渲染一次页面就把批次真放了 | 现走只读 `GET /release-preview`（同一份七项断言，不写库）；`POST /release` 只在点「确认放行」填了批准人后调用，空/空白签署人返回 422 |
| 12 | 放行后中栏「已放行」、左栏还「待放行」 | **已修**（2026-10-15）：预检推进/放行成功只刷新了中栏 | 放行与结论重算成功后中栏 + 左栏同步刷新 |
| 13 | 提交送检后界面无反馈，刷新才看到结果 | **已修**（2026-10-15）：「入场结果」卡片直接 append 到主区，被 `_loadDetail` 的异步回调 `replaceChildren` 整个抹掉 | 卡片进渲染状态（`intakeResult`），由渲染器统一画，异步刷新不再吞掉。**同日补充**：原先展示条件多带 `detail.status === 'pending_intake'`，而自动确认（置信度 ≥0.8）会让批次立刻转「判定」——**最常走的那条路反而看不到这张卡**（C1 第 3 步因此走不通）。现改为只按生命周期显示：换批次 / 回总览 / 确认或拒绝入场时才消失 |
| 14 | 真模型下提交送检**老是超时**（约 15s 报「请求超时」），刷新后批次却已推进 | POST /api/intake 单请求同步跑完整条主链：原始单据解析 + 逐组件抽取都要现场调真模型，单次 40~90s 且**逐组件累加**，前端 15s 默认超时必然 abort；后端不随浏览器中断而停，跑完照常落库 | **已修**（2026-10-15）：提交即开「处理中」等待窗（转圈 + 计时）；超时不算失败，自动切轮询批次状态（2s 一次、上限 4 分钟），批次推进或行已入库（低置信等确认）都收窗展示结果；表单校验失败改为 return false 不再丢输入 |
| 9 | 归因「修改后采纳」刚打开的表单被立刻关掉 | 已修（`ui.js` 的 extraButtons 回调返回 `false` 表示不关窗） | 若复现，检查该分支的 `return false` 是否还在 |
| 10 | 待办异常重复创建 | `ensure_incident` 对「同批次 + 同触发原因 + 未关闭」幂等；**人工端点不做此约束**（业务允许多条同因异常） | 创建表单会提示「本批已有 N 条未关闭的同因异常」，按钮变「仍要新建」 |

---

## 附录 · CSV 测试数据（`docs/test/csv/`）

三组现成文件，直接在「数据入场」→「从 CSV 导入」里选。**标注的判定结果是拿真判定引擎逐行跑出来的**，不是推断。

规则基准（`JH-550W 默认规则` 与 `JH-550W 测试型号 v2` 两版种子规则**参数一致**，选哪个都一样）：
功率 `550 ×(1±3%)` → `[533.5, 566.5]`；`temp 25.0±10%`、`isc 10.5±5%`、`voc 48.0±5%`、`vm 40.0±5%`、`im 13.0±5%`、`ff 0.80±2%`；电流档 A `[12.0, 14.0)`；外观 `pass/fail`。**全部为闭区间——等于端点即合格。**

### csv-01-normal.csv —— 正常样本（5 行）

预览应显示 **5 行有效、0 行有问题**，可提交；判定后 **5 个组件全部合格**。

### csv-02-boundary.csv —— 边界（14 行：9 合格 + 5 不合格）

预览同样 **14 行有效、0 行有问题**（这些行的**格式**都合法，靠判定分胜负）；导入后判定：**9 合格 / 5 不合格**，批次进入**已隔离**并自动建异常。

| SN | 探的是什么 | 判定 |
| --- | --- | --- |
| `SN-EDGE-PMAX-LO` / `-HI` | 功率下/上端点 `533.5` / `566.5` | **合格**（闭区间） |
| `SN-EDGE-TEMP-LO` / `-HI` | 温度下/上端点 `22.5` / `27.5` | **合格** |
| `SN-EDGE-ISC-LO` | `10.5×0.95` 的浮点端点 `9.975` | **合格**（专门探浮点，曾是最易误判的一处） |
| `SN-EDGE-VOC-LO` / `SN-EDGE-VM-HI` | Voc `45.6` / Vm `42.0` 端点 | **合格** |
| `SN-EDGE-IM-HI` | Im 容差上端点 `13.65`（仍在 A 档内） | **合格** |
| `SN-EDGE-FF-HI` | `0.80×1.02 = 0.8160000000000001` 端点 | **合格** |
| `SN-EDGE-PMAX-LO-OUT` / `-HI-OUT` | `533.4` / `566.6`，各越界一步 | **不合格**（`power_range`） |
| `SN-EDGE-TEMP-LO-OUT` | `22.4` | **不合格**（`parameter_tolerances`） |
| `SN-EDGE-IM-OUT` | `13.66` | **不合格**（`parameter_tolerances`） |
| `SN-EDGE-FF-HI-OUT` | `0.8161` | **不合格**（`parameter_tolerances`） |

### csv-03-errors.csv —— 格式错误（导入预览必须拦下）

预览应显示 **2 行有效、4 行有问题**，「确认导入」为**禁用**状态，问题逐条带行号：

| 行 | 内容 | 预期提示 |
| --- | --- | --- |
| 2 | 只有 8 列（缺外观） | `第 2 行：应为 9 列，当前 8 列` |
| 3 | `pmax=abc` | `第 3 行：pmax 不是数字（abc）` |
| 4 | 外观写 `OK` | `第 4 行：外观须为 pass/fail（当前 OK）` |
| 5 / 6 | 同一 SN 出现两次 | `第 6 行：SN SN-ERR-DUP 在文件内重复（第 5 行已出现）` |

第 7 行 `SN-ERR-FINE` 本身合法，用来验证**"一行坏牵连整批"**——它不会因为旁边有错行就被提交。
