# 质慧链 · 针对性测试数据（正常 / 边界 / 失败）

日期：2026-10-14
定位：基于**现有实现**的确定性纯函数与 API 守卫设计。按测试对象分组，每组覆盖**正常 / 边界 / 失败**三类输入，给出「输入值 → 预期输出」。可直接转写为 `tests/domain/*` 单测或 API 集成用例。
依据：`app/domain/{judge,sampling,release,state_machine}.py`；`app/api/{batches,intake,release,reports,incidents}.py`；`migrations/001_init.sql` enum 约束；`AGENT.md` §3.3（domain 纯函数单测含边界值 + 非法路径，不依赖 DB/模型）。
姊妹文档：`docs/test/01-full-chain-test-data.md`（全链路连通数据）。

> **边界语义（判定核心，`judge.py`）**：所有容差区间为**闭区间** `lower = std*(1-tol)`, `upper = std*(1+tol)`；`lower ≤ 实测 ≤ upper` 判 Pass，**等于边界判 Pass**。
> **分档**：`im` 落声明区间 `[lower, upper]` 判 Pass（闭区间）。
> 每条数据只改一个变量，其余参数取标准值（`temp=25.0, isc=10.5, voc=48.0, vm=40.0`），保证单一故障维度。

---

## 1. 判定引擎 `judge()`（五重校验，固定顺序）

判定顺序：① 必填字段 → ② 功率范围 → ③ 参数容差 → ④ 电流分档 → ⑤ 外观。全部 Pass 才 `result=Pass`，任一 Fail 即 `Fail`。纯函数（不读库/不取时/不调模型）。

> 默认标准值（规则 §3.1）：`nameplate=550.0, power_tol=0.03`；`temp 25.0±0.10, isc 10.5±0.05, voc 48.0±0.05, vm 40.0±0.05, im 13.0±0.05, ff 0.80±0.02`；分档 `A=[12.0,14.0], B=[14.0,15.5]`。

### 1.1 正常（每类各 1，`result=Pass`）

| ID | 场景 | 输入（变化字段） | 预期 |
| --- | --- | --- | --- |
| J-N01 | 全中值 | `pmax=550.0, im=13.0, 外观=pass, 六参全标准值` | Pass（5 项全过） |
| J-N02 | 功率偏高 | `pmax=566.5`（=上限 550×1.03） | Pass（闭区间含上限） |
| J-N03 | 功率偏低 | `pmax=533.5`（=下限 550×0.97） | Pass |
| J-N04 | 电流 B 档 | `im=15.0`（落 B 档 `[14.0,15.5]`） | Pass |
| J-N05 | 多 SN 整批 | 10 SN 各自微扰（见 `acceptance_normal`） | 每 SN Pass |

### 1.2 边界（`result=Pass`，钉住闭区间语义）

| ID | 参数 | 边界点 | 预期 | 说明 |
| --- | --- | --- | --- | --- |
| J-B01 | pmax | 下限 533.5 | Pass | = 下限判 Pass（spec §2.3） |
| J-B02 | pmax | 上限 566.5 | Pass | = 上限判 Pass |
| J-B03 | pmax | 533.4（下限 −0.1） | **Fail**（对照组） | 越下限 |
| J-B04 | pmax | 566.6（上限 +0.1） | Fail | 越上限 |
| J-B05 | im 分档 | 12.0（A 档下界） | Pass | 闭区间含下界 |
| J-B06 | im 分档 | 14.0（A/B 档交界） | Pass | 落两档公共界，命中即过 |
| J-B07 | im 分档 | 15.5（B 档上界） | Pass |
| J-B08 | im 分档 | 15.6（B 档上界外） | Fail | 越档（样例 3 触发点，`SN-I-OODB`） |
| J-B09 | ff | 下界 0.784 / 上界 0.816 | Pass | ff 容差 0.02 |
| J-B10 | temp | 下界 22.5 / 上界 27.5 | Pass | 容差 0.10 |

### 1.3 失败（单一故障维度，`result=Fail`，指名未过项）

| ID | 故障 | 输入 | 预期未过校验 |
| --- | --- | --- | --- |
| J-F01 | 功率越下限 | `pmax=533.4` | `power_range` |
| J-F02 | 功率越上限 | `pmax=567.0` | `power_range` |
| J-F03 | 电流越档 | `im=16.5` | `current_band` |
| J-F04 | 温度越界 | `temp=30.0`（>27.5） | `parameter_tolerances`（名 temp） |
| J-F05 | Voc 越界 | `voc=55.0`（>50.4） | `parameter_tolerances`（名 voc） |
| J-F06 | 外观 fail | `appearance_result=fail` | `appearance` |
| J-F07 | 必填缺失 | `sn` 或 `grade` 或六参任一 `None/""` | `required_fields`（missing 列名） |
| J-F08 | 分档缺表 | 规则 `current_bands=[]` 或 `im` 不落在任何档 | `current_band`（matched_band=None） |

