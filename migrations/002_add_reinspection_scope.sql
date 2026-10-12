-- ============================================================
-- ZHL 组件检验判定平台 · Phase B 契约扩展 DDL（PB spec §4 L2/L3 + D5a）
--
-- 影响范围：reinspections 加 2 列（scope_type / scope_sns），
--           quality_incidents 加 1 列（capa_verified）。
-- 回滚方案：DROP COLUMN × 3（见 alembic/versions/002_reinspection_scope.py downgrade）。
--
-- 说明：
--   1. scope_type 走 VARCHAR(40) + CHECK，与 001 的 native CREATE TYPE 风格不同，
--      原因：models.py 约定 native_enum=False（跨库自检），DDL 与 models 保持一致。
--   2. 本脚本不幂等（重复执行报 "column already exists"）。
--      正常路径：alembic revision 002 通过 op.add_column 执行一次；
--      psql -f 直接执行时由 alembic stamp 记录版本，防止重跑。
--   3. 存量行回填语义：scope_sns 保持 []（NOT NULL 可过），
--      应用层创建复检时必须显式传 scope_sns（Phase B API 校验）。
-- ============================================================

BEGIN;

-- ---------- reinspections：PB L2 复检范围列 ----------
-- scope_type：复检范围类型（'minimal' | 'expanded' | 'full_lot'）
ALTER TABLE reinspections
    ADD COLUMN scope_type VARCHAR(40) NOT NULL DEFAULT 'minimal'
        CHECK (scope_type IN ('minimal', 'expanded', 'full_lot'));

-- scope_sns：本次复检实际覆盖的 SN 列表（JSONB 数组，默认 []）
ALTER TABLE reinspections
    ADD COLUMN scope_sns JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ---------- quality_incidents：PB L3 CAPA 人工确认标志 ----------
-- capa_verified：工程师显式确认 CAPA 生效（复检通过 ≠ 已验证，见 L3）
ALTER TABLE quality_incidents
    ADD COLUMN capa_verified BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;
