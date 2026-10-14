# 质慧链 · 业务链路连通测试数据（全链路）

日期：2026-10-14
定位：基于**现有实现**（非愿景）设计的测试数据。覆盖五段主链 + 异常子链的**所有合法业务链路**，用于全链路连通性验证（E2E / 手工走查 / 演示脚本均可用）。
依据：`docs/superpowers/specs/2026-10-10-zhl-system-spec.md` §2.2/§2.5/§2.6；`app/domain/{judge,state_machine,release}.py`；`app/api/*` 路由；`tests/fixtures/acceptance_*.json`。
姊妹文档：`docs/test/02-targeted-test-data.md`（正常/边界/失败三类针对性数据）。

> **约定**
> - 所有数据与现有夹具/种子**同源**：规则 `JH-550W v1`（`rules_seed.json` / 4 个 acceptance 夹具共用），ISO 表 `iso2859_1_aql.json`。
> - 状态词表（DDL enum，`migrations/001_init.sql`）：
>   - 批次 `batch_status`：`draft / pending_intake / pending_judge / pending_release / isolated / disposing / pending_recheck / rejected / released / archived`
>   - 异常 `incident_status`：`new / isolated / assigned / pending_recheck / closed / rejected / scrapped`
>   - `trigger_type`：`rule_fail / sample_fail / appearance_manual / data_consistency / rule_missing`
>   - `severity`：`critical / major / minor`
>   - `evidence_type`：`source_file / extraction_result / human_confirmation / decision_snapshot / task_trace / report_snapshot`
>   - `report_type`：`release / incident`
> - 幂等键（规则版本标识）：`P-DEFAULT+JH-550W+1`。
> - 功率边界：铭牌 550.0 × (1±0.03) → **下限 533.5 / 上限 566.5**；闭区间，等于边界判 Pass。
> - 电流分档：`[12.0,14.0]=A`，`[14.0,15.5]=B`。

---

## 1. 全链路拓扑（现有可走通的合法路径）

主链（批次 10 态机）：

```
draft ──待入场──> pending_intake ──判定全Pass+抽样Pass──> pending_release
                                                          └──放行断言全通过+人工批准──> released ──报告──> archived
   判定任一Fail/数据冲突/规则缺失
        └──> isolated ──指派责任人+时限──> disposing ──措施确认──> pending_recheck
                                                          ├──复检通过+措施有效──> pending_release
                                                          └──复检失败──> rejected ──处置报告──> archived
   任意非归档 ──人工拒绝──> rejected
```

异常子链（异常 7 态机）：

```
new ──SN关联──> isolated ──责任人+时限──> assigned ──复检记录──> pending_recheck
        └──────────────────> scrapped（报废审批）
                                          ├──关闭五条件全满足──> closed
                                          └──复检失败──> rejected
```

> **批次-异常联动**：`close` 端点把异常转 `closed` 时，仅当该批次**全部**异常处于终态才把批次从 `pending_recheck` 推进 `pending_release`（`test_closure.py` 钉住）。

---

## 2. 链路清单（每条链路的「数据 → 端点序列 → 终态」）

> 端点路径均相对当前实现（`app/api/*`）；「种子」指本数据预先插入 DB 的前置状态，「端点序列」是驱动该状态的 API 调用。

