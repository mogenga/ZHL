# 质慧链 · 针对性测试用例（正常 / 边界 / 失败）

日期：2026-10-15
定位：按**测试对象**分组的「输入 → 预期输出」矩阵。每条用例给出**完整输入值**，可直接转写为 pytest 参数化用例，也可手工照做。
依据：`app/domain/{judge,sampling,release,state_machine}.py`、`app/api/*.py`、`app/skills/*.py`、`migrations/001_init.sql`、`AGENT.md` §3.3。

姊妹文档：`01-full-chain-test-data.md`（全链路逐步操作）。

> **本文件所有「预期」都已在跑起来的系统上实测确认**，不是从文档转抄。凡与旧版本表述冲突处，以本文件的实测值为准（差异见 §9）。

---

## 0. 判定引擎的四条口径（先记住，再看用例）

### 0.1 容差是**相对**容差，闭区间

```
lower = 标准值 × (1 − 容差)
upper = 标准值 × (1 + 容差)
lower ≤ 实测值 ≤ upper        ← 闭区间：等于任一端点判 Pass
```

对应 `app/domain/judge.py::tolerance_interval`，公式字符串为
`"lower = std * (1 - tol); upper = std * (1 + tol)"`。

**实测推出的各参数区间**（规则 `JH-550W v1`）：

| 参数 | 标准值 | 容差 | 闭区间 | 边界实测 |
| --- | --- | --- | --- | --- |
| `pmax` | 550.0 | 0.03 | **[533.5, 566.5]** | 533.5 Pass / 533.4 Fail / 566.5 Pass / 566.6 Fail |
| `temp` | 25.0 | 0.10 | [22.5, 27.5] | 22.5 Pass / 27.5 Pass / 30.0 Fail |
| `isc` | 10.5 | 0.05 | [9.975, 11.025] | 9.975 Pass |
| `voc` | 48.0 | 0.05 | [45.6, 50.4] | 45.6 Pass / 50.4 Pass / 55.0 Fail |
| `vm` | 40.0 | 0.05 | [38.0, 42.0] | 40.05 Pass |
| `im` | 13.0 | 0.05 | **[12.35, 13.65]** | 12.35 Pass / 13.65 Pass / 12.0 Fail |
| `ff` | 0.80 | 0.02 | [0.784, 0.816] | 0.784 Pass / 0.816 Pass / 0.779 Fail |

### 0.2 电流分档是**独立**的第五项校验

`im` 必须同时满足：① 落在容差区间 `[12.35, 13.65]`；② 落在某个声明档位（`A=[12.0,14.0]`、`B=[14.0,15.5]`）。

**实测的坑**：`im = 12.0`、`14.0`、`15.5` 全部 **Fail** —— 它们满足分档但违反容差（`parameter_tolerances` 报错），`current_band` 这一项本身反而是 Pass。

| `im` 取值 | `parameter_tolerances` | `current_band` | 总结果 |
| --- | --- | --- | --- |
| 13.0（标准值） | Pass | Pass（A 档） | **Pass** |
| 12.35 / 13.65（容差端点） | Pass | Pass（A 档） | **Pass** |
| 12.0（A 档下界） | **Fail** | Pass | Fail |
| 14.0（A/B 交界） | **Fail** | Pass | Fail |
| 15.5（B 档上界） | **Fail** | Pass | Fail |
| 15.6（B 档外） | **Fail** | **Fail** | Fail |
| 16.5 | **Fail** | **Fail** | Fail |

### 0.3 判定固定五步顺序，`checks` 返回全部五项

① `required_fields` → ② `power_range` → ③ `parameter_tolerances` → ④ `current_band` → ⑤ `appearance`。任一 Fail 即总结果 Fail，但**五项都会算**，`Decision.checks` 里逐条给出输入、公式、计算值与结果。

### 0.4 判定读数据库列，不读模型输出

`judge()` 的入参来自 `module_inspections` 表的**原始列**（`judge.py::_inspection_in` 直接取 `row.pmax/temp/isc/voc/vm/im/ff/appearance_result`），**不是** LLM 的抽取值。

实测对照（同一批两行）：

| SN | 提交列 `pmax` | 模型抽取值 `pmax` | 判定 |
| --- | --- | --- | --- |
| `SN-N-001` | 533.5 | 547.3 | **Pass** |
| `SN-N-002` | 533.4 | 551.94 | **Fail** |

构造用例时只需控制提交列；抽取值与判定结果**没有**因果关系，不要用它做断言。

### 0.5 必填字段缺一即**先于**其他项拦截（但后续项仍会算）

① 检查 `sn / grade / captured_date / pmax / temp / isc / voc / vm / im / ff` 十项非空。

> **实测注意**：把 `pmax` 置为 `None` 会让 ② `check_power_range` 抛
> `TypeError: '<=' not supported between instances of 'float' and 'NoneType'`。
> 即 ① 本身返回 Fail 但**不会短路**，② 会因为拿 `None` 做比较而崩。所以
> **走接口的用例永远拿不到 `None`**（DDL 是 `NOT NULL`，`intake` 端点也会先挡）；
> 只有直接调 `judge()` 的纯函数单测才会碰到，此时必须只断言 ① 的返回，
> 或把该项换成空串 `""`（字符串比较同样会崩，故应在 ① 之后单独断言）。

---

## 1. 判定引擎 `judge()`

基座输入（除标注变化的那一列外全部取此值）：

