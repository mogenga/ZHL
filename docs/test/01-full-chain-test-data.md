# 质慧链 · 全链路连通测试数据

日期：2026-10-14
定位：基于**现有实现**（非愿景）设计。覆盖五段主链 + 异常子链全部合法路径，用于全链路连通验证。姊妹文档 `02-targeted-test-data.md`（正常/边界/失败矩阵）。

> 所有数据与现有夹具/种子**同源**：规则 `JH-550W v1`、ISO 表 `iso2859_1_aql.json`。
> 功率边界：铭牌 550.0 ±3% → **下限 533.5 / 上限 566.5**（闭区间，等于边界判 Pass）。
> 电流分档：`im ∈ [12.0,14.0]=A`、`[14.0,15.5]=B`；`im=16.5` 越档触发异常。

---

## 1. 链路总览

批次（10 态机）：

```
draft → pending_intake → pending_judge ─全Pass+抽样Pass─→ pending_release ─断言全过+批准─→ released → archived
                                    └─任一Fail─→ isolated → disposing → pending_recheck ─复检过─→ pending_release
                                                                      └─复检败─→ rejected → archived
任意非归档 ─人工拒绝─→ rejected
```

异常（7 态机）：`new → isolated → assigned → pending_recheck → closed`（复检败转 `rejected`；`assigned` 可报废 `scrapped`）。
批次-异常联动：`close` 仅当批次**全部**异常终态才推进批次到 `pending_release`。

| # | 链路 | 触发数据 | 关键调用 | 批次终态 | 异常终态 |
| --- | --- | --- | --- | --- | --- |
| C1 | 正常放行 | 10 SN 全 Pass | 建批→送检→确认→判定→放行→报告 | archived | 无 |
| C2 | 功率边界 Pass | pmax=533.5 | 同 C1（小批） | archived | 无 |
| C3 | 功率边界 Fail | pmax=533.4 | 建批→送检→判定→isolated | isolated | 可建异常 |
| C4 | 异常复检通过→放行 | im=16.5 越档 | 建异常→隔离→指派→复检(pass)→CAPA→关闭→放行→报告 | archived | closed |
| C5 | 异常复检失败→拒绝 | 复检 fail | 建异常→…→复检(fail)→拒绝/报告 | archived（经 rejected） | rejected |
| C6 | 关闭五条件缺失 | 逐项缺 | close 反复 409，detail.missing 指名 | pending_recheck | pending_recheck |
| C7 | 多异常批次联动 | 2 只异常 | close 第1只→仍待复检；第2只→待放行 | pending_release | closed×2 |
| C8 | 归档只读守卫 | 已 archived | 任意写操作 → 409 | archived | — |
| C9 | 跨批同 SN 一致性 | 两批同 SN | 第二批送检 → 400 + 自动建 data_consistency 异常 | 第二批 pending_intake | new(critical) |
| C10 | 人工拒绝 | 非归档批次 | reject → D7 报告 + D6a 归档 | archived（经 rejected） | — |
| C11 | 归因三选一 | 隔离批次 | attribute(decision=adopt/reject/defer) | 不变 | 不变 |

**C1–C4 为 spec 四组样例主链；C5–C8 异常分支；C9–C11 守卫 + 新增端点。**

---

## 2. 共用种子数据

### 2.1 规则版本（幂等键 `P-DEFAULT+JH-550W+1`）

```json
{
  "project_id": "P-DEFAULT", "model": "JH-550W", "version_no": 1,
  "nameplate_power": 550.0, "power_tolerance": 0.03,
  "parameter_standards": {
    "temp": {"std_value": 25.0, "tolerance": 0.10},
    "isc":  {"std_value": 10.5, "tolerance": 0.05},
    "voc":  {"std_value": 48.0, "tolerance": 0.05},
    "vm":   {"std_value": 40.0, "tolerance": 0.05},
    "im":   {"std_value": 13.0, "tolerance": 0.05},
    "ff":   {"std_value": 0.80, "tolerance": 0.02}
  },
  "current_bands": [
    {"lower": 12.0, "upper": 14.0, "grade": "A"},
    {"lower": 14.0, "upper": 15.5, "grade": "B"}
  ],
  "sample_plan": {"inspection_type": "iv", "inspection_level": "normal", "aql": 1.0}
}
```

### 2.2 标准值基准（规则 `JH-550W v1` 的 parameter_standards）

判定读的是**检验行数据库列**（`judge.py:_inspection_in` 直接取 `row.pmax/temp/isc/voc/vm/im/ff/appearance_result`），不是 LLM 抽取值。每个 SN 一行，电参数默认取标准值，只改「变量列」：

| 参数 | 标准值 | 容差 | 通过区间 |
| --- | --- | --- | --- |
| pmax (W) | 550.0 | ±3% | **533.5 ~ 566.5**（闭区间） |
| temp (°C) | 25.0 | ±0.10 | 24.90 ~ 25.10 |
| isc (A) | 10.5 | ±0.05 | 10.45 ~ 10.55 |
| voc (V) | 48.0 | ±0.05 | 47.95 ~ 48.05 |
| vm (V) | 40.0 | ±0.05 | 39.95 ~ 40.05 |
| im (A) | 13.0 | ±0.05 | 12.95 ~ 13.05 |
| ff | 0.80 | ±0.02 | 0.78 ~ 0.82 |
| appearance | pass | — | pass |