| # | 链路名 | 触发数据特征 | 端点序列（关键调用） | 批次终态 | 异常终态 | 夹具锚点 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | 正常放行 | 10 SN 全 Pass | `POST /batches` → `POST /intake` → `POST /intake/{task}/confirm` → `POST /batches/{id}/judge` → `POST /batches/{id}/release` → `POST /batches/{id}/report(release)` | `archived`（经 `released`） | 无异常 | `acceptance_normal` |
| C2 | 功率边界 Pass | `pmax=533.5`（=下限） | 同 C1（2 SN 小批） | `archived` | 无异常 | `acceptance_boundary` |
| C3 | 功率边界 Fail | `pmax=533.4`（<下限） | `POST /batches` → `POST /intake` → `judge`（→`isolated`） | `isolated` | 自动/人工建异常 | `acceptance_boundary`（对照组） |
| C4 | 异常隔离复检→放行 | `im=16.5` 越档 | `POST /incidents` → `/{id}/isolate` → `/{id}/assign` → `/{id}/reinspections(pass)` → `/{id}/capa(verified)` → `/{id}/close` → `POST /batches/{id}/release` → `report(release)` | `archived`（经 `pending_release`/`released`） | `closed` | `acceptance_incident` |
| C5 | 异常复检失败→拒绝 | 复检 `result=fail` | `POST /incidents` → `isolate` → `assign` → `reinspections(fail)` → `POST /batches/{id}/reject` → `report(incident)` | `archived`（经 `rejected`） | `rejected` | `acceptance_incident` 变体 |
| C6 | 异常关闭五条件逐条缺失 | 缺 owner / 缺 CAPA 字段 / 缺复检 / 未验证 / 缺原始证据 | `POST /incidents/{id}/close`（反复 409，`detail.missing` 逐条指名） | 维持 `pending_recheck` | `pending_recheck` | `test_closure.py` |
| C7 | 多异常批次的批次联动门禁 | 批次含 2 只异常 | `close` 第 1 只 → 批次仍 `pending_recheck`；`close` 第 2 只 → 批次 `pending_release` | `pending_release` | `closed`×2 | `test_closure.py` |
| C8 | 归档后写操作守卫（D8） | 批次已 `archived` | `POST /batches/{id}/report` / `.../capa` 补勾 / 任意改状态 → 409 `batch archived, terminal state` | `archived`（只读） | — | `test_closure.py`（capa 归档守卫） |
| C9 | 跨批次同 SN 一致性（D5/D17） | 同 `SN-SHARED` 两批 | `POST /intake`（第二批摄入共享 SN）→ 400 `cross_batch_sn_conflict` + 自动建 `data_consistency` critical 异常 | 第二批维持 `pending_intake` | 自动 `new`（critical） | `acceptance_consistency` |
| C10 | 人工拒绝（任意非归档） | 批次 `pending_release`/`isolated`… | `POST /batches/{id}/reject` → D7 生成 `incident` 报告 + D6a 自动归档 | `archived`（经 `rejected`） | — | 本分支新增端点 |
| C11 | 归因三选一（D9/D10） | 隔离批次 + 归因 fixture | `POST /incidents/{id}/attribute`（decision=adopt/reject/defer + cause）→ 写 `confirmed_root_cause`（仅 adopt） | 不变 | 不变 | 本分支新增端点 |

**连通性目标**：C1–C4 为 spec §6.1 四组样例对应的可复现主链；C5–C8 覆盖异常子链的失败/缺失/守卫/联动分支；C9–C11 覆盖一致性守卫与本分支新增的人工拒绝 / AI 归因端点。**11 条链路 = 现有全部合法业务路径。**

---

## 3. 共用种子数据

### 3.1 规则版本（`quality_rule_versions`，幂等键 `P-DEFAULT+JH-550W+1`）

与 4 个 acceptance 夹具 `rules` 块完全一致（同源），用于所有链路：

```json
{
  "project_id": "P-DEFAULT", "model": "JH-550W", "version_no": 1,
  "nameplate_power": 550.0, "power_tolerance": 0.03,
  "parameter_standards": {
    "temp": {"std_value": 25.0, "tolerance": 0.10, "enabled": true},
    "isc":  {"std_value": 10.5, "tolerance": 0.05, "enabled": true},
    "voc":  {"std_value": 48.0, "tolerance": 0.05, "enabled": true},
    "vm":   {"std_value": 40.0, "tolerance": 0.05, "enabled": true},
    "im":   {"std_value": 13.0, "tolerance": 0.05, "enabled": true},
    "ff":   {"std_value": 0.80, "tolerance": 0.02, "enabled": true}
  },
  "current_bands": [
    {"lower": 12.0, "upper": 14.0, "grade": "A"},
    {"lower": 14.0, "upper": 15.5, "grade": "B"}
  ],
  "sample_plan": {"inspection_type": "iv", "inspection_level": "normal", "aql": 1.0},
  "idempotency_key": "P-DEFAULT+JH-550W+1"
}
```

**判定边界推导**（`judge.py` 闭区间，测试数据据此取点）：

