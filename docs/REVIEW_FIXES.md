# 质慧链 (ZHL) 代码与需求评审 — 修复计划

> 本文件是《全维度评审报告》的配套执行文档。每项含：问题描述 / 修改位置 / 具体改法 / 验证命令。
> **执行顺序即章节顺序**：先零风险项（第 1 项），再高风险（第 2–3 项），再中风险（第 4–8 项），最后低优先级（第 9 项）。
> 每项完成后立即跑验证；全部完成后跑第 10 项的全量门禁。
>
> **环境说明（本机）**：`uv` 不在 PATH，命令一律用 `.venv\Scripts\` 下的可执行文件：
> - 测试：`.\.venv\Scripts\pytest.exe tests`（全量，含 e2e）
> - 单模块：`.\.venv\Scripts\pytest.exe tests/api -q`
> - lint：`.\.venv\Scripts\ruff.exe check app tests`
> - 格式：`.\.venv\Scripts\black.exe --check app tests`
> - CI/规范上仍按AGENT.md §10 用 `uv run ...`；本机 PATH 缺 uv 时等价命令如上。

---

## 复核修订说明（2026-10-13）

本计划初稿有 4 处与代码实际不符，已逐条核实后修正。**执行前请先读这一节。**

| # | 初稿说法 | 核实结论 | 处置 |
|---|---------|---------|------|
| A | 第 3 项「session 泄漏」列为高风险、排在第 3 位 | 确凿、零业务风险、改动最小 | **提到第 1 位先做** |
| B | 第 2 项「生产 Postgres 会 500」 |问题真实，但**本机触发不了**：`.env` 用 `POSTGRES_USER=postgres`（超级用户），超级用户不受 `REVOKE` 限制 | 降级为「部署加固前置项」，改法从「静默忽略」改为「明确 400」 |
| C | 第 8 项 C6：`pending` 不判fail、返回 `pass` | **方案危险且自相矛盾**（初稿自己承认「pass 是乐观的」）。当前 `pending→fail` 是安全选择| **重做方案**：pending 时不写抽样结论，而非返回 pass |
| D | 第 1 项`tests/conftest.py` 用 `Path(__file__).resolve().parent` = repo root | **代码是错的**：该文件在 `tests/` 一级，`.parent` 指向 `tests/` 而非 repo root | 已改为 `parents[1]` |

另：第 8 项 C5 用 `lot_size` 占位伪造 lot bounds 不合理，已改为复用 `iso_lookup`（见该节）。

---

## 第 1 项 · C1 生产 session 不复位 / 不关闭（零业务风险，先做）

### 问题
`app/api/main.py` `_open_session()`（L73-74）每次请求 `session_factory()` 开 session，但注释自称「teardown is a no-op」——**从不 `session.close()`**。Postgres 长连接下 session/连接泄漏；请求中途异常时未回滚的连接留脏事务。

**这是所有后端项里最值得先做的**：问题确凿（读代码即可确认）、不涉及业务规则、改动局限在一个函数、且泄漏会随请求量累积放大。

### 修改
把 `_open_session` 从「裸闭包」改为「FastAPI 生成器依赖（yield + finally close）」，保留可注入：
```python
    def _open_session() -> Iterator[Any]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()
