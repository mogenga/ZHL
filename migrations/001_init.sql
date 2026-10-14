-- ============================================================
-- ZHL 组件检验判定平台 · Phase 0 共享契约 DDL（P0 spec §5.1）
-- 12 表 + 枚举 + EXCLUDE/触发器/列级权限 + 种子数据
-- 目标：PostgreSQL 15+（在 PG 18.6 验证）
-- 可独立执行：psql $ZHL_DB -f 001_init.sql
-- ============================================================

BEGIN;

-- ---------- 扩展 ----------
CREATE EXTENSION IF NOT EXISTS btree_gist;      -- D5/D17：EXCLUDE USING gist(sn_uniqueness)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";      -- gen_random_uuid()

-- ---------- 枚举 ----------
CREATE TYPE rule_status AS ENUM ('draft', 'published', 'disabled');
CREATE TYPE batch_status AS ENUM (
    'draft', 'pending_intake', 'pending_judge', 'pending_release', 'isolated',
    'disposing', 'pending_recheck', 'rejected', 'released', 'archived'
);
CREATE TYPE release_decision AS ENUM ('released', 'rejected', 'undecided');
CREATE TYPE source_type AS ENUM ('OCR_FORM', 'IV_CSV', 'MANUAL', 'SIMULATED_DEVICE');
CREATE TYPE appearance_result AS ENUM ('pass', 'fail');
CREATE TYPE intake_status AS ENUM ('pending', 'confirmed', 'isolated', 'rejected');
CREATE TYPE decision_status AS ENUM ('pending', 'pass', 'fail', 'rule_missing');
CREATE TYPE trigger_type AS ENUM ('rule_fail', 'sample_fail', 'appearance_manual', 'data_consistency', 'rule_missing');
CREATE TYPE severity AS ENUM ('critical', 'major', 'minor');
CREATE TYPE incident_status AS ENUM ('new', 'isolated', 'assigned', 'pending_recheck', 'closed', 'rejected', 'scrapped');
CREATE TYPE sample_result AS ENUM ('pass', 'fail', 'rule_missing');
CREATE TYPE evidence_type AS ENUM ('source_file', 'extraction_result', 'human_confirmation', 'decision_snapshot', 'task_trace', 'report_snapshot');
CREATE TYPE trace_validation AS ENUM ('pass', 'fail', 'partial');
CREATE TYPE report_type AS ENUM ('release', 'incident');

-- ---------- 5.1.1 quality_rule_versions（被引用表，先建） ----------
CREATE TABLE quality_rule_versions (
    rule_version_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        text        NOT NULL,
    model             text        NOT NULL,
    name              text        NOT NULL,
    nameplate_power   numeric     NOT NULL CHECK (nameplate_power > 0),
    power_tolerance   numeric     NOT NULL CHECK (power_tolerance >= 0 AND power_tolerance <= 1),
    parameter_standards JSONB    NOT NULL,
    current_bands     JSONB       NOT NULL,
    sample_plan       JSONB       NOT NULL,
    effective_at      timestamptz NOT NULL,
    status            rule_status NOT NULL DEFAULT 'draft',
    version_no        int         NOT NULL,
    created_at        timestamptz DEFAULT now(),
    updated_at        timestamptz DEFAULT now(),
    UNIQUE (project_id, model, version_no)            -- D12 幂等导入
);

-- D6：规则版本不可变 —— published 时拦截字段 UPDATE / DELETE；仅允许 status 单列变更
CREATE FUNCTION fn_rule_version_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.status = 'published' AND NEW IS DISTINCT FROM OLD THEN
            -- 仅允许 status 单列变更（published -> disabled 走停用语义）
            IF (NEW.project_id, NEW.model, NEW.name, NEW.nameplate_power,
                NEW.power_tolerance, NEW.parameter_standards, NEW.current_bands,
                NEW.sample_plan, NEW.effective_at, NEW.version_no)
               IS DISTINCT FROM
               (OLD.project_id, OLD.model, OLD.name, OLD.nameplate_power,
                OLD.power_tolerance, OLD.parameter_standards, OLD.current_bands,
                OLD.sample_plan, OLD.effective_at, OLD.version_no) THEN
                RAISE EXCEPTION 'published rule version % is immutable; only status single-column change allowed',
                    NEW.rule_version_id;
            END IF;
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.status = 'published' THEN
            RAISE EXCEPTION 'cannot delete published rule version %; disable it instead', OLD.rule_version_id;
        END IF;
        RETURN NULL;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_rule_version_guard
    BEFORE UPDATE OR DELETE ON quality_rule_versions
    FOR EACH ROW EXECUTE FUNCTION fn_rule_version_guard();