```python
BASE = {"sn": "SN-X", "project": "P-DEFAULT", "grade": "A", "captured_date": "2026-06-01",
        "pmax": 550.0, "temp": 25.0, "isc": 10.5, "voc": 48.0, "vm": 40.0,
        "im": 13.0, "ff": 0.80, "appearance_result": "pass"}
```

规则：`nameplate_power=550.0, power_tolerance=0.03`，参数标准与分档见 §0.1/§0.2。

### 1.1 正常（全 Pass）

| ID | 场景 | 变化字段 | 预期 `result` | 预期未过项 |
| --- | --- | --- | --- | --- |
| J-N01 | 全部标准值 | 无 | `Pass` | `[]` |
| J-N02 | 功率取上限 | `pmax=566.5` | `Pass` | `[]` |
| J-N03 | 功率取下限 | `pmax=533.5` | `Pass` | `[]` |
| J-N04 | 温度取下界 | `temp=22.5` | `Pass` | `[]` |
| J-N05 | 温度取上界 | `temp=27.5` | `Pass` | `[]` |
| J-N06 | Voc 取下界 | `voc=45.6` | `Pass` | `[]` |
| J-N07 | Voc 取上界 | `voc=50.4` | `Pass` | `[]` |
| J-N08 | isc 取下界 | `isc=9.975` | `Pass` | `[]` |
| J-N09 | ff 取下界 | `ff=0.784` | `Pass` | `[]` |
| J-N10 | ff 取上界 | `ff=0.816` | `Pass` | `[]` |
| J-N11 | im 容差下界 | `im=12.35` | `Pass` | `[]` |
| J-N12 | im 容差上界 | `im=13.65` | `Pass` | `[]` |
| J-N13 | vm 微偏 | `vm=40.05` | `Pass` | `[]` |

### 1.2 边界（钉住闭区间语义，含对照组）

| ID | 参数 | 取值 | 预期 `result` | 预期未过项 | 说明 |
| --- | --- | --- | --- | --- | --- |
| J-B01 | pmax | **533.5**（下限） | `Pass` | `[]` | 等于下限判 Pass |
| J-B02 | pmax | **533.4**（下限 −0.1） | `Fail` | `['power_range']` | 对照组 |
| J-B03 | pmax | **566.5**（上限） | `Pass` | `[]` | |
| J-B04 | pmax | **566.6**（上限 +0.1） | `Fail` | `['power_range']` | 对照组 |
| J-B05 | temp | 22.5 / 27.5 | `Pass` | `[]` | 两端点 |
| J-B06 | temp | 30.0 | `Fail` | `['parameter_tolerances']` | |
| J-B07 | ff | 0.784 / 0.816 | `Pass` | `[]` | 两端点 |
| J-B08 | ff | 0.779 | `Fail` | `['parameter_tolerances']` | |
| J-B09 | im | 12.35 / 13.65 | `Pass` | `[]` | **容差**端点，不是分档端点 |
| J-B10 | im | **12.0**（A 档下界） | `Fail` | `['parameter_tolerances']` | ⚠️ 分档过、容差不过 |
| J-B11 | im | **14.0**（A/B 交界） | `Fail` | `['parameter_tolerances']` | ⚠️ 同上 |
| J-B12 | im | **15.5**（B 档上界） | `Fail` | `['parameter_tolerances']` | ⚠️ 同上 |
| J-B13 | im | **15.6** | `Fail` | `['parameter_tolerances', 'current_band']` | 两项同时不过 |
| J-B14 | im | **16.5** | `Fail` | `['parameter_tolerances', 'current_band']` | 全链路用到的越档值 |

### 1.3 失败（单一故障维度）

| ID | 故障 | 变化字段 | 预期 `result` | 预期未过项 |
| --- | --- | --- | --- | --- |
| J-F01 | 功率越下限 | `pmax=533.4` | `Fail` | `['power_range']` |
| J-F02 | 功率越上限 | `pmax=566.6` | `Fail` | `['power_range']` |
| J-F03 | 电流越档 | `im=16.5` | `Fail` | `['parameter_tolerances', 'current_band']` |
| J-F04 | 温度越界 | `temp=30.0` | `Fail` | `['parameter_tolerances']` |
| J-F05 | Voc 越界 | `voc=55.0` | `Fail` | `['parameter_tolerances']` |
| J-F06 | 外观不合格 | `appearance_result='fail'` | `Fail` | `['appearance']` |
| J-F07 | SN 为空 | `sn=''` | `Fail` | `['required_fields']` |

### 1.4 判定证据的四元组（可复算）

每条检查项都落 `input` / `formula` / `computed` / `result`。实测样例（`pmax=566.6`）：

```json
{"rule": "power_range",
 "input": {"nameplate_power": 550.0, "tolerance": 0.03, "pmax": 566.6},
 "formula": "lower = nameplate * (1 - tol); upper = nameplate * (1 + tol)",
 "computed": {"lower": 533.5, "upper": 566.5},
 "result": "Fail"}
```

`parameter_tolerances` 项的 `computed` 逐参数给出 `{lower, upper, value}`，实测（全标准值时）：

```json
{"temp": {"lower": 22.5, "upper": 27.500000000000004, "value": 25.0},
 "isc":  {"lower": 9.975, "upper": 11.025, "value": 10.5},
 "voc":  {"lower": 45.599999999999994, "upper": 50.400000000000006, "value": 48.0},
 "vm":   {"lower": 38.0, "upper": 42.0, "value": 40.0},
 "im":   {"lower": 12.35, "upper": 13.65, "value": 13.0},
 "ff":   {"lower": 0.784, "upper": 0.8160000000000001, "value": 0.8}}
```