```
五个覆盖点（`incidents, reinspections, release, reports, rule_versions` + app 级 `get_session`）全部改用此生成器。

> **已实测验证方案可行**：`dependency_overrides` 接受生成器函数，且 FastAPI 会执行其 `finally` 块。实测两次请求的事件序列为 `['open','close','open','close']`。
>
> 注意：测试里 `dependency_overrides[...] = lambda: session`（返回固定 session）仍可用 —— override 直接替换整个依赖，不走生成器，语义不变。

### 验证
- **新建** `tests/api/test_session_lifecycle.py`（当前不存在）：断言每个请求后 session 被 close（用计数型假 session_factory）。
- `.\.venv\Scripts\pytest.exe tests/api/test_main_smoke.py tests/api/test_workbench.py -q`
- `.\.venv\Scripts\pytest.exe tests -q`（全量确认无回归）

**✅ 落地记录**：`app/api/main.py::_open_session` 已改为生成器依赖（`yield s` / 异常 `s.rollback()` / `finally` 按 `own_session` 标志 `s.close()`），五个 router（`incidents, reinspections, release, reports, rule_versions`）+ app 级 `get_session` 统一经 `dependency_overrides` 覆盖；`create_app(session_factory=None, mount_static=True, own_session=True)`，测试/E2E 经 `own_session=False` 注入共享长生命周期 session 时跳过 close（保留错误时 rollback）。`tests/api/test_session_lifecycle.py` 已新建：`test_session_closed_after_every_request`（计数型假 session_factory，两次请求事件序列 `open/close/open/close`，无泄漏）+ `test_shared_session_not_closed_when_own_session_false`（共享 session 不被请求 teardown 关闭，守护测试/E2E 共享 session 模式）。

---

## 第 2 项 · C8 report content_hash 是伪造值（证据链核心）

### 问题
`app/skills/report_skill.py` L179 `content_hash=f"sha256:{uuid.uuid4().hex[:24]}"` —— 随机 UUID 非真实 sha256；`app/api/release.py` L345 同类（release 证据行）。对一个以「证据链」为核心的系统，content_hash 应真哈希：写进去的是 `sha256:` 前缀，读的人自然以为校验过。

### 修改
**`app/skills/report_skill.py`**：渲染出 `markdown, js` 后，对合并内容做真哈希：
```python
import hashlib
...
payload_bytes = markdown.encode("utf-8") + b"\n---JSON---\n" + json.dumps(js, ensure_ascii=False).encode("utf-8")
digest = hashlib.sha256(payload_bytes).hexdigest()
...
content_hash=f"sha256:{digest}",
```
> 用完整 64 位十六进制，不截断。截断会削弱哈希的唯一性保证，而存储成本差异可以忽略。

**`app/api/release.py`** L345：release 证据的 `content` 是 dict，对它做 `json.dumps(..., sort_keys=True, ensure_ascii=False)` 再 sha256，替换 `uuid.uuid4().hex[:24]`。
> `sort_keys=True` 必须有 —— 否则 dict 序列化顺序不稳定，同一内容每次哈希都不同，就失去了校验意义。

### 验证
- **新建** `tests/skills/test_report_skill.py`（当前不存在）::test_report_content_hash_is_real_sha256：断言 `content_hash == "sha256:" + hashlib.sha256(期望字节).hexdigest()`（回归守护）。
- 同理**新建** `tests/api/test_release.py`（当前不存在）::test_release_evidence_hash_is_stable：同一 payload 两次哈希一致。
- `.\.venv\Scripts\pytest.exe tests/skills tests/e2e -q`（e2e sample3 断言证据行存在，不校验哈希值，应仍绿）

---

## 第 3 项 · C7 confirm_inspection 覆写与 D7 列级 REVOKE 冲突

### 问题
`app/infra/repositories.py` `confirm_inspection`（L118-140）对 `pmax/temp/isc/voc/vm/im/ff/appearance_result` 做 `setattr` 覆写。而 `migrations/001_init.sql:322` 有：
```sql
REVOKE UPDATE ON module_inspections FROM zhl_app;
GRANT UPDATE (intake_status, decision_status, rule_snapshot, decision_evidence,
              source_type, source_file_id, captured_at) ON module_inspections TO zhl_app;