> 校验顺序钉住（固定序）：`J-F07`（缺字段）会先于其他项拦截；多项同时异常时，`checks` 返回全部 5 项的逐条结果（`Decision.checks`），不只首个失败。

### 1.4 参数未启用（`enabled=false` 跳过）

| ID | 场景 | 输入 | 预期 |
| --- | --- | --- | --- |
| J-X01 | ff 停用 | 规则 `ff.enabled=false` + 实测 `ff=0.50` | `parameter_tolerances` 跳过 ff，判 Pass |
| J-X02 | 多参停用 | `temp/ff.enabled=false` | 仅 isc/voc/vm/im 参与容差 |

---

---

## 2. 抽样 `iso_lookup` / `freeze_sample`（D9 无静默默认放行）

`iso_lookup(inspection_type, inspection_level, aql, lot_size, iso_table)` 查预置 ISO 行；查不到 → 判规则异常（禁止按默认值放行，与架构 §10 一致）。冻结样本按 `seed=batch_id` 确定性重放。

### 2.1 正常

| ID | 输入（type, level, aql, lot） | 预期 |
| --- | --- | --- |
| S-N01 | `iv, normal, 1.0, lot=10` | 命中 9–15 行：`n=13, ac=0, re=1` |
| S-N02 | `iv, normal, 0.65, lot=5` | 命中 2–8 行：`n=8, ac=0, re=1` |
| S-N03 | `iv, normal, 1.5, lot=20` | 命中 16–25 行：`n=20, ac=1, re=2` |

### 2.2 边界（lot 区间端点）

| ID | lot | 命中行 | 预期 |
| --- | --- | --- | --- |
| S-B01 | 8 | 2–8（含上界） | `n=8` |
| S-B02 | 9 | 9–15（含下界） | `n=13` |
| S-B03 | 15 | 9–15 | `n=13` |
| S-B04 | 16 | 16–25 | `n=20` |
| S-B05 | 25 | 16–25 | `n=20` |
| S-B06 | 50 | 26–50 | `n=32` |
| S-B07 | 90 | 51–90 | `n=50` |

### 2.3 失败（D9：缺表 → 无静默放行）

| ID | 输入 | 预期 |
| --- | --- | --- |
| S-F01 | lot=1（低于 2 下界） | `iso_lookup` 返 None → 判 `rule_missing`，**不**默认放行 |
| S-F02 | lot=151（超 150 上界） | None → `rule_missing` |
| S-F03 | 未预置的 `(type,level,aql)` 组合 | None → `rule_missing`（如 `visual, normal, 1.5, lot=200`） |
| S-F04 | 冻结后重复冻结（幂等重放） | 同一 seed 选中相同 SN 集，不重复写入 |

> **与放行断言 ⑦ 联动**：放行时 `release.py` 用预置 `full` 行（`aql=0.65, lot=1..100`）构造 `ReinspectionScopeIn`，`iso_lookup` 解析非 None 才过（见 §4 R-N07）。

---

---

## 3. 状态机 `transition()`（批次 10 态 / 异常 7 态）

`transition(current, target, preconditions, kind)` 表驱动；前置条件未满足 → `accepted=False` + 指名缺失。纯数据表（`state_machine.py`）。

### 3.1 正常（合法边 + 前置满足）

| ID | 机 | 转移（前置） | 预期 |
| --- | --- | --- | --- |
| T-N01 | 批次 | `draft → pending_intake`（∅） | accepted |
| T-N02 | 批次 | `pending_judge → pending_release`（`all_pass, sample_pass`） | accepted |
| T-N03 | 批次 | `pending_recheck → pending_release`（`recheck_passed, measures_effective`） | accepted |
| T-N04 | 批次 | `pending_release → released`（`assertions_passed, human_approved`） | accepted |
| T-N05 | 批次 | `released → archived`（∅，D6a 空前置） | accepted |
| T-N06 | 异常 | `new → isolated`（`sn_linked`） | accepted |
| T-N07 | 异常 | `isolated → assigned`（`owner_assigned`） | accepted |

### 3.2 边界（前置临界 / 终态不可达）