> 浮点表示带尾数（如 `27.500000000000004`）是预期行为，断言请用容差比较而非 `==`。

### 1.5 参数未启用

| ID | 场景 | 输入 | 预期 |
| --- | --- | --- | --- |
| J-X01 | ff 停用 | 规则 `ff.enabled=false` + 实测 `ff=0.50` | `parameter_tolerances` 跳过 ff → `Pass` |
| J-X02 | 多参停用 | `temp`、`ff` 均 `enabled=false` | 仅 isc/voc/vm/im 参与容差校验 |

---

## 2. 抽样 `iso_lookup` / `freeze_sample`

### 2.1 正常（`iv` / `normal`，实测命中值）

| ID | 输入 `(type, level, aql, lot)` | 预期 |
| --- | --- | --- |
| S-N01 | `iv, normal, 1.0, lot=10` | `n=13, ac=0, re=1` |
| S-N02 | `iv, normal, 1.0, lot=5` | `n=8, ac=0, re=1` |
| S-N03 | `iv, normal, 1.0, lot=20` | `n=20, ac=1, re=2` |
| S-N04 | `iv, normal, 0.65, lot=100` | `n=80, ac=3, re=4` |
| S-N05 | `iv, normal, 1.5, lot=100` | `n=80, ac=5, re=6` |
| S-N06 | `visual, normal, 1.0, lot=10` | `n=13, ac=0, re=1` |

### 2.2 边界（lot 区间端点，`iv/normal/aql=1.0` 实测）

| ID | lot | 命中区间 | 预期 |
| --- | --- | --- | --- |
| S-B01 | 2 | 2–8 | `n=8, ac=0, re=1` |
| S-B02 | 8 | 2–8（含上界） | `n=8, ac=0, re=1` |
| S-B03 | 9 | 9–15（含下界） | `n=13, ac=0, re=1` |
| S-B04 | 15 | 9–15 | `n=13, ac=0, re=1` |
| S-B05 | 16 | 16–25 | `n=20, ac=1, re=2` |
| S-B06 | 25 | 16–25 | `n=20, ac=1, re=2` |
| S-B07 | 26 | 26–50 | `n=32, ac=1, re=2` |
| S-B08 | 50 | 26–50 | `n=32, ac=1, re=2` |
| S-B09 | 51 | 51–90 | `n=50, ac=2, re=3` |
| S-B10 | 90 | 51–90 | `n=50, ac=2, re=3` |
| S-B11 | 91 | 91–150 | `n=80, ac=3, re=4` |
| S-B12 | 150 | 91–150 | `n=80, ac=3, re=4` |

### 2.3 失败（D9：缺表即规则异常，**没有静默默认放行**）

| ID | 输入 | 预期 |
| --- | --- | --- |
| S-F01 | `lot=1`（低于最小下界 2） | `iso_lookup → None` → 抽样结论 `rule_missing` |
| S-F02 | `lot=151`（超最大上界 150） | `None` → `rule_missing` |
| S-F03 | 未预置组合 `(type,level,aql)`，如 `iv, tightened, 1.0, lot=20` | `None` → `rule_missing` |
| S-F04 | `freeze_sample` 同一 seed 重放 | 选中 SN 集完全一致（实测两次调用结果相同） |
| S-F05 | 可用 SN 少于 `n` | 选中全部可用 SN（`n = min(iso.n, len(sns))`），不报错 |

**端到端实测**：批次计划数量 `151`，判定结果 `Pass`，但抽样结论 `rule_missing`，批次被 **隔离** 并自动建 `rule_missing / major` 异常，轨迹 `structured_output = {"result": "Pass", "sample_result": "rule_missing"}`。

---

## 3. 状态机 `transition()`

`transition(current, target, preconditions, kind)` 表驱动，非法边与前置不足都返回 `accepted=False` 并给出 `reason`。

### 3.1 批次合法边（实测全表）

| 从 | 到 | 前置条件 |
| --- | --- | --- |
| `draft` | `pending_intake` | （空） |
| `draft` | `rejected` | `human_rejected` |
| `pending_intake` | `pending_judge` | （空） |
| `pending_intake` | `rejected` | `human_rejected` |
| `pending_judge` | `pending_release` | `all_pass`, `sample_pass` |
| `pending_judge` | `isolated` | `any_fail` |
| `pending_judge` | `rejected` | `human_rejected` |
| `isolated` | `disposing` | `assigned_owner`, `due_at_set` |
| `isolated` | `rejected` | `human_rejected` |
| `disposing` | `pending_recheck` | `measures_confirmed` |
| `pending_recheck` | `pending_release` | `recheck_passed`, `measures_effective` |
| `pending_recheck` | `rejected` | （空） |
| `pending_release` | `released` | `assertions_passed`, `human_approved` |
| `pending_release` | `rejected` | `human_rejected` |
| `released` | `archived` | （空） |
| `rejected` | `archived` | （空） |

> `archived` **无任何出边**（终态）。

### 3.2 异常合法边（实测全表）