-- ---------- 5.1.10 evidence（被引用表，先建） ----------
CREATE TABLE evidence (
    evidence_id   UUID           PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_type evidence_type  NOT NULL,
    object_ref    JSONB          NOT NULL,          -- {kind, id}；kind ∈ 8 类对象
    content_hash  text           NOT NULL,          -- SHA-256
    file_path     text,
    content       JSONB,
    generated_at  timestamptz    NOT NULL DEFAULT now(),
    generated_by  text           NOT NULL DEFAULT 'system'
);
-- D8：证据只追加 —— 应用角色 REVOKE UPDATE, DELETE（见末尾角色段）

-- ---------- 5.1.2 delivery_batches ----------
CREATE TABLE delivery_batches (
    batch_id           UUID              PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_no           text              NOT NULL UNIQUE,
    project_id         text              NOT NULL,
    model              text              NOT NULL,
    planned_quantity   int               NOT NULL,
    actual_quantity    int               NOT NULL DEFAULT 0,
    rule_version_id    UUID              NOT NULL REFERENCES quality_rule_versions(rule_version_id),
    status             batch_status      NOT NULL DEFAULT 'draft',
    release_decision   release_decision  NOT NULL DEFAULT 'undecided',
    release_evidence_id UUID             REFERENCES evidence(evidence_id),
    created_at         timestamptz       DEFAULT now(),
    updated_at         timestamptz       DEFAULT now()
);

-- ---------- 5.1.3 module_inspections ----------
CREATE TABLE module_inspections (
    inspection_id    UUID              PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id         UUID              NOT NULL REFERENCES delivery_batches(batch_id),
    sn               text              NOT NULL,
    sn_num           bigint            NOT NULL,           -- D5：hashtext(sn)，触发器填充
    sn_uniqueness    int8range         NOT NULL,           -- D5：EXCLUDE 辅助列
    source_type      source_type       NOT NULL,
    source_file_id   UUID              REFERENCES evidence(evidence_id),
    captured_at      timestamptz       NOT NULL,
    pmax             numeric           NOT NULL,
    temp             numeric           NOT NULL,
    isc              numeric           NOT NULL,
    voc              numeric           NOT NULL,
    vm               numeric           NOT NULL,
    im               numeric           NOT NULL,
    ff               numeric           NOT NULL,
    appearance_result appearance_result NOT NULL,
    intake_status    intake_status     NOT NULL DEFAULT 'pending',
    decision_status  decision_status   NOT NULL DEFAULT 'pending',
    rule_snapshot    JSONB,
    decision_evidence JSONB,
    created_at       timestamptz       DEFAULT now(),
    updated_at       timestamptz       DEFAULT now(),
    UNIQUE (batch_id, sn),                                   -- 批次内唯一
    CONSTRAINT sn_uniqueness_check CHECK (sn_uniqueness = int8range(sn_num, sn_num + 1, '[]')),
    EXCLUDE USING gist (sn_uniqueness WITH &&)                -- D5/D17：SN 跨批次不可重复（不降级）
);

-- D5：BEFORE INSERT 自动填充 sn_num / sn_uniqueness
CREATE FUNCTION fn_inspection_fill_sn() RETURNS trigger AS $$
BEGIN
    IF NEW.sn_num IS NULL THEN
        NEW.sn_num := pg_catalog.hashtext(NEW.sn);
    END IF;
    NEW.sn_uniqueness := int8range(NEW.sn_num, NEW.sn_num + 1, '[]');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_inspection_fill_sn
    BEFORE INSERT ON module_inspections
    FOR EACH ROW EXECUTE FUNCTION fn_inspection_fill_sn();

-- ---------- 5.1.4 quality_incidents（先于 incident_sn_links） ----------
CREATE TABLE quality_incidents (
    incident_id            UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id               UUID             NOT NULL REFERENCES delivery_batches(batch_id),
    trigger_type           trigger_type     NOT NULL,
    severity               severity          NOT NULL,
    evidence_ids           JSONB            NOT NULL DEFAULT '[]'::jsonb,
    owner                  text,
    due_at                 timestamptz,
    containment_action     text,
    root_cause_hypotheses  JSONB,
    confirmed_root_cause   text,
    corrective_action      text,
    verification_id        UUID,          -- 后挂 FK → reinspections.reinspection_id（循环依赖，见下）
    status                 incident_status  NOT NULL DEFAULT 'new',
    created_at             timestamptz     DEFAULT now(),
    updated_at             timestamptz     DEFAULT now()
);