| ID | 转移 | 说明 | 预期 |
| --- | --- | --- | --- |
| T-B01 | `rejected → archived`（∅） | 终态自动归档（D6a） | accepted |
| T-B02 | `archived → *`（任意） | 归档为终态，无出边 | rejected（no transition） |
| T-B03 | 批次 `pending_judge → pending_release` 但 `sample_pass=False` | 前置缺 1 项 | rejected（指名 sample_pass） |
| T-B04 | 异常 `pending_recheck → rejected`（∅） | 复检失败分支 | accepted |

### 3.3 失败（非法 / 前置缺）

| ID | 转移 | 预期 |
| --- | --- | --- |
| T-F01 | 批次 `new → isolated` 前置缺 `sn_linked` | rejected（precondition not met） |
| T-F02 | 批次 `draft → released`（跳态） | rejected（no transition 边） |
| T-F03 | 未知 `kind="foo"` | rejected（unknown kind） |
| T-F04 | 异常 `new → assigned`（直跳，P0 表无此边） | rejected（钉住不得直跳，`test_sample3` 断言） |

---

---

## 4. 放行断言 `assert_release()`（七项逐条）

`assert_release(batch, sample, incidents, isolated_sns, evidence, capa, reinspection_scopes, iso_table, required_sns)` 返回逐条结果，绝不裸 bool。任一项 Fail 即 `passed=False`，逐项 reason 指名（spec §2.4）。

### 4.1 正常（七项全过，`passed=True`）

| ID | 各入参取值 | 预期 |
| --- | --- | --- |
| R-N01 | 批次 `pending_release`；`sample=pass`；无 open critical；无隔离 SN；证据非空；CAPA 全 verified；复检覆盖齐 + ISO 可解析 | 7/7 Pass，`passed=True` |

### 4.2 边界（逐项单独阻断，逐项指名）

每项单独造一个 Fail，其余 6 项保持 Pass，验证「逐条返回 + 指名未满足项」：

| ID | 阻断项 | 输入变化 | 预期 reason |
| --- | --- | --- | --- |
| R-B01 | ① state | 批次 `status=pending_recheck`（非 pending_release） | `state` Fail，「expected pending_release」 |
| R-B02 | ② sample_passed | `sample.result=fail` | `sample_passed` Fail |
| R-B03 | ③ no_open_critical | 存在 open critical 异常（`severity=critical, status=isolated`） | `no_open_critical_incident` Fail（列异常 id） |
| R-B04 | ④ no_isolated_sn | `isolated_sns=["SN-X"]` 非空 | `no_isolated_sn` Fail（列 SN） |
| R-B05 | ⑤ all_evidence | `evidence=[]` 或某行 `evidence_id` 空 | `all_evidence_present` Fail |
| R-B06 | ⑥ all_capa_verified | 某 CAPA `verified=False` | `all_capa_verified` Fail（列异常 id） |
| R-B07 | ⑦ 复检覆盖 | open 异常无 scope row / scope 未覆盖 required SN / ISO 返 None | `reinspection_scope_covered` Fail（指名缺失 SN 或「ISO」） |

### 4.3 失败（多阻断叠加 + D7）

| ID | 场景 | 预期 |
| --- | --- | --- |
| R-F01 | ②④⑦ 同时 Fail | `passed=False`，`checks` 含 3 项 Fail 各带 reason |
| R-F02 | D7 拒绝批 only incident 报告 | `rejected` 批次调 `report(report_type=release)` → 409（见 §6） |

> **⑦ 两种模式**：传 `required_sns`（权威，来自 `incident_sn_links`）→ 缺口逐项指名；不传（Phase 0 向后兼容）→ 自引用模式（H-3 已知限制，open 异常无 scope row 不阻断，需传 `required_sns` 才捕获）。

---

---

## 5. 异常关闭 `close_incident()`（五硬约束）

`close_incident(incident, reinspection, capa_verified)` 返回 `can_close` + `missing` 元组。缺一即 `can_close=False`。

### 5.1 正常（五条件全满足）

| ID | 输入 | 预期 |
| --- | --- | --- |
| C-N01 | `owner=ZHL-QA`；根因+纠正措施非空；复检记录 `result=pass`；`capa_verified=True` | `can_close=True, missing=()` |

### 5.2 边界（单项临界）

| ID | 输入 | 预期 missing |
| --- | --- | --- |
| C-B01 | `owner=""` | 含 `owner` |
| C-B02 | 根因或措施空串 | 含 `capa_fields` |
| C-B03 | `reinspection=None` | 含 `reinspection_record` |
| C-B04 | `reinspection.result=fail` | 含 `reinspection_passed` |
| C-B05 | `capa_verified=False` | 含 `capa_verified` |

