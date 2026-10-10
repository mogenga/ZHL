-- ============================================================
-- ZHL DDL 手工验证脚本（Task 5 Step 2，Review Focus #3/#5）
-- 在 zhl 库上跑：psql -d zhl -f migrations/verify_001.sql
-- 期望结果逐项标注在每条 SQL 前
-- ============================================================

\echo '===== [1] 表数应为 12 ====='
SELECT count(*) AS base_tables
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE';

\echo '===== [2] 扩展 ====='
SELECT extname FROM pg_extension WHERE extname IN ('btree_gist','uuid-ossp') ORDER BY extname;

\echo '===== [3] 触发器（EXCLUDE/列级填充/规则守卫） ====='
SELECT t.tgname AS trigger_name
FROM pg_trigger t
WHERE t.tgrelid = 'module_inspections'::regclass
   OR t.tgrelid = 'quality_rule_versions'::regclass
  AND NOT t.tgisinternal
ORDER BY 1;

\echo '===== [4] EXCLUDE 约束存在（D17 不降级） ====='
SELECT conname FROM pg_constraint
WHERE conrelid = 'module_inspections'::regclass AND contype = 'x';

\echo '===== [5] 种子：规则版本 2 行 ====='
SELECT count(*) AS rule_versions FROM quality_rule_versions;

\echo '===== [6] 种子：ISO 抽样表 36 行 ====='
SELECT count(*) AS iso_rows FROM iso_sampling_table;

-- ============================================================
-- Review Focus #3：同 SN 跨批次 → EXCLUDE 拒绝
-- ============================================================
\echo '===== [7] 造 2 批次 + 规则 ====='
INSERT INTO quality_rule_versions
    (project_id, model, name, nameplate_power, power_tolerance,
     parameter_standards, current_bands, sample_plan, effective_at, status, version_no)
VALUES ('P-DEFAULT','JH-550W','verify-rv',550,0.03,
        '{}'::jsonb,'[]'::jsonb,'{}'::jsonb, now(), 'draft', 9)
ON CONFLICT DO NOTHING;
INSERT INTO delivery_batches (batch_no, project_id, model, planned_quantity, rule_version_id, status)
VALUES ('B-V-1','P-DEFAULT','JH-550W',2,(SELECT rule_version_id FROM quality_rule_versions WHERE version_no=9 LIMIT 1),'pending_intake'),
       ('B-V-2','P-DEFAULT','JH-550W',2,(SELECT rule_version_id FROM quality_rule_versions WHERE version_no=9 LIMIT 1),'pending_intake')
ON CONFLICT (batch_no) DO NOTHING;

\echo '===== [8] 批次1 插入 SN-DUP-001（应成功） ====='
INSERT INTO module_inspections
    (batch_id, sn, source_type, captured_at, pmax, temp, isc, voc, vm, im, ff, appearance_result)
VALUES ((SELECT batch_id FROM delivery_batches WHERE batch_no='B-V-1'),
        'SN-DUP-001','SIMULATED_DEVICE', now(), 550,25,10.5,48,40,13,0.80,'pass')
ON CONFLICT DO NOTHING;

\echo '===== [9] 批次2 插入同 SN-DUP-001（应被 EXCLUDE 拒绝） ====='
DO $$
BEGIN
    BEGIN
        INSERT INTO module_inspections
            (batch_id, sn, source_type, captured_at, pmax, temp, isc, voc, vm, im, ff, appearance_result)
        VALUES ((SELECT batch_id FROM delivery_batches WHERE batch_no='B-V-2'),
                'SN-DUP-001','SIMULATED_DEVICE', now(), 550,25,10.5,48,40,13,0.80,'pass');
        RAISE NOTICE 'UNEXPECTED: cross-batch same SN was inserted (EXCLUDE did NOT fire)';
    EXCEPTION
        WHEN exclusion_violation THEN
            RAISE NOTICE 'OK: EXCLUDE rejected same SN across batches (D17)';
    END;
END
$$;

-- ============================================================
-- Review Focus #5：published 规则 UPDATE/DELETE 被触发器拦截
-- ============================================================
\echo '===== [10] 发布一条规则（status 单列变更允许） ====='
UPDATE quality_rule_versions SET status='published', version_no=9 WHERE project_id='P-DEFAULT' AND version_no=9 AND status='draft';
SELECT count(*) AS published_rules FROM quality_rule_versions WHERE status='published';