-- ---------- 5.1.6 reinspections（先于 quality_incidents.verification_id） ----------
CREATE TABLE reinspections (
    reinspection_id  UUID           PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id      UUID           NOT NULL REFERENCES quality_incidents(incident_id),
    inspection_id    UUID           NOT NULL REFERENCES module_inspections(inspection_id),
    rule_version_id  UUID           NOT NULL REFERENCES quality_rule_versions(rule_version_id),
    source_file_id   UUID           REFERENCES evidence(evidence_id),
    pmax             numeric        NOT NULL,
    temp             numeric        NOT NULL,
    isc              numeric        NOT NULL,
    voc              numeric        NOT NULL,
    vm               numeric        NOT NULL,
    im               numeric        NOT NULL,
    ff               numeric        NOT NULL,
    result           appearance_result NOT NULL,
    executor         text           NOT NULL,
    executed_at      timestamptz    NOT NULL,
    evidence_id      UUID           REFERENCES evidence(evidence_id),
    created_at       timestamptz    DEFAULT now(),
    updated_at       timestamptz    DEFAULT now()
);

-- 补齐循环依赖：quality_incidents.verification_id → reinspections
ALTER TABLE quality_incidents
    ADD CONSTRAINT fk_incident_verification
    FOREIGN KEY (verification_id) REFERENCES reinspections(reinspection_id);

-- ---------- 5.1.5 incident_sn_links ----------
CREATE TABLE incident_sn_links (
    incident_id   UUID NOT NULL REFERENCES quality_incidents(incident_id) ON DELETE CASCADE,
    inspection_id UUID NOT NULL REFERENCES module_inspections(inspection_id) ON DELETE CASCADE,
    PRIMARY KEY (incident_id, inspection_id)           -- 复合主键（D4，不用自由文本）
);

-- ---------- 5.1.7 samples ----------
CREATE TABLE samples (
    sample_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id          UUID NOT NULL REFERENCES delivery_batches(batch_id),
    inspection_type   text NOT NULL,
    rule_version_id   UUID NOT NULL REFERENCES quality_rule_versions(rule_version_id),
    sample_round      int  NOT NULL DEFAULT 1,
    sn                text NOT NULL,
    selected_at       timestamptz NOT NULL,
    seed              text NOT NULL,                     -- = batch_id 字符串
    created_at        timestamptz DEFAULT now(),
    UNIQUE (batch_id, inspection_type, rule_version_id, sample_round, sn)
);

-- ---------- 5.1.8 sample_conclusions ----------
CREATE TABLE sample_conclusions (
    conclusion_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id          UUID NOT NULL REFERENCES delivery_batches(batch_id),
    inspection_type   text NOT NULL,
    rule_version_id   UUID NOT NULL REFERENCES quality_rule_versions(rule_version_id),
    sample_round      int  NOT NULL DEFAULT 1,
    n                 int  NOT NULL,
    ac                int  NOT NULL,
    re                int  NOT NULL,
    result            sample_result NOT NULL,
    evidence_id       UUID REFERENCES evidence(evidence_id),
    created_at        timestamptz DEFAULT now(),
    updated_at        timestamptz DEFAULT now(),
    UNIQUE (batch_id, inspection_type, rule_version_id, sample_round)
);

-- ---------- 5.1.9 iso_sampling_table（只读配置表，D9/D16） ----------
CREATE TABLE iso_sampling_table (
    row_id            int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inspection_type   text   NOT NULL,
    inspection_level  text   NOT NULL,
    aql               numeric NOT NULL,
    lot_size_min      int    NOT NULL,
    lot_size_max      int    NOT NULL,
    n                 int    NOT NULL,
    ac                int    NOT NULL,
    re                int    NOT NULL,
    CHECK (ac < re),
    UNIQUE (inspection_type, inspection_level, aql, lot_size_min, lot_size_max)
);

-- ---------- 5.1.11 agent_task_traces ----------
CREATE TABLE agent_task_traces (
    task_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id            UUID REFERENCES delivery_batches(batch_id),
    input_ref           JSONB NOT NULL,
    skill_name          text  NOT NULL,
    plugins_invoked     JSONB NOT NULL DEFAULT '[]'::jsonb,
    params_summary      JSONB NOT NULL DEFAULT '{}'::jsonb,
    model_version       text,
    structured_output   JSONB,
    validation_result   trace_validation NOT NULL DEFAULT 'partial',
    failure_reason      text,
    human_action        JSONB,
    started_at          timestamptz NOT NULL,
    finished_at         timestamptz
);

