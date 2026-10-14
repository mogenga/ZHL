-- 002: §8.2 组件级隔离——intake_status 新增 'isolated' 取值（2026-10-14）。
-- 判定失败/异常创建时将受影响组件标记为 isolated；异常关闭（人工签署）
-- 后恢复为 confirmed。使放行断言④「无隔离组件」真实生效（此前恒真）。
-- 已初始化的库需手工补跑本文件；新库由更新后的 001_init.sql 覆盖。
-- 幂等：PG 的 ALTER TYPE ADD VALUE 不支持对「值」的 IF NOT EXISTS 去重，
-- 故用 pg_type + pg_enum 守卫——已含 'isolated' 则整块跳过，可安全重跑。
-- 注意：PG 12+ 允许事务内 ALTER TYPE ADD VALUE，但新值须在事务提交后才
-- 使用；本脚本单语句独立执行，提交后即刻可用。

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_type
        WHERE typname = 'intake_status'
          AND EXISTS (
                SELECT 1 FROM pg_catalog.pg_enum
                WHERE pg_enum.enumtypid = pg_type.oid
                  AND pg_enum.enumlabel = 'isolated'
          )
    ) THEN
        EXECUTE 'ALTER TYPE intake_status ADD VALUE ''isolated'' AFTER ''confirmed''';
    END IF;
END
$$;