\echo '===== [11] UPDATE published 规则的字段值（应被触发器 REJECT） ====='
DO $$
BEGIN
    BEGIN
        UPDATE quality_rule_versions
        SET nameplate_power = 560
        WHERE status = 'published' AND version_no = 9;
        RAISE NOTICE 'UNEXPECTED: published rule field UPDATE was allowed';
    EXCEPTION
        WHEN raise_exception THEN
            RAISE NOTICE 'OK: published rule field UPDATE rejected by trigger (D6)';
    END;
END
$$;

\echo '===== [12] DELETE published 规则（应被触发器 REJECT） ====='
DO $$
BEGIN
    BEGIN
        DELETE FROM quality_rule_versions WHERE status = 'published' AND version_no = 9;
        RAISE NOTICE 'UNEXPECTED: published rule DELETE was allowed';
    EXCEPTION
        WHEN raise_exception THEN
            RAISE NOTICE 'OK: published rule DELETE rejected by trigger (D6)';
    END;
END
$$;

\echo '===== [13] status 单列变更 published->disabled（应允许，停用语义） ====='
UPDATE quality_rule_versions SET status='disabled' WHERE status='published' AND version_no=9;
SELECT status FROM quality_rule_versions WHERE version_no=9 AND project_id='P-DEFAULT';

-- ============================================================
-- D7/D8：应用角色 zhl_app 列级/表级权限
-- （需以 zhl_app 身份连接验证；这里以超级用户 SET ROLE 模拟）
-- ============================================================
\echo '===== [14] zhl_app UPDATE module_inspections.pmax（应权限拒） ====='
DO $$
BEGIN
    SET ROLE zhl_app;
    BEGIN
        UPDATE module_inspections SET pmax = pmax + 1;
        RAISE NOTICE 'UNEXPECTED: zhl_app could UPDATE pmax';
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'OK: zhl_app pmax UPDATE rejected (column-level NO UPDATE, D7)';
    END;
    RESET ROLE;
END
$$;

\echo '===== [15] zhl_app 普通列 UPDATE intake_status（应允许，非 D7 保护列） ====='
DO $$
BEGIN
    SET ROLE zhl_app;
    BEGIN
        UPDATE module_inspections SET intake_status = 'pending' WHERE pmax > -1;
        RAISE NOTICE 'OK: zhl_app can UPDATE non-protected columns (intake_status)';
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'UNEXPECTED: zhl_app UPDATE on intake_status rejected';
    END;
    RESET ROLE;
END
$$;

\echo '===== [16] zhl_app DELETE evidence（应权限拒，D8 证据只追加） ====='
DO $$
BEGIN
    SET ROLE zhl_app;
    BEGIN
        DELETE FROM evidence;
        RAISE NOTICE 'UNEXPECTED: zhl_app could DELETE evidence';
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'OK: zhl_app evidence DELETE rejected (append-only, D8)';
    END;
    RESET ROLE;
END
$$;

\echo '===== [17] zhl_app INSERT evidence（应允许，证据追加） ====='
DO $$
BEGIN
    SET ROLE zhl_app;
    BEGIN
        INSERT INTO evidence (evidence_type, object_ref, content_hash, generated_by)
        VALUES ('decision_snapshot','{"kind":"batch","id":"verify"}','sha256:verify','zhl_app');
        RAISE NOTICE 'OK: zhl_app can INSERT evidence (append allowed)';
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'UNEXPECTED: zhl_app evidence INSERT rejected';
    END;
    RESET ROLE;
END
$$;

\echo '===== [18] zhl_app UPDATE iso_sampling_table（应权限拒，D9 只读配置） ====='
DO $$
BEGIN
    SET ROLE zhl_app;
    BEGIN
        UPDATE iso_sampling_table SET ac = ac WHERE 1 = 0;
        RAISE NOTICE 'UNEXPECTED: zhl_app could UPDATE iso table';
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'OK: zhl_app iso_sampling_table UPDATE rejected (read-only config, D9)';
    END;
    RESET ROLE;
END
$$;

\echo '===== VERIFY DONE ====='
