-- ============================================================
-- ZHL 规则版本管理 · 审计事件表 DDL（spec 2026-10-14-rule-version-management）
-- 影响范围：新增 rule_version_events 表 + rule_version_action 枚举
-- 回滚方案：DROP TABLE rule_version_events; DROP TYPE rule_version_action;
-- ============================================================
BEGIN;

CREATE TYPE rule_version_action AS ENUM ('created','published','disabled');

CREATE TABLE rule_version_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_version_id UUID NOT NULL REFERENCES quality_rule_versions(rule_version_id),
    action          rule_version_action NOT NULL,
    operator        text NOT NULL,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_rule_events_rule ON rule_version_events(rule_version_id, created_at);

-- 授权（对齐 001_init.sql:320 GRANT 块）
GRANT SELECT, INSERT ON rule_version_events TO zhl_app;

COMMIT;