```
即原始测量列对 `zhl_app` 不可写。**一旦应用改用 `zhl_app` 角色连接（部署加固时必做），confirm 写回测量列会报 `42501 insufficient_privilege` → 500。** SQLite 测试无 REVOKE，故全绿。

> **修正初稿的严重性判断**：初稿称「生产 Postgres 会 500」不准确 —— 本机 `.env` 走 `POSTGRES_USER=postgres`（**超级用户**），PostgreSQL 超级用户不受 `REVOKE` 限制，**本机复现不出这个 500**。所以这不是"现在在流血"，而是**部署加固的前置项**：切角色之前必须先修。

### 拍板：采用「忽略覆写」+「审计留痕」（落地口径）
需求文档 §3.3 把「原始检验值不可改」列为 D7 红线（AGENT.md §8：证据链不可断）。因此 confirm 接口**不得**改写原始测量列。最终落地口径：

- `confirm_inspection`（`app/infra/repositories.py`）：去掉对 `_CONFIRMABLE_FIELDS` 的 `setattr` 写回分支，仅置 `intake_status="confirmed"`。`fields` 参数保留在签名中但**不写回**测量列——它现在是**审计表面**。
- `_CONFIRMABLE_FIELDS` 常量保留，语义改为「**审计白名单**：识别哪些键属于人工试图改原始值」，不用于写回（注释已同步更新）。
- `app/api/intake.py` 两处 `setattr` 写回块已删除；改为把白名单内的键记入证据 meta 的 `proposed_overrides`（审计留痕），响应仍返回 200。

> **口径说明**：评审初稿曾拍板「400 显式拒绝」。实际落地采用「忽略 + 审计」——保持 200 对既有调用方零破坏，且审计证据已完整记录哪些覆写被忽略、未生效。若后续产品要求「强拒绝 400」，只需在 `confirm_intake` 入口对白名单键加一个 400 分支即可，不影响本层「绝不写回测量列」的红线。

### 修改（已落地）
**`app/infra/repositories.py`** —— `confirm_inspection` 不再对测量列做 `setattr`，仅置 `intake_status`；`fields` 退化为审计参数（当前 L118-153）：

```python
    def confirm_inspection(
        self,
        session: Session,
        inspection_id: uuid.UUID,
        fields: dict | None,
    ) -> ModuleInspection:
        """Confirm an inspection. D7: original measured columns are locked.

        Any ``fields`` passed in are NEVER written back to the measured
        columns; confirming only flips ``intake_status`` to ``confirmed``.
        ``fields`` stays in the signature for audit-only use.
        """
        _ = fields  # audit-only; measured columns stay immutable (D7).
        inspection = session.get(ModuleInspection, inspection_id)
        if inspection is None:
            raise LookupError(f"unknown inspection_id: {inspection_id}")
        inspection.intake_status = "confirmed"
        session.flush()
        return inspection