| 从 | 到 | 前置条件 |
| --- | --- | --- |
| `new` | `isolated` | `sn_linked` |
| `isolated` | `assigned` | `owner_assigned` |
| `isolated` | `scrapped` | — |
| `assigned` | `pending_recheck` | `reinspection_recorded` |
| `assigned` | `scrapped` | `scrap_approved` |
| `pending_recheck` | `closed` | `close_conditions_met` |
| `pending_recheck` | `rejected` | （空） |

> **⚠️ 没有 `assigned → rejected`，也没有 `new → assigned`。**
> `new` 必须先 `isolated`；`assigned` 必须先进 `pending_recheck` 才能转 `rejected`。

### 3.3 用例

| ID | 调用 | 预期 |
| --- | --- | --- |
| T-N01 | batch `draft → pending_intake`（∅） | `accepted=True` |
| T-N02 | batch `pending_judge → pending_release`（`all_pass, sample_pass`） | `accepted=True` |
| T-N03 | batch `pending_recheck → pending_release`（`recheck_passed, measures_effective`） | `accepted=True` |
| T-N04 | batch `pending_release → released`（`assertions_passed, human_approved`） | `accepted=True` |
| T-N05 | batch `released → archived`（∅） | `accepted=True` |
| T-N06 | batch `rejected → archived`（∅） | `accepted=True` |
| T-N07 | incident `new → isolated`（`sn_linked`） | `accepted=True` |
| T-N08 | incident `isolated → assigned`（`owner_assigned`） | `accepted=True` |
| T-N09 | incident `pending_recheck → closed`（`close_conditions_met`） | `accepted=True` |
| T-B01 | batch `pending_judge → pending_release`（只给 `all_pass`） | `accepted=False`，`reason` 含 `precondition(s) not met: sample_pass` |
| T-B02 | batch `pending_release → released`（只给 `assertions_passed`） | `accepted=False`，`reason` 含 `precondition(s) not met: human_approved` |
| T-B03 | incident `new → isolated`（∅，缺 `sn_linked`） | `accepted=False`，`reason` 含 `precondition(s) not met: sn_linked` |
| T-B04 | incident `assigned → pending_recheck`（只给 `measures_confirmed`） | `accepted=False`，`reason` 含 `precondition(s) not met: reinspection_recorded` |
| T-B05 | incident `assigned → scrapped`（∅） | `accepted=False`，`reason` 含 `precondition(s) not met: scrap_approved` |
| T-F01 | batch `draft → released`（跳态） | `accepted=False`，`reason = "no transition batch:draft -> released (skip/invalid)"` |
| T-F02 | batch `archived → pending_intake` | `accepted=False`，`archived` 无出边 |
| T-F03 | incident `new → assigned`（直跳） | `accepted=False`，`no transition incident:new -> assigned` |
| T-F04 | **incident `assigned → rejected`** | `accepted=False`，`no transition incident:assigned -> rejected` |
| T-F05 | batch `rejected → rejected`（重复拒绝） | `accepted=False`，`no transition batch:rejected -> rejected` |
| T-F06 | `kind="foo"` | `accepted=False`，`reason = "unknown kind: foo"` |
| T-F07 | batch `new → isolated`（把异常状态当批次用） | `accepted=False`，`no transition batch:new -> isolated` |

---

## 4. 放行断言 `assert_release()`（七项）

七项名称固定（`app/skills/release_inputs.py::RELEASE_ASSERTION_NAMES`）：
`state` / `sample_passed` / `no_open_critical_incident` / `no_isolated_sn` / `all_evidence_present` / `all_capa_verified` / `reinspection_scope_covered`。

### 4.1 正常

| ID | 输入 | 预期 |
| --- | --- | --- |
| R-N01 | 批次 `pending_release`；抽样最新结论 `pass`；无 open critical；无隔离 SN；证据非空且都带 `object_ref`；所有 CAPA `verified=True`；复检范围覆盖齐且 ISO 可解析 | 7/7 `passed=True` |

### 4.2 边界（逐项单独阻断，逐项指名）

| ID | 阻断项 | 输入变化 | 预期 `reason`（实测原文） |
| --- | --- | --- | --- |
| R-B01 | ① `state` | 批次 `status='pending_recheck'` | `state` Fail |
| R-B02 | ② `sample_passed` | `sample.result='fail'` | `"sample result is fail, expected pass"` |
| R-B03 | ② 无结论 | `sample.result=''` | `"no sampling conclusion for inspection_type <type>"` |
| R-B04 | ③ `no_open_critical_incident` | 存在 `severity=critical` 且非终态的异常 | `"N open critical incident(s): <ids>"` |
| R-B05 | ④ `no_isolated_sn` | `isolated_sns=['SN-X']` | `"isolated SNs remain: SN-X"` |
| R-B06 | ⑤ `all_evidence_present` | `evidence=[]` | `"missing required evidence"` |
| R-B07 | ⑤ 证据未锚定 | 某证据 `object_ref=None` | `"evidence not traceable to an object (missing id/object_ref): E1"` |
| R-B08 | ⑥ `all_capa_verified` | 某 CAPA `verified=False` | `"unverified CAPA: <incident_id>"` |
| R-B09 | ⑦ `reinspection_scope_covered` | open 异常**无** scope 行 | `"open incident I1 has no reinspection scope row"` |
| R-B10 | ⑦ 覆盖不全 | required `['SN-1','SN-2']`，scope 只覆盖 `['SN-1']` | `"incident I1: scope covers ['SN-1'] but required ['SN-1', 'SN-2']; missing ['SN-2']"` |
| R-B11 | ⑦ ISO 查不到 | scope 行的 `(type, level, aql, lot)` 在表中无匹配 | `"incident I1: ISO lookup returned None for (iv, normal, AQL=1.0, lot_size=10)"` |
| R-B12 | ⑦ 规则方案缺项 | scope 行 `plan_error="sample_plan missing ['aql']"` | `"incident I1: sample_plan missing ['aql']"` |