| 参数 | 标准值 | 容差% | 下限 | 上限 | 边界取点 |
| --- | --- | --- | --- | --- | --- |
| pmax（功率） | 550.0 | 0.03 | **533.5** | **566.5** | `533.5` Pass / `533.4` Fail（§2.3 边界语义） |
| temp | 25.0 | 0.10 | 22.5 | 27.5 | 取 `25.0`（中间） |
| isc | 10.5 | 0.05 | 9.975 | 11.025 | 取 `10.5` |
| voc | 48.0 | 0.05 | 45.6 | 50.4 | 取 `48.0` |
| vm | 40.0 | 0.05 | 38.0 | 42.0 | 取 `40.0` |
| im（同时受分档约束） | 13.0 | 0.05 | 12.35 | 13.65 | A 档内取 `13.0`；越档取 `16.5` |
| ff | 0.80 | 0.02 | 0.784 | 0.816 | 取 `0.80` |

电流分档（`check_current_band`）：`im ∈ [12.0,14.0]`→A、`[14.0,15.5]`→B；`im=16.5` 越档 → `current_band` 判 Fail（样例 3 的触发点）。

### 3.2 ISO 抽样表（`iso_sampling_rows`，驱动放行断言 ⑦ 的 `iso_lookup`）

预置行（`iso2859_1_aql.json` 节选，`inspection_level=normal`，`aql=1.0`）：

| inspection_type | lot 区间 | n | ac | re |
| --- | --- | --- | --- | --- |
| iv | 2–8 | 8 | 0 | 1 |
| iv | 9–15 | 13 | 0 | 1 |
| iv | 16–25 | 20 | 1 | 2 |
| iv | 51–90 | 50 | 2 | 3 |

> E2E 异常复检路径（C4/C5）会额外预置一条 `inspection_level=full`、`aql=0.65`、`lot_size_min=1,max=100`、`n=2/ac=0/re=1` 的行（见 `test_sample3_incident_closure.py::_seed_batch`），供放行断言 ⑦ 的 `iso_lookup` 解析；**链路 C4/C5 的种子必须包含该行**，否则 item ⑦ 判 `rule_missing`。

### 3.3 基础检验记录模板（`module_inspections`，各 SN 一行）

以 C1 正常批为例（10 SN 全 Pass），其余链路在此基础上改单个字段：

| sn | pmax | im | ff | appearance | 用途 |
| --- | --- | --- | --- | --- | --- |
| SN-N-001 | 550.0 | 13.0 | 0.80 | pass | C1 正常 |
| SN-N-008 | 547.5 | 13.0 | 0.80 | pass | C1 正常（功率偏低但仍 ≥533.5） |
| SN-N-009 | 553.0 | 13.0 | 0.80 | pass | C1 正常 |
| SN-B-LOW | 533.5 | 13.0 | 0.80 | pass | C2 边界 Pass |
| SN-B-BELOW | 533.4 | 13.0 | 0.80 | pass | C3 边界 Fail |
| SN-I-OK | 550.0 | 13.0 | 0.80 | pass | C4/C5 同批通过件 |
| SN-I-OODB | 550.0 | **16.5** | 0.80 | pass | C4/C5 电流越档触发件 |
| SN-SHARED | 550.0 | 13.0 | 0.80 | pass | C9 跨批冲突件 |

> 其余电参数一律取标准值（§3.1 边界表「取」列）：`temp=25.0, isc=10.5, voc=48.0, vm=40.0`。仅改动目标 SN 的目标字段，保证每条链路单一变量。

### 3.4 确定性 ID（可复现 / 外键引用）

沿用 `test_sample3_incident_closure.py` 的确定性 UUID（E2E-only，不入共享库）：

```python
BATCH   = UUID("ab000000-1111-2222-3333-444444444401")   # 批次
RULE    = UUID("ab000000-1111-2222-3333-444444444402")   # 规则版本
EV1..EV3 = UUID("ab000000-1111-2222-3333-444444444403..05")  # 证据行
CONC1   = UUID("ab000000-1111-2222-3333-444444444406")   # 抽样结论
INSP_OK   = UUID("ab000000-0000-0000-0000-000000000001") # 检验记录 ID（按 SN 派生）
INSP_OODB = UUID("ab000000-0000-0000-0000-000000000002")
```

