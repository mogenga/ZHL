# 提示词：ZHL 前端可用性改造（P0 解阻断）

> 用途：整段粘贴给执行方 AI。项目根 `D:\jsai\ZHL`。

## 背景

前端 10 个单HTML 页（`app/static/`：index / intake / batch / incidents / incident / recheck / release / report / trace / state）+ `style.css` + `common.js`，已验证通过。

**铁律不得破坏**：零构建、零 CDN、零框架、零 mock（未实现能力标 TODO，不造假数据）、API 唯一数据源、三态齐全、不引新依赖。

**现状 = 不可用**，三个根因：

1. **首页建批次必失败**。`app/api/batches.py:65` 要求 `QualityRuleVersion`（`app/infra/models.py:134`）的真实 UUID，但库里无种子数据（`scripts/` 只有 `start-pg.ps1`），也无接口能拿到合法 ID。建批次失败 → 后续全链条进不去。
2. **6 个页面首屏都是空 UUID 输入框**（batch/incident/recheck/release/report/trace），靠人肉粘贴 ID 进入；后端无 `GET /api/batches`，故首页标 TODO 不做列表。
3. **流程无串联**，除 intake 外无任何 next 跳转。

## 已核实的路由（勿凭记忆改）

POST `/api/batches`(batches.py:65) · GET `/api/batches/{id}`(:96) · POST `.../judge`(:143) · POST `/api/incidents`(incidents.py:179) · POST `.../assign`(:214) · POST `.../capa`(:273) · GET `/api/incidents`(:332) · GET `.../{id}`(:337) · POST `.../reinspections`(reinspections.py:186) · GET 同路径(:272) · POST `/api/batches/{id}/release`(release.py:296) · POST `.../report`(reports.py:98) · GET `.../reports`(:162) · GET `.../reports/{rid}`(:176) · GET `/api/batches/{id}/traces`(api/main.py:145) · POST `/api/intake`(intake.py:98) · POST `/api/intake/{task_id}/confirm`(:198)

启动入口 `uvicorn app.main:app`（`app/main.py:196`），内部走 `create_app_with_trace`，故 traces 路由可用。

## 任务

**1. 补 `GET /api/batches` 列表**（`app/api/batches.py`，同 `build_router` 风格、`Depends(get_deps)` 注入）。返回 `batch_id / batch_no / project_id / model / status / planned_quantity / created_at`，按创建时间倒序，支持可选 `status` 过滤，不做分页。复用 Repository，补 pytest。

**2. 规则版本可发现化。** 加只读`GET /api/quality-rule-versions`（装配进 `app/api/main.py:create_app`）+幂等 `scripts/seed-demo.ps1`（打印插入的 `rule_version_id`）。前端 `index.html` 的规则版本输入框改下拉，数据来自该接口；返回空则不渲染表单，提示「尚无规则版本，请先执行 seed 脚本」。**禁止硬编码或预填任何 UUID。**

**3. 首页改工作台。** 主体改批次列表（`batch_no / project_id / model / 状态徽标 / 计划数量 / 创建时间`），行内跳转「详情/轨迹/入场/放行/报告」全带 URL 参数；空列表引导去创建表单；创建成功后写入 localStorage 并刷新列表；删除 `index.html:43-47` 那条已过时的 TODO。

**4. 上下文记忆 + 流程串联。** `common.js` 加 `ZHL.remember(kind,id)` / `ZHL.recall(kind)`（localStorage）。6 个按 ID 进入的页面统一 `ZHL.qparam() || ZHL.recall()` 预填（现有点：batch.html:65、incident.html:69、intake.html:90、recheck.html:77、release.html:49、report.html:53、trace.html:40），预填非URL 来源时给轻提示；拿到 batchId 即写入记忆。补 next 跳转：判定通过→放行；复检 pass→放行 / fail→回批次；异常详情→复检、回列表；放行成功→生成报告。**跳转按钮在前置条件不满足时必须禁用。**

**5.（P1，可跳）** recheck 8 个量测参数加「JSON/表单」双模式；`describeError()` 优先取 `detail.reason` 作主文案；提交按钮请求中 `disabled` 防重提交；无任何 batchId 时给引导而非空屏；加 favicon 消除 404 噪音；`state.html` 移出主导航。

## 约束

- 不得为「让页面看起来能用」而 mock 数据。接口 404 与返回空，文案必须可区分。
- 后端新增端点可注入 session、`tests/` 有覆盖、不破坏既有 46 个 API 冒烟。
- 判断某项不必要（如分页、鉴权、reject 路由）就**说明理由并跳过**，不做半成品。

## 交付

1. 改动清单：路径 + 改了什么 + 前后对比。
2. 验证结果**贴真实输出**：`ruff check`、`pytest` 全绿（含新增）、实起 uvicorn 后逐个 curl 验首页列表可取 / `/` 302 / 10 静态页 200 / 不存在文件 404、内联脚本 node+DOM 桩逐页无错、零构建复检（无 package.json 等、common.js 无框架关键词、无外部 http(s)）。
3. 页面 → 接口 → 三态对照表，有变化处标注。
4. 环境阻塞（如缺 `ZHL_DATABASE_URL`）必须写明阻塞点和需要人做什么。