### 4.3 失败（叠加 + 端到端）

| ID | 场景 | 预期 |
| --- | --- | --- |
| R-F01 | ②④⑦ 同时不满足 | `passed=False`，`checks` 三项各带 `reason`，其余四项仍为 Pass |
| R-F02 | API 层：批次非 `pending_release` | `409 {"error": "batch_not_pending_release", "status": "..."}` |
| R-F03 | API 层：批次已 `archived` | `409 {"error": "batch archived, terminal state"}` |
| R-F04 | API 层：断言失败（如抽样 fail 未闭环） | **`200`** + `passed=false` + 逐项 `checks`；**不是** 4xx/5xx —— 断言失败是可重试的业务结论，不是错误 |
| R-F05 | 断言失败后批次状态 | **不变**（实测：`200` 返回后批次仍为 `pending_release`，未写入 `release_decision`） |

---

## 5. 异常关闭 `close_incident()`（五条件）

`close_incident(incident, reinspection, capa_verified) -> CloseResult(can_close, missing)`，缺一即 `can_close=False`。API 层还额外补三项：`sn_not_linked`、`original_decision_evidence`、`containment_action`。

### 5.1 正常

| ID | 输入 | 预期 |
| --- | --- | --- |
| C-N01 | `owner='ZHL-QA'`；根因 + 纠正措施非空；复检 `result='pass'`；`capa_verified=True`；异常已关联 SN；有判定证据；临时措施非空 | `can_close=True, missing=()`，接口 `200` |

### 5.2 边界（单项缺失，实测 `missing` 原文）

| ID | 缺什么 | 输入 | 预期 `missing` |
| --- | --- | --- | --- |
| C-B01 | 责任人 | `owner=""` | `["owner"]`（叠加其他时一起出现） |
| C-B02 | CAPA 字段 | 根因或纠正措施为空串 | 含 `capa_fields` |
| C-B03 | 复检记录 | `reinspection=None` | 含 `reinspection_record` |
| C-B04 | 复检通过 | `reinspection.result='fail'` | 含 `reinspection_passed` |
| C-B05 | 已验证 | `capa_verified=False` | 含 `capa_verified` |
| C-B06 | 临时措施 | `containment_action` 为空 | 含 `containment_action`（API 层补） |

### 5.3 失败（叠加 + 状态守卫）

| ID | 场景 | 预期 |
| --- | --- | --- |
| C-F01 | 无复检 + 未勾验证 + 临时措施为空 | `409 {"error": "close_conditions_unmet", "missing": ["reinspection_record", "capa_verified", "containment_action"]}`（**一次全列**） |
| C-F02 | 复检已有但未勾验证、临时措施已填 | `409 … "missing": ["capa_verified"]` |
| C-F03 | 异常状态为 `new` | `409 {"error": "incident_not_closeable", "status": "new"}` |
| C-F04 | 异常状态为 `isolated` | `409 {"error": "incident_not_closeable", "status": "isolated"}` |
| C-F05 | 异常状态为 `assigned` | `409 {"error": "incident_not_closeable", "status": "assigned"}` |
| C-F06 | 异常状态为 `rejected` | `409 {"error": "incident_not_closeable", "status": "rejected"}` |
| C-F07 | 批次已 `archived` | `409 {"error": "batch archived, terminal state"}` |

### 5.4 CAPA 的「已验证」门禁（L3）

| ID | 场景 | 输入 | 预期 |
| --- | --- | --- | --- |
| C-V01 | `verified=True` 但无复检记录 | `POST /capa`，`verified=true` | `400 {"error": "capa_verified_requires_pass_reinspection"}` |
| C-V02 | `verified=True` 且复检 `fail` | 同上 | 同上 400 |
| C-V03 | `verified=True` 且复检 `pass` | 同上 | `200` |
| C-V04 | `verified=False` | 同上，先不勾 | `200`（允许先填字段，后补勾） |
| C-V05 | 异常 `pending_recheck` 态补勾 | 复检已有且 `pass` | `200`，无状态转移（只更新字段） |

---

## 6. 复检范围

### 6.1 正常

| ID | 输入 | 预期 |
| --- | --- | --- |
| RS-N01 | `scope_type="minimal"`，不传 `scope_sns` | 服务端自动填为该异常 `incident_sn_links` 的全部 SN；`scope_applied="minimal"` |
| RS-N02 | `scope_type="expanded"` 且 `scope_sns` 覆盖最小组件集 | `201`，`scope_applied="expanded"`；轨迹记 `scope_expansion=true` |

### 6.2 边界与失败