证据 `evidence_type` 与链路对应：`decision_snapshot`（判定）、`extraction_result`（归因/抽取）、`human_confirmation`（放行/确认）、`report_snapshot`（报告）。归因 fixture 的短标签 `EV-ATT-001/002/003` 由 E2E 的 `_EvidenceStore` 别名到 `EV2/EV3/EV1`，使 D2 证据门保留可解析的假设（§C11）。

---

## 4. 各链路逐步数据与请求体

> 每步给出：调用、请求体（关键字段）、预期响应、DB 状态变化。省略脚手架（seed/fixture 加载）。

### C1 正常放行（`acceptance_normal`，10 SN）

1. `POST /api/batches`  
   `{"batch_no":"B-NORMAL-001","project_id":"P-DEFAULT","model":"JH-550W","rule_version_id":"<RULE>","planned_quantity":10}`  
   → `201 {status:"pending_intake"}`。批次 `draft→pending_intake`。
2. `POST /api/intake`  
   `{"batch_id":"<BATCH>","source_type":"IV_CSV","inspections":[ …10 行 SN-N-001..010… ]}`（字段见 §3.3）  
   → 自动确认（conf≥0.8）或返回 `status=pending`+`low_conf_sns`。
3. `POST /api/intake/{task_id}/confirm`  
   `{"action":"confirm"}`（task_id = str(batch_id)）→ 整批 `intake_status=confirmed` + `human_confirmation` 证据。
4. `POST /api/batches/{id}/judge`  
   → 10/10 Pass，`decision_status=pass`；批次 `pending_intake→pending_judge`（图内）再→`pending_release`（全 Pass + 抽样 Pass）。
5. `POST /api/batches/{id}/release`  
   `{"approved_by":"ZHL-ENG"}` → 7 项断言全 Pass，`{passed:true, status:"released"}`；批次 `pending_release→released` + 放行证据 + 轨迹。
6. `POST /api/batches/{id}/report`  
   `{"report_type":"release"}` → `201` 报告 v1；批次 `released→archived`（D6a）。

**终态**：批次 `archived`；无异常；三层审计（trace `release`/`report_api` + 证据 + 报告行）齐备。

### C2 功率边界 Pass（`acceptance_boundary`，2 SN）

同 C1 但 `SN-B-LOW.pmax=533.5`（=下限）→ 判 Pass；批次到 `released→archived`。对照组 `SN-B-BELOW=533.4` 单独走 C3。验证 spec §2.3「等于下限判 Pass」。

### C3 功率边界 Fail（对照组）

`SN-B-BELOW.pmax=533.4` → `power_range` 判 Fail → 批次 `pending_judge→isolated`（`any_fail`）。后续可接 C4 或 C10。

### C4 异常隔离复检→放行（`acceptance_incident`，核心样例 3）

前置：批次 `B-INCIDENT-001` `isolated`，`SN-I-OODB.im=16.5` 越档 + 证据 `EV1/EV2/EV3` + ISO `full` 行。

1. `POST /api/incidents`  
   `{"batch_id":"<B1>","trigger_type":"rule_fail","severity":"major","affected_sns":["SN-I-OODB"],"evidence_ids":["<EV1>"]}` → `201`，异常 `new`。
2. `POST /api/incidents/{id}/isolate` → `200 status=isolated`（SN 已关联）。
3. `POST /api/incidents/{id}/assign`  
   `{"owner":"ZHL-QA","due_at":"2026-06-05T00:00:00+00:00","containment_action":"isolate SN-I-OODB from lot"}` → 异常 `assigned`；批次 `isolated→disposing`。
4. `POST /api/incidents/{id}/reinspections`  
   `{"inspection_id":"<INSP_OODB>","pmax":550.0,"temp":25.0,"isc":10.5,"voc":48.0,"vm":40.0,"im":13.0,"ff":0.80,"result":"pass","executor":"ZHL-QA","executed_at":"2026-06-02T00:00:00+00:00","scope_type":"minimal"}` → `201 passed:true`；复检记录 + 异常 `verification_id`。
5. `POST /api/incidents/{id}/capa`  
   `{"confirmed_root_cause":"cell mismatch -> Impp drift","corrective_action":"replace lead cell","verified":true}` → L3 门禁（已有 pass 复检）通过；批次 `disposing→pending_recheck`。