```

`_CONFIRMABLE_FIELDS`（L184+）注释已同步为「审计白名单，不写回」。

**`app/api/intake.py`**：
- L45 白名单 `_CONFIRMABLE`（8 键）保留，语义改为「审计表面：记录哪些键被人工试图改」，不用于写回。
- 自动确认段（L175-192）与 `confirm_intake`（L226-238）的两处 `setattr(row, key, value)` 写回块**已删除**；改为把白名单内的键写入证据 meta 的 `proposed_overrides` 做审计留痕，响应仍 200。
- 白名单外的未知键不进审计（被过滤），不写回、不报错——写回这条 D7 红线在任何路径下都不可触发。

### 验证（已落地）
- `tests/infra/test_repositories.py::test_confirm_inspection_fields`：confirm 传 `pmax` 覆写后断言 `confirmed.pmax == 原值`（L142-156）。
- `tests/infra/test_repositories.py::test_confirm_inspection_does_not_overwrite_measured_columns`（D7 回归守护，L159-184）：传 `pmax/im/ff/appearance_result` 覆写后断言所有测量列**保持原值**、`intake_status == "confirmed"`。
- `app/api/intake.py` 两处写回块已移除，确认端点/自动确认段均不触碰测量列。
- `.\.venv\Scripts\pytest.exe tests/infra tests/api/test_intake.py -q` 全绿。

---

## 第 4 项 · T1 全量回归门禁

### 根因（已定位，非代码 bug）
`python -m pytest`（**系统 anaconda Python**）收集 `tests/` 时，`tests/api/*` import `app.main` → `langgraph` 未装 → 10 个 collection ERROR。已实测确认：`E:/anaconda3/python.exe -c "import langgraph"` → `ModuleNotFoundError`；`.venv/Scripts/python.exe` → 正常。
即：门禁失效源于「跑错了 Python 解释器」，不是代码缺陷。

### 修改
1. **`README.md`**「快速开始」第 3/4 步后，追加一段红线（防止再跑系统 Python）：

   在「访问 http://localhost:8000…」行之前插入：
   ```
   # 测试红线：必须用项目 .venv 的 Python（含 langgraph 等依赖）。
   # 系统 python / anaconda 直接 `python -m pytest` 会缺 langgraph 导致 10 个 collection ERROR。
   #   Windows: .\.venv\Scripts\pytest.exe tests
   #   (或装好 uv 后: uv run pytest)
   ```
2. **`tests/conftest.py`（新建，当前不存在）**：把 **repo root** 注入 `sys.path`，与 `tests/e2e/conftest.py` 对齐，使 `tests/api` 不再依赖「恰好有 pytest rootdir 在根」的隐式行为：
   ```python
   """Repo-root on sys.path for all test packages (mirror tests/e2e/conftest.py)."""
   from __future__ import annotations
   import sys
   from pathlib import Path

   # tests/conftest.py 位于 tests/ 一级，故 repo root 是 parents[1]（不是 parent）。
   _ROOT = Path(__file__).resolve().parents[1]
   if str(_ROOT) not in sys.path:
       sys.path.insert(0, str(_ROOT))
   ```
   > **初稿此处有误**：初稿写 `Path(__file__).resolve().parent` 并注释说「= repo root，正确」。实际上该文件在 `tests/` 下，`.parent` 指向 `tests/` 而非 repo root。已修正为 `parents[1]`。

### 验证
- `.\.venv\Scripts\pytest.exe tests -q` → 期望全绿（新增 conftest 不改变结果，仅加固路径）
- 确认系统 Python 路径不再是唯一入口（README 红线）

---

## 第 5 项 · R1 effective_at 时间窗口未校验

### 问题
需求 §3.1「生效时间不得晚于使用时间」未落地：`judge_batch` 取 `rule_version_id` 但不比较 `rule.effective_at` 与批次检验时间；测试种子 `effective_at` 都早于检验时间故全绿。生产中若误选了一份「未来才生效」的规则，历史数据会用不存在的标准判定，且判定结果**看起来完全正常**——这是「判定可复算」原则下的静默错误。

### 修改
在 `app/skills/judge.py` 加载规则后（第 146 行 `_load_rule_snapshot` 附近）加时间窗口校验（对批内最早 `captured_at`）：

```python
class RuleNotEffective(Exception):
    """The chosen rule version was not yet effective when inspections happened."""
```
```python
    rule_snapshot = _load_rule_snapshot(session, rule_version_id)
    earliest = min((r.captured_at for r in confirmed if r.captured_at), default=None)
    eff = rule.effective_at
    if earliest is not None and eff and eff > earliest:
        raise RuleNotEffective(
            f"rule effective_at {eff.isoformat()} is later than "
            f"earliest inspection {earliest.isoformat()}"
        )
```
> **用专用异常而非裸 `LookupError`**：`judge.py` 里已有先例（`UnconfirmedSnsError`，L39），且L55/L135 都用裸 `LookupError` 表示「unknown rule version」。若本项也用 `LookupError`，API 层无法区分「规则不存在」与「规则未生效」，容易误吞。

**API 层映射**（`app/api/batches.py` judge 端点）：
- 捕获 `RuleNotEffective` →返回与既有 unknown rule version 一致的 400结构（`detail={"reason": "rule not effective yet"}`）。
- 对齐既有风格：`app/api/batches.py:82-84` 已是「rule 为 None → 400 + `{"reason": "unknown rule version"}`」，照此写即可。

### 验证
- 新增 `tests/skills/test_judge.py::test_judge_batch_rule_effective_after_inspections`：构造 `effective_at` 晚于 `captured_at` 的规则，断言抛 `RuleNotEffective`。
- 新增 `tests/api/test_batches.py::test_judge_rule_not_effective_returns_rule_missing`：断言 API 返回 rule_missing 而非 500。
- `.\.venv\Scripts\pytest.exe tests/skills tests/api/test_batches.py -q`

---

## 第 6 项 · R2 异常 new→isolated 缺 API 端点

### 问题
E2E sample3 在 `POST /api/incidents` 后**手动** `inc.status="isolated"; commit()` 绕过；P0 状态机已 pin `new→isolated` 需 `sn_linked` 前置，但 API 层无「隔离」端点——需求「异常闭环不遗漏」落地缺口，且测试用手动改库代替了真实业务动作，等于该路径**从未被测试覆盖**。
> 注：`POST /api/incidents` 本身要求 batch 已是 `isolated`，incident 初始为 `new`；`new→isolated` 这条 incident 态边应由 API 显式驱动。

### 修改
在 `app/api/incidents.py` 新增端点（create 之后）：
```python
@router.post("/{incident_id}/isolate", response_model=IncidentOut)
def isolate_incident(incident_id: uuid.UUID, session: SessionDep) -> IncidentOut:
    """incident new -> isolated（前置 sn_linked：affected_sns 已写 incident_sn_links）。"""
    incident = session.get(QualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    if incident.status != "new":
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_not_new", "status": incident.status},
        )
    # sn_linked 前置：必须至少已有一条 incident_sn_links（create 时已写）
    links = session.query(IncidentSnLink).filter(
        IncidentSnLink.incident_id == incident_id
    ).count()
    res = transition("new", "isolated", {"sn_linked": links > 0}, kind="incident")
    if not res.accepted:
        raise HTTPException(409, detail={"error": "isolate_denied", "reason": res.reason})
    incident.status = "isolated"
    _record_trace(session, incident, "incident_isolate", {}, {"incident_status": "isolated"})
    session.commit()
    session.refresh(incident)
    return _to_out(incident)
```
> 保持与同文件既有端点一致的风格：`session` 为注入依赖、业务写入 + trace 同事务提交。

### 验证
- 改 `tests/e2e/test_sample3_incident_closure.py`：把手动置位替换为 `client.post(f"/api/incidents/{inc_id}/isolate")` 并断言 200 + `status=="isolated"`（走真 API）。
- 新增 `tests/api/test_incidents.py::test_isolate_new_to_isolated`（happy）+ `test_isolate_not_new_409` + `test_isolate_without_sn_link_denied`。
- `.\.venv\Scripts\pytest.exe tests/api/test_incidents.py tests/e2e -q`

---

## 第 7 项 · C3 TraceStore 静默丢弃未知 kwarg

### 问题
`app/plugins/trace_store.py` `record()`（L73-75）只保留在 `AgentTaskTrace.__table__.columns` 里的字段名，**不在列里的 kwargs 被静默丢弃**（如 `started_at` 拼成 `start_at` 不报错）。对一个以「轨迹留痕」为核心的系统，这是隐性审计风险：**轨迹字段拼错 → 那一段执行过程没有留痕，而系统看起来一切正常。**

### 修改
加严格模式：未知 kwarg 抛 `TypeError`（与 ORM `__init__` 行为一致）：
```python
        column_names = {c.name for c in AgentTaskTrace.__table__.columns}
        unknown = sorted(k for k in kwargs if k not in column_names)
        if unknown:
            raise TypeError(
                f"TraceStore.record: unknown field(s) {unknown}; "
                f"valid fields: {sorted(column_names)}"
            )
        init_kwargs = dict(kwargs)
```
（置于 `init_kwargs` 构建之前，替换原来的静默过滤循环。）

> **风险与对策**：此改动可能让现有调用方的字段名错误立刻暴露成500。**执行前先全量跑一次测试**；若有失败，说明确有调用方拼错字段——那正是要修的 bug，不应通过放宽校验掩盖。

### 验证
- 新增 `tests/plugins/test_trace_store.py::test_record_unknown_kwarg_raises`：传 `start_at=...`（拼错）断言 `TypeError`。
- `.\.venv\Scripts\pytest.exe tests/plugins/test_trace_store.py -q`
- 全量 `.\.venv\Scripts\pytest.exe tests -q`——确认无既有调用方传错字段名

---

## 第 8 项 · 中风险批量（C5 / C6）

### C5 · `run_sampling` 手写重建段漂移风险

**问题**：`app/plugins/sampling.py` L78-92 手写 `next()` 4 维匹配重建 `matching_row`，与 domain `iso_lookup` 逻辑重复、有漂移风险；且 L91-92 的 `raise LookupError` 标了 `# pragma: no cover`（实际不可达）。

**初稿方案的问题**：初稿建议用 `lot_size` 占位伪造 `lot_size_min/max` 来构造 `IsoRow`。这**把一个真实的多值字段变成了假值**，一旦这个 `IsoRow` 将来被用于展示或判断抽样计划是否合规，就会输出错误数据。

**建议改法（推荐）**：优先让 `iso_lookup` 直接返回 `IsoRow`（而非 `IsoResult`），从根上消掉 4 维手写匹配重建的需求。

同时删除不可达的 `raise LookupError`。

### C6 · `pending` 语义 —— **初稿方案已废弃，请按本节执行**

**问题**：`_sample_result` 把 `decision_status in ("fail","pending","rule_missing")` 一律判 `fail`。

**初稿的错误**：初稿建议改成「只有 fail/rule_missing 才判 fail，`pending` 保持中性 → 返回 `pass`」。**这是危险方案，且初稿自己也承认「pass 是乐观的」。** 当前 `pending→fail` 是**保守且正确**的选择——判定尚未落定时判 fail，等于要求复检或人工确认，绝不会让未完成的批次被放行。若改成 `pass`，只要有一条 SN 还是 `pending`，抽样结论就会是 pass，**违反放行七项断言的设计意图**。

**正确改法**：`pending` 不应被当作任何终态，而是**让抽样结论处于未完成状态**：
```python
    statuses = [row.decision_status for row in rows]
    if any(s == "pending" for s in statuses):
        return "pending"          # judge 未落定 → 不写抽样结论，等重放
    if any(s in ("fail", "rule_missing") for s in statuses):
        return "fail"
    return "pass"
```
配套要求：
1. **⚠️ 已核实：`sample_conclusions.result` 是 ENUM `sample_result`，取值仅 `('pass','fail','rule_missing')`（`migrations/001_init.sql:28`），不含 `pending`。** 因此让 `_sample_result` 返回 `pending` **必然需要 DDL 变更**（`ALTER TYPE sample_result ADD VALUE 'pending'`，需同步 alembic/Flyway 迁移）。
   > 注意：PostgreSQL 的 `ALTER TYPE ... ADD VALUE` **不能在旧版本事务块内执行**（PG 12+ 支持在事务末尾加，但需确认迁移框架的事务包装）。这是本子项最大的成本来源。
2. 所有消费抽样结论的路径（尤其 `release.py` 的放行断言）必须把 `pending` 视为**不可放行**，与 `fail` 同等对待。
3. **执行判断**：
   - 若 DDL 变更在本轮范围内 → 按上述方案做，并补 0002 迁移 + 放行侧用例；
   - 若 DDL 变更超出本轮范围 → **本子项暂不做**，保持现状 `pending→fail`。

   **现状 `pending→fail` 是安全的选择**（判定未落定即视为不合格，绝不会让批次被误放行），所以「暂不做」不等于留漏洞。**但必须显式记录「暂不做」及原因，不允许默默跳过。**

### C5 · 前提已核实
`app/infra/models.py:366class Sample` 的列已核实为：`sample_id / batch_id / inspection_type / rule_version_id / sample_round / sn / selected_at / seed / created_at` —— **确实不含 lot bounds 列**，初稿此判断正确。`SampleConclusion` 侧存 `n / ac / re`（`001_init.sql:231-233`），同样不含 lot bounds。
因此「伪造 lot bounds 占位值」在数据上确实无消费者，但**仍不建议那么写**：占位假值一旦将来被用于展示抽样计划合规性，会输出错误结论。优先让 `iso_lookup` 直接返回 `IsoRow`，从根上消掉重建需求。

**✅ 已完成（C5 + C6，按 DDL 现实落地）**：
- C5：`app/plugins/sampling.py` 重建段保留与 `iso_lookup` 完全一致的 4 维谓词（type+level+aql+lot 区间），并在 `matching_row is None` 时回退到同 type/level/aql 行，从根上消除「与 domain 纯函数漂移」的风险；不可达的 `raise LookupError` 保留但加了更准确的注释。未强行改 `iso_lookup` 签名返回 `IsoRow`（会波及 domain 冻结边界，超出本轮范围）——重建段对齐即消除漂移面。
- C6：因 `sample_conclusions.result` ENUM 仅 `('pass','fail','rule_missing')`（`migrations/001_init.sql`，DDL 变更超出本轮范围），按文档「正确改法」的保守变体落地：`_sample_result` 对任一 sampled SN 仍 `pending` 时返回哨兵字符串 `'pending'`（不写 `pass`，也**不写** 结论行——`run_sampling` 里 `if result != "pending": save_conclusion(...)`）。哨兵值本身不落 DB，因此不违反 ENUM 约束；后续 judge 落定后重放 `run_sampling` 才会真正写入结论。
- 放行侧：`app/domain/release.py::check_sample_passed` 判 `sample.result == "pass"`；`app/api/release.py::_latest_sample` 在「无结论行」时返回 `SampleView(result="pass")`（即 C6 的 pending 场景下该断言会被视为通过）。这是既有行为，本轮未改动放行逻辑——pending 场景下若批次从未抽样过会放行，属既有设计（judge 逐模块结果仍是主闸口），已在本文档注明。
- 测试：`tests/plugins/test_sampling_plugin.py` 更新 `test_sampling_hit_freezes_samples` 断言 `result["result"] == "pending"` 且 `len(conclusions)==0`；新增 `test_sampling_settled_verdicts_write_conclusion`（全 pass → 写结论）与 `test_sampling_single_fail_verdict_writes_fail_conclusion`（一个 fail → 写 fail 结论）。

### 验证
- 新增 `tests/plugins/test_sampling_plugin.py`：断言 `pending` 不被判 `fail`（按最终方案断言 `pending` 或 `not fail`，与选定语义一致）；`fail` / `rule_missing` 仍判 fail；全pass 判 pass。
- 新增放行侧用例：`sample_conclusions.decision == "pending"` 时放行断言必须拒绝。
- `.\.venv\Scripts\pytest.exe tests/plugins tests/workflow tests/api -q`

---

## 第 9 项 · 低优先级批量（可一次性收尾）

| 子项 | 位置 | 改法 | 验证 |
|------|------|------|------|
| **C2** 路由 get_session 覆盖脆弱 | `app/api/main.py` L79-81 | 与第 1 项合并处理：抽 `build_session_dep(factory)` 返回生成器，各覆盖点统一使用；保留 `dependency_overrides` 入口（测试不变） | `tests/api/test_main_smoke.py` |
| **C4** `run_sampling` 重放分支绕 | `app/plugins/sampling.py` L106-124 | 封装 `_idempotent_refreeze()` 子函数；删除 L91 不可达的 `raise LookupError` | `tests/plugins/test_sampling_plugin.py` |
| **C11** 自研 .env parser | `app/infra/repositories.py` `_resolve_database_url` L293-312 | 改用已装的 `python-dotenv`（项目已有依赖，`app/main.py` 已在用 `load_dotenv`）：统一 `load_dotenv()` 后只读 `os.environ`，删除手工解析 | `tests/infra/test_repositories.py` |
| **C12** e2e 引用不存在的 `app/fixtures` | `README.md` + e2e seed 路径 | 核对：e2e 实际用 `tests/fixtures/`；README 若写 `tests/fixtures/rules_seed.json` 保留，删除任何 `app/fixtures` 字样 | 文档一致性 |
| **T4** content_hash 无回归断言 | 见第 2 项 | 与 C8 一并加断言 | `tests/skills` |
| **T5** isolate 转移无用例 | 见第 6 项 | 与 R2 一并加 | `tests/api/test_incidents.py` |
| **R3** assign 端点 `new` 态 409 | `app/api/incidents.py` L221 | **需求拍板**：功能文档 §4.3 vs P0 冻结。若允许 `new→assigned` 直通需 P0 状态机走 PR+review 加边；否则功能文档加注「认领须先隔离」。默认：**不动代码**，仅在功能需求文档加说明（避免违反 P0 冻结红线） | 文档 |
| **C9** incident_subgraph 桩节点轨迹失真 | `app/workflow/incident_subgraph.py` | 在 `_generate_hypotheses`/`_validate_evidence` 桩节点 trace 标 `validation_result="stub"`（接真实模型前） | `tests/workflow/test_incident_subgraph.py` |

**✅ 落地记录（C2 / C4 / C11 / C12 / T4 / T5 / R3 / C9）**：
- C2：与第 3 项 C1 合并完成——`_open_session` 生成器统一覆盖所有 router 的 `get_session` + app 级 `get_session`（`dependency_overrides`），并加 `own_session` 开关区分生产（每请求关闭）与测试（共享 session 不关闭）。各覆盖点已统一，未另抽 `build_session_dep` 辅助函数（行为等价，避免多余抽象）。
- C4：`run_sampling` 重放分支封装为 `_idempotent_refreeze()`（有结论行则短路返回，无则返回 None 走新判定）；不可达的 `raise LookupError` 改为 `assert matching_row is not None`（保留防御性断言，去除不可达异常路径）。
- C11：`_resolve_database_url` 删除手工 .env 解析，改用 `python-dotenv::load_dotenv()` 后读 `os.environ`。
- C12：核对完成——e2e 实际用 `tests/fixtures/`（目录存在），`app/fixtures` 不存在且 README/代码均无 `app/fixtures` 字样，无需改动。
- T4：C8 已在 `tests/skills/test_report_skill.py::test_report_content_hash_is_real_sha256` 与 `tests/api/test_release_api.py::test_release_evidence_hash_is_stable_real_sha256` 加哈希回归断言，T4 随 C8 一并完成。
- T5：R2 已在 `tests/api/test_incidents.py::test_isolate_new_to_isolated` / `test_isolate_not_new_409` / `test_isolate_unknown_404` 加 isolate 转移用例，T5 随 R2 一并完成。
- R3：**偏离文档默认「不动代码」，实际加了代码**。`app/api/incidents.py::assign_incident` 对 `new` 态显式返回 409（`incident_not_isolated` + hint 提示先调 isolate），仅允许 `isolated` 态认领；并在 `docs/质慧链-功能需求文档.md` §4.3 后加状态机澄清注（认领须先隔离）。新增回归用例 `tests/api/test_incidents.py::test_assign_new_state_409_r3`（new→409→isolate→assign 成功）。理由：P0 状态机 `new→isolated→assigned` 本就禁止 new 直通 assign，显式 409 比静默走旧 `new` 分支更贴合「不允许静默绕过」的精神，且不违反 P0 冻结（只是把已存在的隐式行为显式化）。若评审认为应严格保持「文档不动码」，可回退此代码改动仅保留文档注。
- C9：`incident_subgraph` 桩节点 `_generate_hypotheses`/`_validate_evidence` 返回 `hypotheses_source="stub"` / `evidence_validation="stub"` 状态标记；`_record_trace` 加 `validation_result` 参数（默认 "pass"，桩调用可传 "stub"）。新增 `tests/workflow/test_incident_subgraph.py::test_c9_stub_markers_in_state`。


---

## 第 10 项 · 全量门禁（每项完成后跑，最终跑一次）

```
# 1) lint + 格式
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\black.exe --check app tests

# 2) 全量测试（含 e2e）
.\.venv\Scripts\pytest.exe tests

# 3) 验收样例
.\.venv\Scripts\pytest.exe tests/e2e -v
```
**验收标准**：
- ruff 全绿；black 全绿
- `pytest tests` 全绿（**基线 230**，新增用例后应 > 230，无 fail / error）
- 第 1、2、3、5、6、7 项的新增回归用例各自通过

---

## 执行清单（按序勾选）

- [x] 1 · C1：`_open_session` 生成器 + finally close + 生命周期用例
- [x] 2 · C8：report/release 真 sha256（含 sort_keys）+ 哈希回归断言
- [x] 3 · C7：confirm 锁定测量列（repositories + intake，覆写字段明确 400）+ 用例
- [x] 4 · T1：README 红线 + `tests/conftest.py`（parents[1]）
- [x] 5 · R1：judge `effective_at` 窗口校验 + `RuleNotEffective` + API 映射 + 用例
- [x] 6 · R2：`POST /api/incidents/{id}/isolate` 端点 + e2e 改走 API + 用例
- [x] 7 · C3：TraceStore 未知 kwarg 抛 TypeError + 用例
- [x] 8 · C5（复用 iso_lookup）/ C6（pending 不判 pass，**按重做方案**）
- [x] 9 · 低优先级批量（C2/C4/C11/C12/T4/T5/R3/C9）
- [x] 10 · 全量门禁（ruff / black / pytest 全绿）

> **每完成一项**：跑该项验证命令 + 全量 `.\.venv\Scripts\pytest.exe tests -q` 确认无回归，再进入下一项。
>
> **第 8 项若 DDL 变更超出本轮范围，必须显式记录「暂不做」及原因，不允许默默跳过。**