电流分档（judge 额外检查）：`im ∈ [12.0,14.0]=A`、`[14.0,15.5]=B`；**`im=16.5` 越档 → 触发 `current_band_oob` 异常**。

### 2.3 各链路送检数据（完整 9 列：SN,pmax,temp,isc,voc,vm,im,ff,外观）

每 SN 一行、只改变量列，其余列取 §2.2 标准值。下表为**完整可粘贴行**，其余电参数同标准值：

| 用途 | SN | pmax | im | 其余列 | 说明 |
| --- | --- | --- | --- | --- | --- |
| C1 正常 | SN-N-001 ~ 010 | 550.0 | 13.0 | 标准值 | 10 行全 Pass |
| C2 边界 Pass | SN-B-LOW | **533.5** | 13.0 | 标准值 | pmax=下限，判 Pass |
| C3 边界 Fail | SN-B-BELOW | **533.4** | 13.0 | 标准值 | pmax<下限，判 Fail |
| C4/C5 越档 | SN-I-OODB | 550.0 | **16.5** | 标准值 | im 越档，触发异常 |
| C9 跨批冲突 | SN-SHARED | 550.0 | 13.0 | 标准值 | 同 SN 送两批 |

完整行示例（判定引擎按这 9 列取值）：

```
SN-N-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-B-LOW,533.5,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-B-BELOW,533.4,25.0,10.5,48.0,40.0,13.0,0.80,pass
SN-I-OODB,550.0,25.0,10.5,48.0,40.0,16.5,0.80,pass
SN-SHARED,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass
```

> 注：工作台前端「检测数据」textarea 与 API 路径均提交**完整 9 列**（`intake.js` 每行解析 SN + 7 电参 + 外观，缺列/非数前端即拦截；后端 `intake.py` 再校验一次，缺列返回 B3 400 `incomplete_inspection` 而非 500）。判定结果以 DB 列为准，9 列即「送检数据」完整形态。

### 2.4 ISO 抽样（放行断言 ⑦ 用）

| type | lot 区间 | n | ac | re |
| --- | --- | --- | --- | --- |
| iv | 2–8 | 8 | 0 | 1 |
| iv | 9–15 | 13 | 0 | 1 |
| iv | 16–25 | 20 | 1 | 2 |

C4/C5 另预置一条 `level=full, aql=0.65, lot=1–100, n=2/ac=0/re=1`（供 item ⑦ 解析）。

---

## 3. 人工验收操作流程

> 入口：浏览器打开工作台页（`workbench.html`）。左栏四 tab（批次 / 规则库 / 异常 / 报告）+ 中央流水线节点面板。
> 每条链路 = 一系列「点击 → 输入 → 理想反馈」。**批次创建 / 检测数据 / 复检六参**均已给出完整值，可直接照填。

### C1 正常放行（10 SN 全 Pass）

| 步骤 | 点击哪里 | 输入什么 | 理想反馈 |
| --- | --- | --- | --- |
| 1 | 左栏「＋ 新建批次」 | **批次号** `B-20261014-001`、**项目** `P-DEFAULT`、**型号** `JH-550W`、**计划数量** `10`、**规则版本** `JH-550W 检验规则 v1`（须已发布） | 批次出现在「批次」tab，状态「待入场」，toast「批次已创建，进入数据入场」 |
| 2 | 中栏流水线节点「送检」（数据入场面板）→点「提交送检数据」 | 数据来源=`检测数据`；检测数据框粘贴 10 行（§2.3 的 C1 完整 9 列：`SN-N-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass` … 至 `SN-N-010`） | 置信度≥0.8（mock 0.81）→ 自动确认，批次转「待判定」 |
| 3 | 若出现「确认入场」（conf<0.8） | 点「确认入场」 | 批次转「待判定」 |
| 4 | 流水线节点「判定」 | 点「执行判定」 | 10/10 Pass；批次转「待放行」，右栏「判定合格=10」 |
| 5 | 流水线节点「放行」 | **批准人** `ZHL-ENG`，点「确认放行」 | 7 项断言全 ✓，批次转「已放行」 |
| 6 | 左栏「报告」tab →点「生成」 | **报告类型**=`放行报告` | 生成 v1 放行报告；批次自动转「已归档」 |

**理想终态**：批次 `archived`，流水线全绿，右侧轨迹含 判定/放行/报告 记录。

### C4 异常复检通过→放行（核心闭环）

前置：先按 C3 走一遍判定把批次判成「已隔离」（`SN-I-OODB.im=16.5` 越档）。