6. `POST /api/incidents/{id}/close`  
   → 五条件全满足（owner / CAPA 字段 / 原始证据 / 复检记录 / 已验证）→ 异常 `pending_recheck→closed`；批次 `pending_recheck→pending_release`（仅当全异常终态）。
7. 预置 `SampleConclusion`（`result=pass`）供放行断言 ②；`SN-I-OODB.intake_status` 恢复 `confirmed`（§9.8 #3）。
8. `POST /api/batches/{id}/release` → 7 项全 Pass（含 ⑦ 复检覆盖）→ `released`。
9. `POST /api/batches/{id}/report {report_type:"release"}` → 报告 + `archived`。

**终态**：批次 `archived`，异常 `closed`。三层审计含 `attribution`/`release`/`report_api`。

### C5 异常复检失败→拒绝（C4 变体）

同 C4 至第 4 步，但 `reinspections` 用 `result:"fail"` + `pmax:540.0` → 异常 `pending_recheck→rejected`、批次 `pending_recheck→rejected`。随后：

- `POST /api/batches/{id}/reject {reason:"复检不通过，拒绝交付","rejected_by":"ZHL-QA"}` → D7 生成 `incident` 处置报告 + D6a `rejected→archived`；**或**
- `POST /api/incidents/{id}/close` → `409 incident_not_closeable`（rejected 异常不可关闭，M-7 行锁钉住）。

**终态**：批次 `archived`（经 `rejected`），异常 `rejected`。

### C6 异常关闭五条件逐条缺失（`test_closure.py`）

对同一异常反复调 `POST /api/incidents/{id}/close`，每次只补齐一项，`409 detail.missing` 逐条指名：

| 缺失项 | 触发前置 | `detail.missing` 含 |
| --- | --- | --- |
| owner | 跳过 assign | `owner` |
| CAPA 字段 | capa 传空串 | `capa_fields` |
| 复检记录 | 未调 reinspections | `reinspection_record` |
| 未验证 | `verified=false` | `capa_verified` |
| 原始证据 | 创建时 `evidence_ids=[]` | `original_decision_evidence` |
| 临时措施 | assign 时 `containment_action=null` | `containment_action` |

一次全缺（脚手架直写 `pending_recheck`）→ `missing` 同时含上述全部（M-5）。

### C7 多异常批次的批次联动门禁

批次含 2 只异常（`SN-I-OODB` + `SN-I-OK`）：
1. close 第 1 只 → 批次**仍** `pending_recheck`（还有未关闭异常）；
2. close 第 2 只 → 批次 `pending_release`（全终态）。

验证 close 的「全异常终态才推进批次」语义（Review Focus #2）。

### C8 归档后写操作守卫（D8，只读终态）

批次 `archived` 后：
- `POST /api/batches/{id}/report` → `409`（`require_not_archived`）；
- `POST /api/incidents/{id}/capa`（pending_recheck 补勾）→ `409 batch archived, terminal state`；
- 任意改状态端点 → 409。

**终态**：`archived` 不变，写操作全部被守卫拦下。

### C9 跨批次同 SN 一致性（D5/D17，`acceptance_consistency`）

1. 批次 A 摄入 `SN-SHARED`（正常）；
2. 批次 B 摄入同 `SN-SHARED` → `POST /api/intake` 命中仓库层 `CrossBatchSnConflict` → `400 {reason:"cross_batch_sn_conflict"}`；
3. 自动路径挂 `data_consistency` critical 异常到**被占用批次 A**（§8.1，不产生孤儿异常）；归档批次跳过自动建异常。

**终态**：批次 B 维持 `pending_intake`；两批次均不可放行（一致性异常 open critical → 断言 ③ 阻断）。

### C10 人工拒绝（任意非归档 → rejected，本分支端点）

`POST /api/batches/{id}/reject {reason,rejected_by}`：
- 状态机 `rejected` 边（`human_rejected` 前置，任意非归档态可达）→ D7 `incident` 报告（`ReportSkill.generate`）→ D6a `auto_archive_if_terminal` 自动归档 → D1 轨迹 + 单事务 commit。
- D8 已归档 → `409`；未知 batch → `404`。

