# Alembic 迁移手册（ZHL）

> 2026-10-12 整理。Phase B 期间 alembic 只做了最小初始化（marker revision + 一条
> 契约扩展），Phase C 起才真正接管增量 DDL。本手册回答"我拿到一个新库 / 一个
> 老库 / 一个被 psql 手动跑过的库，分别该怎么和 alembic 对齐"。

## 修订图（2026-10-12 基线）

```
<base> ──> 001_init ──> 002_reinspection_scope (head)
```

- `001_init`：**marker revision**，不生成 DDL。它对应 `migrations/001_init.sql`
  （Phase 0 的 12 表 + 枚举 + EXCLUDE + 触发器 + 列级权限 + 种子数据），
  那份 SQL 是**在 psql 里直接跑的**，不走 alembic。
- `002_reinspection_scope`：PB L2/L3 契约扩展，加 3 列
  （`reinspections.scope_type` / `reinspections.scope_sns` /
  `quality_incidents.capa_verified`）。对应
  `migrations/002_add_reinspection_scope.sql`，但正常路径走
  `alembic upgrade`。

## 三种场景的操作步骤

### 场景 A：全新空库（`createdb zhl` 之后）

1. 以超级用户 / owner 身份建库：
   ```sh
   psql -h $DB_HOST -U postgres -d postgres -c "CREATE DATABASE zhl OWNER zhl_admin;"
   ```
2. 建 `zhl_app` / `zhl_reader` 角色（若尚未建），`psql -d zhl -f` 执行
   `migrations/001_init.sql`：
   ```sh
   psql -h $DB_HOST -U postgres -d zhl -f migrations/001_init.sql
   psql -h $DB_HOST -U postgres -d zhl -f migrations/verify_001.sql   # 18 项全绿再走下一步
   ```
3. 把基线记到 alembic：
   ```sh
   export ZHL_DATABASE_URL="postgresql+psycopg://zhl_app:zhl_app@localhost:15432/zhl"
   .venv/Scripts/python -m alembic stamp 001_init
   ```
   `stamp` 不会跑任何 DDL，只是写 `alembic_version`。
4. 升级到 head：
   ```sh
   .venv/Scripts/python -m alembic upgrade head
   ```
   此时 001 是 no-op（校验 6 张关键表存在），002 加 3 列。

> 如果 `stamp 001_init` 前忘了跑 001_init.sql，`upgrade head` 会在 001 的
> `upgrade()` 里被挡下并给出 "缺哪几张表 / 先跑 001_init.sql" 的指引——这是
> 故意的安全阀，不是 bug。

### 场景 B：已有库（曾 psql -f 跑过 001，且曾 psql -f 跑过 002 SQL）

```sh
.venv/Scripts/python -m alembic current        # 大概率输出空（未 stamp 过）
.venv/Scripts/python -m alembic stamp 002_reinspection_scope   # 直接把两条都盖住
.venv/Scripts/python -m alembic current        # 应为 002_reinspection_scope (head)
```

因为 002 的 Python 版和 SQL 版产出**相同的表形态**（内联 CHECK 都被 PG 自动命名
为 `reinspections_scope_type_check`），stamp 到 002 就是真实对齐，不需要
downgrade/upgrade 折腾。

### 场景 C：要回退 002 的 3 列

```sh
.venv/Scripts/python -m alembic downgrade 001_init
```

002 的 `downgrade()` 会：
1. 通过 inspector 找出实际存在的 CHECK 约束名
   （兼容 `reinspections_scope_type_check` 与历史上的 `ck_reinspections_scope_type`），
2. `DROP CONSTRAINT`，
3. `DROP COLUMN` 三个（`capa_verified`、`scope_sns`、`scope_type`）。

三列都带默认值，drop 本身不丢数据；但应用层 Phase B API 里
"创建复检必须显式传 scope_sns" 的校验会随列一起失效，回退前后应用要同步切换
到 pre-PB 行为，否则写路径会挂。

**不能回退到 `<base>`**：001 的 `downgrade()` 直接 `raise RuntimeError`，
理由写在它的 docstring 里——001 不是 alembic 生成的，其触发器 / EXCLUDE /
权限回收 / 种子数据的反向 DDL 不可知，唯一支持的回退方式就是 `dropdb zhl`
重建。

## 数据库 URL 从哪来

`alembic.ini` 的 `sqlalchemy.url` 留空是**故意的**（避免把明文凭证写进 git）。
`alembic/env.py` 按优先级取值：

1. 环境变量 `ZHL_DATABASE_URL`；
2. 项目根 `.env` 里的 `ZHL_DATABASE_URL`（`app/` 与 `alembic/` 共用，
   与 langgraph-checkpoint-postgres 保持一致）；
3. 都不给就 `RuntimeError`，提示去看 `.env.example`。

开发机上的值见 `compose.yaml` 的 `zhl_db` 服务（PG 18，端口 `5432:15432` 映射），
默认库 `zhl`、应用角色 `zhl_app` / 只读角色 `zhl_reader`。

## 新增修订的规范（Phase C 起）

- 生成：`alembic revision -m "003_<short_snake_name>"`，不要手写 revision id，
  用 alembic 生成的短 hash 即可；`001_init` / `002_reinspection_scope`
  这两个带语义前缀的历史 id 不再新增。
- 生成前确认 `app/infra/models.py` 已更新（`target_metadata = Base.metadata`），
  `alembic revision --autogenerate` 后**必须**人工过一遍 diff，再 `upgrade`。
- 与 SQL 文件的关系：契约级 DDL（列 / 约束 / 种子 / 权限变更）在
  `migrations/00N_*.sql` 里留一份可 `psql -f` 的镜像，但**执行路径**统一走
  alembic；SQL 文件主要用于 `psql` 应急、DBA 手动对齐、以及 `verify_*.sql`
  校验脚本引用。若两者出现分叉，以 alembic 为准，SQL 文件要回补。
- `verify_*.sql` 校验脚本与 revision 一一对应（`verify_001.sql` 对 001），
  每次新契约扩展加一份。

## 常见坑速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `alembic` 任意命令 KeyError / 找不到 revision | 当时 001 的 .py marker 缺失（已修） | 现在已修，新坑请开 issue |
| `downgrade` 报 `constraint "ck_..." does not exist` | 老版本 002 用命名 CHECK，SQL 用内联 CHECK | 已统一到内联；downgrade 双名兼容 |
| `stamp 001_init` 后 `upgrade head` 报"缺表" | 漏跑 `psql -f 001_init.sql` | 补跑 SQL + `verify_001.sql` 再升 |
| autogenerate 出的 diff 为空但库里缺列 | models 与 DB 都被改过、且没走 alembic | `stamp` 到当前真实 head，再补一条新 revision |
