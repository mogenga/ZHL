-- 002: §8.2 组件级隔离——intake_status 新增 'isolated' 取值（2026-10-14）。
-- 判定失败/异常创建时将受影响组件标记为 isolated；异常关闭（人工签署）
-- 后恢复为 confirmed。使放行断言④「无隔离组件」真实生效（此前恒真）。
-- 已初始化的库需手工补跑本文件；新库由更新后的 001_init.sql 覆盖。
-- 注意：PG 12+ 允许事务内 ALTER TYPE ADD VALUE，但新值须在事务提交后才可使用。

ALTER TYPE intake_status ADD VALUE IF NOT EXISTS 'isolated' AFTER 'confirmed';