### 5.3 失败（多项叠加 + 归档守卫）

| ID | 输入 | 预期 |
| --- | --- | --- |
| C-F01 | 全部缺失 | `missing` 含 owner/capa_fields/reinspection_record/capa_verified（一次全指名，M-5） |
| C-F02 | 批次已归档后补勾 capa | `409 batch archived, terminal state`（D8，M-8） |

---

---

## 6. API 守卫层（B3 / D8 / D7 / 跨批冲突 / 404）

路由层错误响应（`AGENT.md` B3 结构化 detail）。

### 6.1 正常

| ID | 端点 | 输入 | 预期 |
| --- | --- | --- | --- |
| G-N01 | `POST /api/batches` | 合法 rule_version_id + 字段 | `201 status=pending_intake` |
| G-N02 | `POST /api/batches/{id}/judge` | 全 confirmed | `200`（逐 SN 判定） |
| G-N03 | `GET /api/batches/{id}` | 已知 UUID | `200`（批次 + 时间线） |

### 6.2 边界（UUID 守卫 B3）

| ID | 端点 | 输入 | 预期 |
| --- | --- | --- | --- |
| G-B01 | `GET /api/batches/{id}` | 畸形 UUID（非 UUID 串） | `404`（不 500，`test_batches_uuid_guard`） |
| G-B02 | `POST /api/batches` | 未知 `rule_version_id` | `400 unknown rule version` |
| G-B03 | `POST /api/intake` | 低置信（conf<0.8） | `status=pending` + `low_conf_sns` |

### 6.3 失败（409 / 400 守卫）

| ID | 端点 | 场景 | 预期 |
| --- | --- | --- | --- |
| G-F01 | `POST /api/batches/{id}/report` | 批次已 `archived` | `409`（D8 `require_not_archived`） |
| G-F02 | `POST /api/batches/{id}/reject` | 批次已 `archived` | `409`（D8） |
| G-F03 | `POST /api/batches/{id}/reject` | 未知 batch UUID | `404` |
| G-F04 | `POST /api/batches/{id}/report` | `rejected` 批次 + `report_type=release` | `409`（D7 仅 incident） |
| G-F05 | `POST /api/intake` | 跨批同 SN | `400 cross_batch_sn_conflict`（+ 自动建 data_consistency 异常） |
| G-F06 | `POST /api/batches/{id}/judge` | 含未确认 SN | `400 unconfirmed_sns=[…]`（A4） |
| G-F07 | `POST /api/incidents/{id}/attribute` | LLM fixture 无该 incident 键 | `200 {hypotheses:[], hint}`（非 404/500，spec §6 Q3） |

---

---

## 7. 汇总表（ID 一览）

| 对象 | 正常 | 边界 | 失败 |
| --- | --- | --- | --- |
| 判定 `judge()` | J-N01..05 | J-B01..10 | J-F01..08（+ J-X01..02 未启用） |
| 抽样 `iso_lookup`/`freeze` | S-N01..03 | S-B01..07 | S-F01..04 |
| 状态机 `transition()` | T-N01..07 | T-B01..04 | T-F01..04 |
| 放行 `assert_release()` | R-N01 | R-B01..07 | R-F01..02 |
| 关闭 `close_incident()` | C-N01 | C-B01..05 | C-F01..02 |
| API 守卫 | G-N01..03 | G-B01..03 | G-F01..07 |

**合计**：正常 24 / 边界 33 / 失败 29 = 86 条针对性用例。

### 转写建议

- **domain 纯函数单测**（`tests/domain/`，不依赖 DB/模型）：§1–§5 可直接转 pytest 参数化；每个对象一条 `@pytest.mark.parametrize` 覆盖正常/边界/失败三组。
- **API 集成**（`tests/api/`，SQLite `StaticPool` + `dependency_overrides`）：§6 需真实路由，沿用 `test_release_reject.py`/`test_incident_attribute.py` 的 `_seed` 脚手架（确定性 UUID + 规则/证据/复检行预置）。
- **与夹具对应**：§1/§2/§4 的边界值已部分钉在 `tests/fixtures/acceptance_*.json` 与 `tests/domain/test_judge.py`、`tests/domain/test_sampling.py`、`tests/domain/test_release.py`；本文件是其**数据设计说明 + 补盲清单**（未覆盖项如 J-F08 分档缺表、R-B07 ⑦ 三种失败分支、G-F07 未知 incident 为空假设可补用例）。
- 本文件与 `docs/test/01-full-chain-test-data.md` 互补：01 是「链路拓扑 + 逐步请求体」，02 是「单对象 正常/边界/失败 输入→输出矩阵」。