-- ---------- 5.1.12 reports ----------
CREATE TABLE reports (
    report_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id            UUID NOT NULL REFERENCES delivery_batches(batch_id),
    report_type         report_type NOT NULL,
    version             int  NOT NULL DEFAULT 1,
    source_snapshot_id  UUID NOT NULL REFERENCES evidence(evidence_id),
    agent_task_id       UUID REFERENCES agent_task_traces(task_id),
    content_md          text NOT NULL,
    content_json        JSONB NOT NULL,
    generated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, report_type, version)
);

-- ---------- 索引 ----------
-- 高频 btree
CREATE INDEX ix_inspections_batch   ON module_inspections (batch_id);
CREATE INDEX ix_inspections_sn      ON module_inspections (sn);
CREATE INDEX ix_incidents_batch     ON quality_incidents (batch_id);
CREATE INDEX ix_batches_rule        ON delivery_batches (rule_version_id);
CREATE INDEX ix_reinspections_inc   ON reinspections (incident_id);
CREATE INDEX ix_reports_batch_type  ON reports (batch_id, report_type);
CREATE INDEX ix_samples_batch       ON samples (batch_id);
CREATE INDEX ix_conclusions_batch   ON sample_conclusions (batch_id);
CREATE INDEX ix_traces_batch        ON agent_task_traces (batch_id);
-- JSONB GIN
CREATE INDEX gin_inspections_rule_snap   ON module_inspections USING gin (rule_snapshot);
CREATE INDEX gin_inspections_dec_ev       ON module_inspections USING gin (decision_evidence);
CREATE INDEX gin_incidents_root_hyp       ON quality_incidents USING gin (root_cause_hypotheses);
CREATE INDEX gin_traces_struct_out        ON agent_task_traces USING gin (structured_output);
CREATE INDEX gin_evidence_object_ref      ON evidence USING gin (object_ref);
CREATE INDEX gin_evidence_content         ON evidence USING gin (content);
CREATE INDEX gin_reports_content_json     ON reports USING gin (content_json);

-- ---------- 应用角色（D7/D8 权限回收） ----------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zhl_app') THEN
        CREATE ROLE zhl_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO zhl_app;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON delivery_batches, module_inspections, quality_incidents,
       incident_sn_links, reinspections, samples, sample_conclusions,
       agent_task_traces, reports, quality_rule_versions TO zhl_app;
-- D7：原始检验值列级 NO UPDATE（先撤整表 UPDATE，再仅授回非保护列）
REVOKE UPDATE ON module_inspections FROM zhl_app;
GRANT UPDATE (intake_status, decision_status, rule_snapshot, decision_evidence,
              source_type, source_file_id, captured_at)
    ON module_inspections TO zhl_app;
-- D8：证据只追加
GRANT SELECT, INSERT ON evidence TO zhl_app;
REVOKE UPDATE, DELETE ON evidence FROM zhl_app;
-- D9：ISO 抽样表只读配置（初始化 INSERT 走超级用户，应用角色仅 SELECT）
GRANT SELECT ON iso_sampling_table TO zhl_app;
REVOKE INSERT, UPDATE, DELETE ON iso_sampling_table FROM zhl_app;
-- D7：原始检验值列级 NO UPDATE（由上面撤整表+选择性授予实现，双保险）

-- ---------- 种子数据（幂等 ON CONFLICT DO NOTHING，D12） ----------
-- 规则种子：2 型号（JH-550W 默认 + 测试型号）
INSERT INTO quality_rule_versions
    (rule_version_id, project_id, model, name, nameplate_power, power_tolerance,
     parameter_standards, current_bands, sample_plan, effective_at, status, version_no)
VALUES
    (
        '11111111-1111-1111-1111-111111111111', 'P-DEFAULT', 'JH-550W', 'JH-550W 默认规则 v1', 550, 0.03,
        '{"temp":{"std_value":25.0,"tolerance":0.10,"enabled":true},"isc":{"std_value":10.5,"tolerance":0.05,"enabled":true},"voc":{"std_value":48.0,"tolerance":0.05,"enabled":true},"vm":{"std_value":40.0,"tolerance":0.05,"enabled":true},"im":{"std_value":13.0,"tolerance":0.05,"enabled":true},"ff":{"std_value":0.80,"tolerance":0.02,"enabled":true}}'::jsonb,
        '[{"lower":12.0,"upper":14.0,"grade":"A"},{"lower":14.0,"upper":15.5,"grade":"B"}]'::jsonb,
        '{"inspection_type":"iv","inspection_level":"normal","aql":1.0}'::jsonb,
        now(), 'draft', 1
    ),
    (
        '22222222-2222-2222-2222-222222222222', 'P-TEST', 'JH-550W', 'JH-550W 测试型号 v2', 550, 0.03,
        '{"temp":{"std_value":25.0,"tolerance":0.10,"enabled":true},"isc":{"std_value":10.5,"tolerance":0.05,"enabled":true},"voc":{"std_value":48.0,"tolerance":0.05,"enabled":true},"vm":{"std_value":40.0,"tolerance":0.05,"enabled":true},"im":{"std_value":13.0,"tolerance":0.05,"enabled":true},"ff":{"std_value":0.80,"tolerance":0.02,"enabled":true}}'::jsonb,
        '[{"lower":12.0,"upper":14.0,"grade":"A"},{"lower":14.0,"upper":15.5,"grade":"B"}]'::jsonb,
        '{"inspection_type":"iv","inspection_level":"normal","aql":1.0}'::jsonb,
        now(), 'draft', 2
    )