| 步骤 | 点击哪里 | 输入什么 | 理想反馈 |
| --- | --- | --- | --- |
| 1 | 左栏「异常」tab，点「创建异常」 | 触发原因=规则失败、严重度=较严重、受影响条码=SN-I-OODB（每行一个）、证据引用=判定快照 ID | 异常 `new`，挂到批次 |
| 2 | 异常节点「隔离」 | 无（直接点） | 异常→`isolated` |
| 3 | 异常节点「指派」 | 责任人=`ZHL-QA`、处置期限=日期、临时措施=隔离该组件 | 异常→`assigned`，批次→`disposing` |
| 4 | 流水线「复检」节点 | 复检组件=`SN-I-OODB`、结果=`通过`、Pmax=`550.0`/Temp=`25.0`/Isc=`10.5`/Voc=`48.0`/Vm=`40.0`/Im=`13.0`/Ff=`0.80`、执行人=`ZHL-QA`、执行时间=当前、范围=`最小集` | 复检 201 `passed:true`，批次→`pending_recheck` |
| 5 | 异常节点「CAPA 确认」 | 确认根因=电芯虚焊致 Impp 漂移、纠正措施=更换主电芯、勾选「已验证」 | 批次→`pending_recheck` |
| 6 | 异常节点「关闭」 | 无（点） | 异常→`closed`，批次→`pending_release` |
| 7 | 放行节点 | 批准人=`ZHL-ENG`，点「确认放行」 | 7 项全 ✓，批次→`released` |
| 8 | 报告 tab「生成」 | 放行报告 | 报告 v1 + 批次→`archived` |

**理想终态**：批次 `archived`，异常 `closed`，轨迹含 归因/放行/报告。

### C5 异常复检失败→拒绝

同 C4 至步骤 4，但「结果」选 **不通过**（`pmax=540`）。

| 步骤 | 点击哪里 | 输入什么 | 理想反馈 |
| --- | --- | --- | --- |
| 4' | 复检节点 | 结果=`不通过`、Pmax=`540.0`（<下限 533.5 判 Fail）、其余同 C4 步骤 4 | 复检 `passed:false`，异常→`rejected`，批次→`rejected` |
| 5' | 放行节点切「拒绝」 | 拒绝原因=复检不通过、操作人=`ZHL-QA`，点「确认拒绝」 | 生成 异常处置报告，批次自动→`archived` |

**理想终态**：批次 `archived`（经 `rejected`），异常 `rejected`。

### C6 关闭五条件缺失（逐条指名）

异常停在「待复检」时反复点「关闭」，看右侧提示逐条缺失：

| 缺什么 | 理想反馈（detail.missing） |
| --- | --- |
| 没指派责任人 | `owner` |
| CAPA 根因/措施空 | `capa_fields` |
| 没做复检 | `reinspection_record` |
| 没勾「已验证」 | `capa_verified` |
| 没证据引用 | `original_decision_evidence` |

全缺时一次性列出全部。

### C7 多异常批次联动

同批 2 只异常（`SN-I-OODB` + 另一 SN）。关闭第 1 只 → 批次**仍**「待复检」；关闭第 2 只 → 批次才「待放行」。

### C8 归档只读

批次「已归档」后，再点「生成报告 / CAPA / 改状态」→ 一律提示 `409 批次已归档，终态只读`。

### C9 跨批同 SN 一致性

批次 A 送检 `SN-SHARED`；批次 B 再送检同 `SN-SHARED` → 批次 B 提示 `400 跨批次 SN 冲突`，并在**批次 A** 自动挂一条 `data_consistency` 严重异常；B 维持「待入场」。

### C10 人工拒绝

批次处于任意非归档态 → 放行节点切「拒绝」，填 拒绝原因 + 操作人 → 生成 异常处置报告 + 批次自动「已归档」。

### C11 归因三选一

异常节点「归因」→ 系统出假设排序；操作人选 `采纳/驳回/修改`，采纳时填根因描述 → 写确认根因；无匹配 fixture 时提示「该异常 UUID 无归因 fixture」。

---

## 4. 走查顺序与复现

推荐剧本（每链路独立会话，互不污染）：

```
C1 正常放行  →  C2 边界 Pass vs C3 边界 Fail
C4 复检通过放行  vs  C5 复检失败拒绝
C6 五条件缺失 → C7 多异常联动 → C8 归档只读
C9 跨批冲突  →  C10 人工拒绝  →  C11 归因三选一
```

```bash
# 全量 E2E / 验收样例
uv run pytest tests/api/test_e2e_acceptance.py tests/e2e/ -v
# 起容器后查批次状态
docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl -c "SELECT batch_no, status FROM delivery_batches;"
```

---

## 5. 与现有夹具对应

| 链路 | 夹具 / 测试 |
| --- | --- |
| C1 | `acceptance_normal.json` + `test_e2e_acceptance.py`（样例一） |
| C2/C3 | `acceptance_boundary.json`（样例二 / 对照组） |
| C4/C5 | `acceptance_incident.json` + `test_sample3_incident_closure.py` / `test_closure.py` |
| C6/C7/C8 | `test_closure.py` |
| C9 | `acceptance_consistency.json` + `test_sample4_consistency.py` |
| C10/C11 | 本分支 `test_release_reject.py` / `test_incident_attribute.py` |