| ID | 输入 | 预期 |
| --- | --- | --- |
| RS-B01 | `scope_type="expanded"`，不传 `scope_sns` | `400 {"error": "scope_sns_required"}` |
| RS-B02 | `scope_sns` 未覆盖最小组件集 | `400 {"error": "scope_gap", "missing": [...]}` |
| RS-B03 | 未知 `scope_type` | `400 {"error": "unknown_scope_type", "scope_type": "..."}` |
| RS-B04 | 异常状态为 `new` | `409 {"error": "incident_not_reinspectable", "status": "new"}` |
| RS-B05 | 异常状态为 `closed` | `409 {"error": "incident_not_reinspectable", "status": "closed"}` |
| RS-B06 | `result="fail"`，异常在 `pending_recheck` | `201`；异常 → `rejected`；批次 → `rejected` |
| RS-B07 | **`result="fail"`，异常仍在 `assigned`** | **`409 {"error": "incident_transition_denied", "reason": "no transition incident:assigned -> rejected (skip/invalid)"}`** —— 必须先经 `pending_recheck` |
| RS-B08 | `result="pass"` | `201`，`passed=true`；异常与批次状态**不变**（关闭是后续人工动作） |
| RS-B09 | 复检写回 `verification_id` | 复检行独立落库，`module_inspections` 原值不变 |

---

## 7. 复检闭环结论（第 2 轮抽样）

**为什么存在**：抽样 `fail` → 隔离 → 异常处置 → 复检 → 关闭 → 待放行。放行断言②读的是「最新一轮抽样结论」（按 `sample_round` 取最大）。若无人回写第 2 轮，批次会**永久卡在断言②**。

`derive_remediation_conclusion(sample)` 判定一个 SN「已解除」的三条硬条件：

1. 该 SN 至少关联一个异常（判 fail 却无异常 = 闭环根本没走）；
2. 其关联异常**全部**处于 `closed` —— `rejected` / `scrapped` **不算解除**；
3. 存在 `result='pass'` 且 `scope_sns` 覆盖该 SN 的复检记录。

### 7.1 正常

| ID | 输入 | 预期 |
| --- | --- | --- |
| RC-N01 | 样本全部 `judge_result='pass'` | `result='pass'` |
| RC-N02 | 判 fail 的 SN 有异常且全部 `closed` + 有覆盖它的通过复检 | `result='pass'` |
| RC-N03 | 走完完整闭环（实测） | 新增 `sample_round=2, result='pass'`；批次 → `pending_release` |

### 7.2 边界与失败

| ID | 输入 | 预期 |
| --- | --- | --- |
| RC-B01 | 样本为空列表 | `result='fail'`，`reason='no sampled SN to conclude on'`（**空不等于通过**） |
| RC-B02 | SN 判 fail 但无任何关联异常 | `fail`，`reason` 含 `judged fail with no incident linked` |
| RC-B03 | SN 的异常停在 `pending_recheck` | `fail`，`reason` 含 `incident not closed: <id>(pending_recheck)` |
| RC-B04 | SN 的异常是 `rejected` | `fail`，`reason` 含 `incident not closed: <id>(rejected)` |
| RC-B05 | SN 的异常是 `scrapped` | `fail`，同上 |
| RC-B06 | 有 `closed` 异常但无通过复检覆盖 | `fail`，`reason` 含 `no passing reinspection covering this SN` |
| RC-B07 | 复检 `pass` 但 `scope_sns` 未覆盖该 SN | `fail`，同上 |
| RC-B08 | 复检覆盖该 SN，但复检属于**别的**异常 | `fail`（覆盖口径绑定 SN 自己的异常） |

### 7.3 API 层 `/recheck`

| ID | 场景 | 预期 |
| --- | --- | --- |
| RC-A01 | 批次 `pending_release`，闭环成立 | `200`，`written=true`，`sample_round=2`，随后放行断言②转 Pass |
| RC-A02 | 再调一次（幂等） | `200`，`written=false`，`skipped="already_passed"`（实测） |
| RC-A03 | 批次无任何抽样结论 | `200`，`written=false`，`skipped="no_sample_conclusion"` |
| RC-A04 | 闭环未成立 | `200`，`result="fail"` + `reason` 指名未解除的 SN，**不写任何结论行** |
| RC-A05 | 批次非 `pending_release` | `409 {"error": "batch_not_pending_release", "status": "..."}` |
| RC-A06 | 批次已 `archived` | `409 {"error": "batch archived, terminal state"}` |

---

## 8. API 守卫层

### 8.1 正常

| ID | 端点 | 输入 | 预期 |
| --- | --- | --- | --- |
| G-N01 | `POST /api/batches` | 合法 `rule_version_id`（published） | `201 {"status": "pending_intake"}` |
| G-N02 | `POST /api/intake` | 全部行以标准值提交 | `200 {"status": "confirmed"}`，批次推进 |
| G-N03 | `POST /api/batches/{id}/judge` | 全部 SN 已确认 | `200`（逐 SN 判定） |
| G-N04 | `GET /api/batches/{id}` | 已知 UUID | `200`（批次 + 逐 SN + 抽样结论 + 时间线） |
| G-N05 | `POST /api/batches/{id}/release` | 断言全过 + `approved_by` | `200 {"passed": true, "status": "released"}` |

### 8.2 边界