**终态**：批次 `archived`（经 `rejected`），`incident` 报告 v1。

### C11 归因三选一（D9/D10，本分支端点）

`POST /api/incidents/{id}/attribute {decision, cause}`：
- `decision=adopt` → 写 `incident.confirmed_root_cause=cause`（必须填 cause，否则 400）；
- `decision=reject/defer` → 不写根因；
- LLM fixture 无该 incident 键 → `200 {hypotheses:[], hint:"…"}`（非 404/500，spec §6 Q3）；
- D2 门：假设 `evidence_ids` 须能在 `FileEvidenceStore` 解析，否则被丢弃（无证据假设不入列）。

**终态**：批次状态不变；归因轨迹留痕；adopt 时根因已写。

---

## 5. 走查顺序建议（演示 / CI）

推荐一条「主链 + 异常分支 + 守卫」的连贯演示剧本（每个链路独立起一个 SQLite 会话，互不干扰）：

```
# 1. 正常主链（§C1）——证明五段主链能走通
C1 正常放行 → 看 7 项断言 + 报告 + 归档

# 2. 边界对照（§C2/§C3）——证明判定闭区间语义
C2 533.5 Pass  vs  C3 533.4 Fail

# 3. 异常闭环（§C4/§C5）——证明异常子链双向出口
C4 复检通过→放行   vs   C5 复检失败→拒绝

# 4. 守卫与联动（§C6–§C8）——证明门禁逐条有效
C6 五条件逐条缺失  →  C7 多异常联动  →  C8 归档只读

# 5. 一致性 + 新增端点（§C9–§C11）
C9 跨批同 SN 冲突  →  C10 人工拒绝  →  C11 归因三选一
```

**复现命令**（演示入口，`AGENT.md` §5.2 要求从零可复现）：

```bash
# 全量 E2E / 验收样例（含 C1/C2/C3/C4 对应测试）
uv run pytest tests/api/test_e2e_acceptance.py tests/e2e/ -v

# 手工走查单条链：起 PG 容器后用 uvicorn 起应用，按 §4 端点序列 curl
docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl -c "SELECT status FROM delivery_batches;"
```

> CI 建议按「C1→C4 主链 / C5 失败链 / C6-C8 守卫 / C9-C11 专项」四个 suite 分组，每 suite 独立 SQLite 会话，互不污染。

---

## 6. 与现有夹具的对应关系

| 本数据链路 | 现有夹具 / 测试 | 说明 |
| --- | --- | --- |
| C1 | `tests/fixtures/acceptance_normal.json` + `tests/api/test_e2e_acceptance.py`（样例一） | 10 SN 正常放行 |
| C2 | `tests/fixtures/acceptance_boundary.json`（样例二，`SN-B-LOW`） | 功率下限 Pass |
| C3 | `tests/fixtures/acceptance_boundary.json`（`SN-B-BELOW`，对照组） | 功率下限 −0.1 Fail |
| C4/C5 | `tests/fixtures/acceptance_incident.json` + `tests/e2e/test_sample3_incident_closure.py` / `test_closure.py` | 电流越档异常闭环 |
| C6 | `tests/e2e/test_closure.py::test_close_names_each_unmet_condition` 等 | 五条件缺失指名 |
| C7 | `tests/e2e/test_closure.py::test_close_gates_on_all_incidents_terminal` | 多异常联动 |
| C8 | `tests/e2e/test_closure.py::test_capa_blocked_after_batch_archived` | 归档只读守卫 |
| C9 | `tests/fixtures/acceptance_consistency.json` + `tests/e2e/test_sample4_consistency.py` | 跨批同 SN 一致性 |
| C10 | 本分支 `tests/api/test_release_reject.py` | 人工拒绝端点 |
| C11 | 本分支 `tests/api/test_incident_attribute.py` | 归因三选一端点 |

> 本文件是**数据设计说明**（供 E2E / 手工 / 演示引用），不替代 `tests/` 内的可执行用例。C1–C9 的测试实现已存在于 `tests/api/test_e2e_acceptance.py` 与 `tests/e2e/`；C10/C11 为本分支新增，测试在 `tests/api/`。