ON CONFLICT (project_id, model, version_no) DO NOTHING;

-- ISO 抽样表种子（D9：3 类型 × 3 AQL × 批量区间；缺项即规则异常）
INSERT INTO iso_sampling_table
    (inspection_type, inspection_level, aql, lot_size_min, lot_size_max, n, ac, re)
VALUES
    ('iv',       'normal', 0.65, 2,  8,    8, 0, 1),
    ('iv',       'normal', 0.65, 9,  15,   13, 0, 1),
    ('iv',       'normal', 0.65, 16, 25,   20, 1, 2),
    ('iv',       'normal', 0.65, 26, 50,   32, 1, 2),
    ('iv',       'normal', 0.65, 51, 90,   50, 2, 3),
    ('iv',       'normal', 0.65, 91, 150,  80, 3, 4),
    ('iv',       'normal', 1.0,  2,  8,    8, 0, 1),
    ('iv',       'normal', 1.0,  9,  15,   13, 0, 1),
    ('iv',       'normal', 1.0,  16, 25,   20, 1, 2),
    ('iv',       'normal', 1.0,  26, 50,   32, 1, 2),
    ('iv',       'normal', 1.0,  51, 90,   50, 2, 3),
    ('iv',       'normal', 1.0,  91, 150,  80, 3, 4),
    ('iv',       'normal', 1.5,  2,  8,    8, 0, 1),
    ('iv',       'normal', 1.5,  9,  15,   13, 0, 1),
    ('iv',       'normal', 1.5,  16, 25,   20, 1, 2),
    ('iv',       'normal', 1.5,  26, 50,   32, 2, 3),
    ('iv',       'normal', 1.5,  51, 90,   50, 3, 4),
    ('iv',       'normal', 1.5,  91, 150,  80, 5, 6),
    ('visual',   'normal', 0.65, 2,  8,    8, 0, 1),
    ('visual',   'normal', 0.65, 9,  15,   13, 0, 1),
    ('visual',   'normal', 0.65, 16, 25,   20, 1, 2),
    ('visual',   'normal', 1.0,  2,  8,    8, 0, 1),
    ('visual',   'normal', 1.0,  9,  15,   13, 0, 1),
    ('visual',   'normal', 1.0,  16, 25,   20, 1, 2),
    ('visual',   'normal', 1.5,  2,  8,    8, 0, 1),
    ('visual',   'normal', 1.5,  9,  15,   13, 0, 1),
    ('visual',   'normal', 1.5,  16, 25,   20, 1, 2),
    ('electrical','normal', 0.65, 2,  8,    8, 0, 1),
    ('electrical','normal', 0.65, 9,  15,   13, 0, 1),
    ('electrical','normal', 0.65, 16, 25,   20, 1, 2),
    ('electrical','normal', 1.0,  2,  8,    8, 0, 1),
    ('electrical','normal', 1.0,  9,  15,   13, 0, 1),
    ('electrical','normal', 1.0,  16, 25,   20, 1, 2),
    ('electrical','normal', 1.5,  2,  8,    8, 0, 1),
    ('electrical','normal', 1.5,  9,  15,   13, 0, 1),
    ('electrical','normal', 1.5,  16, 25,   20, 1, 2)
ON CONFLICT (inspection_type, inspection_level, aql, lot_size_min, lot_size_max) DO NOTHING;

COMMIT;

-- ============================================================
-- 验证检查（不属交付 DDL，单独跑）：
--   \dt            -- 应见 12 张表
--   同 SN 跨批次 INSERT → EXCLUDE 报错（D17 不降级）
--   UPDATE quality_rule_versions ... WHERE status='published' → 触发器 REJECT
--   GRANT zhl_app 后 UPDATE module_inspections.pmax / DELETE evidence → 列级/表级权限拒
-- ============================================================