| ID | 端点 | 输入 | 预期 |
| --- | --- | --- | --- |
| G-B01 | `GET /api/batches/{id}` | 畸形 UUID `not-a-uuid` | `404 {"detail": {"batch_id": "not-a-uuid"}}`（**不 500**） |
| G-B02 | `GET /api/batches/{id}` | 不存在的合法 UUID | `404`（同上形状） |
| G-B03 | `POST /api/batches` | 未知 `rule_version_id` | `400 {"reason": "unknown rule version"}` |
| G-B04 | `POST /api/batches` | 规则为 `draft` / `disabled` | `400 {"reason": "rule version not published", "status": "draft"}` |
| G-B05 | `POST /api/intake` | 计划数量 `151` | `200` 但抽样 `rule_missing` → 批次隔离 + 自动建 `rule_missing / major / new` 异常；轨迹 `{"result": "Pass", "sample_result": "rule_missing"}` |
| G-B05b | `POST /api/intake` | **计划数量 `1`** | 同上（ISO 表最小 lot 区间是 2–8）：抽报 `rule_missing`、批次隔离 |
| G-B06 | `POST /api/intake` | 含 `SN-LOW-*`（Mock） | `200 {"status": "pending", "confidence": 0.62, "low_conf_sns": ["SN-LOW-001"]}`（批次 `confidence` = 各 SN 最小值） |
| G-B06b | `POST /api/intake` | 10 个 `SN-N-*`（Mock） | `200 {"status": "confirmed", "confidence": 0.851, "low_conf_sns": []}`（0.851 是这 10 个里最低的 `SN-N-008`） |
| G-B07 | `POST /api/intake` | 同时传 `raw_source` 与 `inspections` | `400 {"reason": "ambiguous_intake"}` |
| G-B08 | `POST /api/intake` | 行数 ≠ 9 列 | `400`（`incomplete_inspection`，前端也会先拦） |

### 8.3 失败（409 / 400 守卫）

| ID | 端点 | 场景 | 预期 |
| --- | --- | --- | --- |
| G-F01 | 任意写端点 | 批次已 `archived` | `409 {"error": "batch archived, terminal state"}`（实测 6 个端点一致） |
| G-F02 | `POST /api/batches/{id}/report` | `rejected` 批次 + `report_type=release` | `409 {"error": "rejected_batch_only_incident_report", "report_type": "release"}` |
| G-F03 | `POST /api/batches/{id}/report` | **`isolated` 批次 + `report_type=release`** | `409 {"error": "release_report_requires_released_batch", "status": "isolated", "hint": "..."}` |
| G-F04 | `POST /api/batches/{id}/judge` | 含未确认 SN | `400 {"detail": {"unconfirmed_sns": [...]}}` |
| G-F05 | `POST /api/intake` | 跨批同 SN | `400 {"reason": "cross_batch_sn_conflict", "error": "SN 'X' already recorded in a different batch"}` + 在被占用批次上自动建 `data_consistency / critical / new` 异常 |
| G-F06 | `POST /api/intake` | 同批重复 SN | `400 {"reason": "sn_already_intaked", "duplicate_sns": [...]}` |
| G-F07 | `POST /api/intake` | 批次不在 `pending_intake` | `409 {"error": "batch_not_pending_intake", "status": "...", "expected": "pending_intake"}` |
| G-F08 | `POST /api/intake/{id}/confirm` | 批次已离开 `pending_intake` | `409 {"error": "no_pending_confirmation", "status": "...", "expected": "pending_intake"}` |
| G-F09 | `POST /api/incidents` | 批次非 `isolated` | `409 {"error": "batch_not_isolated", "status": "..."}` |
| G-F10 | `POST /api/incidents` | `affected_sns` 含批外 SN | `400 {"error": "sn_not_found", "missing": [...]}` |
| G-F11 | `POST /api/incidents` | 引用了不存在的证据 id | `400 {"error": "evidence_not_found", "missing": [...]}` |
| G-F12 | `POST /api/incidents/{id}/isolate` | 异常非 `new` | `409 {"error": "incident_not_new", "status": "..."}` |
| G-F13 | `POST /api/incidents/{id}/assign` | 异常非 `isolated` | `409 {"error": "incident_not_assignable", "status": "..."}` |
| G-F14 | `POST /api/incidents/{id}/capa` | 异常非 `assigned` 且非 `pending_recheck` | `409 {"error": "incident_not_assigned", "status": "..."}` |
| G-F15 | `POST /api/batches/{id}/release` | 批次非 `pending_release` | `409 {"error": "batch_not_pending_release", "status": "..."}` |
| G-F16 | `POST /api/batches/{id}/reject` | 批次已是 `rejected` | `409 {"error": "batch_reject_not_allowed", "from_status": "rejected", "reason": "no transition batch:rejected -> rejected (skip/invalid)"}` |

### 8.4 AI 归因端点

| ID | 端点 | 场景 | 预期 |
| --- | --- | --- | --- |
| G-A01 | `POST /api/incidents/{id}/attribute`（不带 `decision`） | 正常 | `200 {"status": "awaiting_human", "hypotheses_source": "attribution", "evidence_validation": "skill", "hypotheses": [...]}` |
| G-A02 | 同上，再调一次 | 已有停驻运行 | `200`，仍 `awaiting_human`（不报错） |
| G-A03 | `POST /api/incidents/{id}/adopt` | `decision="adopt"` + `hypothesis_index=0` | `200`；`confirmed_root_cause` = **后端存储的假设原文**（前端传的文本被忽略） |
| G-A04 | 同上 | `decision="modify"` + `modified_cause` | `200`；`confirmed_root_cause` = 修订文本；轨迹 `root_cause_source="modified_hypothesis"` + `hypothesis_index` + 原假设原文 |
| G-A05 | 同上 | `decision="reject"` | `200`；`confirmed_root_cause` **不变**（不写业务字段） |
| G-A06 | 同上 | `decision="modify"` 但 `modified_cause` 为空 | `400 {"error": "modify_requires_text", ...}` |
| G-A07 | 同上 | `decision="adopt"` 但既无假设下标也无 `cause` | `400 {"error": "adopt_requires_cause", ...}` |
| G-A08 | 同上 | 未知 `decision` | `400 {"error": "bad_decision", "allowed": ["adopt", "reject", "modify", "defer"]}` |
| G-A09 | 同上 | 该异常**没有**停驻的归因运行 | `409 {"error": "no_parked_attribution", "hint": "..."}` |
| G-A10 | 同上 | 批次已归档 | `409 {"error": "batch archived, terminal state"}` |
| G-A11 | `POST .../attribute`（带 `decision`，单发模式） | 未知 incident UUID（Mock 无 fixture 键） | `200 {"hypotheses": [], "hint": "..."}`（**不是** 404/500） |

---

## 9. 汇总与转写建议

### 9.1 用例统计

| 对象 | 正常 | 边界 | 失败 | 小计 |
| --- | --- | --- | --- | --- |
| 判定 `judge()` | J-N01..13 | J-B01..14 | J-F01..07 | 34 |
| 判定证据结构 | §1.4 | — | — | 2 |
| 参数未启用 | J-X01..02 | — | — | 2 |
| 抽样 `iso_lookup`/`freeze` | S-N01..06 | S-B01..12 | S-F01..05 | 23 |
| 状态机 `transition()` | T-N01..09 | T-B01..05 | T-F01..07 | 21 |
| 放行 `assert_release()` | R-N01 | R-B01..12 | R-F01..05 | 18 |
| 关闭 `close_incident()` | C-N01 | C-B01..06 | C-F01..07 + C-V01..05 | 19 |
| 复检范围 | RS-N01..02 | RS-B01..09 | — | 11 |
| 复检结论 | RC-N01..03 | RC-B01..08 | RC-A01..06 | 17 |
| API 守卫 | G-N01..05 | G-B01..08 | G-F01..16 | 29 |
| AI 归因端点 | G-A01..04 | G-A05..09 | G-A10..11 | 11 |

**合计：正常 44 / 边界 87 / 失败 56 = 187 条针对性用例。**

### 9.2 转写建议

- **domain 纯函数单测**（`tests/domain/`，不依赖 DB/模型）：§1–§5、§7.1–7.2 可直接 `@pytest.mark.parametrize`。浮点断言用 `pytest.approx` 或容差比较（§1.4）。
- **API 集成**（`tests/api/`，SQLite `StaticPool` + `dependency_overrides`）：§6、§7.3、§8 需真实路由。脚手架参照 `tests/api/test_reinspections.py::_seed`（确定性 UUID + 规则/证据/复检行预置）与 `tests/e2e/test_demo_path.py::_client`（注入 `MockLLMClient`）。
- **API 测试必须显式注入 Mock**：`app.main` 会 `load_dotenv`，开发机的 `.env` 里有真实 key 时端点会**真的去调外部模型**（慢、依赖网络、引用的证据 id 会被 D2 闸门正当地拒掉）。两种做法任选：
  ```python
  os.environ["ZHL_LLM_MODE"] = "mock"                    # 全局
  app.dependency_overrides[incidents_router.get_llm] = lambda: MockLLMClient(_FIXTURES)   # 定点
  ```
- **边界值必须切 Mock 才能写死**：真模型给什么置信度不可预测（§0.4/§1 §2 的写死值都是 Mock 模式的）。

### 9.3 本文件相对旧版本的**更正**（实测差异）

| 旧表述 | 实测事实 | 影响 |
| --- | --- | --- |
| `ff` 界限写作 `[0.784, 0.816]` 但同表另一处写 `±0.02 → 0.78~0.82` | 闭区间是 **[0.784, 0.816]**；`0.78` 与 `0.82` 都 **Fail** | 直接用 0.78/0.82 当边界会误判 |
| `temp` 容差写作 `22.5~27.5`（当时按 ±10% 记） | 相对容差 `0.10` → **[22.5, 27.5]** 正确，但同表曾标 `24.90~25.10` | `24.90~25.10` 是**绝对** ±0.10 的算法，与实现不符 |
| `im` 分档边界 `12.0 / 14.0 / 15.5` 预期 `Pass` | 三者实测全 **Fail**（容差先卡） | 分档与容差是两把尺子，必须同时满足 |
| 「`assigned` 可报废 `scrapped`」 | 边存在但前置是 `scrap_approved`，不带前置条件时 `accepted=False` | 用例须带前置 |
| 关闭五条件缺失清单只列 5 项 | API 层实为 **8 项**（多 `sn_not_linked` / `original_decision_evidence` / `containment_action`） | 断言 `missing` 时要覆盖全 8 项 |
| 复检范围「结果 fail → 异常/批次转拒绝」未加限定 | 仅从 `pending_recheck` 成立；从 `assigned` 返回 **409** | 见 RS-B07 |
| 无第 2 轮抽样结论的用例 | 已补 §7（`materialize_remediation_conclusion` + `/recheck`） | 抽样 fail 批次能否放行的关键 |
| 无 `/api/batches/{id}/recheck` 端点 | 已存在（`app/api/release.py`） | 见 RC-A01..06 |
| 无「isolated 批次取放行报告」用例 | 实测 `409 release_report_requires_released_batch` | 见 G-F03 |
