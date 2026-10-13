# ZHL · 演示种子（幂等）
#
# 用法：  .\scripts\seed-demo.ps1
# 作用：  向 PostgreSQL 灌入一条可驱动的 published 规则版本（JH-550W 演示版），
#         幂等（ON CONFLICT DO NOTHING + 固定 UUID），并在末尾打印
#         rule_version_id，便于前端工作台下拉选择 / 手工建批次时引用。
#
# 设计说明：
#   - 固定 UUID '11111111-1111-1111-1111-111111111111'（与 001_init.sql 种子对齐），
#     重复执行不会翻倍（D12 幂等导入）。
#   - status 置为 published，使 GET /api/quality-rule-versions 下拉有「可判定」项；
#     001_init.sql 里的 draft 种子仅用于 DDL 自测，不驱动真实判定链路。
#   - 连接串取 ZHL_DATABASE_URL（.env）；缺省时回落 start-pg.ps1 默认
#     postgresql://postgres:postgres@localhost:15432/zhl。
#
# 前置：PostgreSQL 已建表（跑过 start-pg.ps1 或 migrations/001_init.sql）。

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # 项目根目录

# 读 ZHL_DATABASE_URL（.env），缺省回落 start-pg.ps1 默认。
# SQLAlchemy URL 形如 postgresql+psycopg://user:pass@host:port/db，psql 需
# postgresql://user:pass@host:port/db（去 driver 前缀、+psycopg 子串）。
function Get-PgSqlConn {
    $raw = $env:ZHL_DATABASE_URL
    if (-not $raw) {
        # 尝试从 .env 读
        $envFile = Get-Location
        $envPath = Join-Path $envFile ".env"
        if (Test-Path $envPath) {
            $line = Select-String -Path $envPath -Pattern '^ZHL_DATABASE_URL=' | Select-Object -First 1
            if ($line) { $raw = $line.Line.Substring('ZHL_DATABASE_URL='.Length).Trim().Trim('"').Trim("'") }
        }
    }
    if (-not $raw) { $raw = "postgresql://postgres:postgres@localhost:15432/zhl" }
    # postgresql+psycopg:// → postgresql://
    $raw = $raw -replace '^postgresql\+psycopg://', 'postgresql://'
    $raw = $raw -replace '^postgresql\+[^:]+://', 'postgresql://'
    return $raw
}

$pgConn = Get-PgSqlConn
Write-Host "==> 使用连接串：$pgConn" -ForegroundColor Cyan

# 检查 psql 可用（未安装则引导用 start-pg.ps1 的 docker psql）
$psql = Get-Command psql -ErrorAction SilentlyContinue
if (-not $psql) {
    Write-Host "!! 本地未找到 psql。可改用 docker 内的 psql：" -ForegroundColor Red
    Write-Host "   docker exec zhl-pg psql -U postgres -d zhl -f /tmp/seed_demo.sql" -ForegroundColor Red
    Write-Host "   或安装 psql / 配置 PATH 后重试。" -ForegroundColor Red
    exit 1
}

$seedSql = @'
BEGIN;

-- 演示用 published 规则版本（幂等，固定 UUID，与 001_init.sql 种子对齐）
INSERT INTO quality_rule_versions
    (rule_version_id, project_id, model, name, nameplate_power, power_tolerance,
     parameter_standards, current_bands, sample_plan, effective_at, status, version_no)
VALUES
    (
        '11111111-1111-1111-1111-111111111111', 'P-DEFAULT', 'JH-550W',
        'JH-550W 演示规则（published）', 550, 0.03,
        '{"temp":{"std_value":25.0,"tolerance":0.10,"enabled":true},"isc":{"std_value":10.5,"tolerance":0.05,"enabled":true},"voc":{"std_value":48.0,"tolerance":0.05,"enabled":true},"vm":{"std_value":40.0,"tolerance":0.05,"enabled":true},"im":{"std_value":13.0,"tolerance":0.05,"enabled":true},"ff":{"std_value":0.80,"tolerance":0.02,"enabled":true}}'::jsonb,
        '[{"lower":12.0,"upper":14.0,"grade":"A"},{"lower":14.0,"upper":15.5,"grade":"B"}]'::jsonb,
        '{"inspection_type":"iv","inspection_level":"normal","aql":1.0}'::jsonb,
        now(), 'published', 1
    )
ON CONFLICT (project_id, model, version_no) DO UPDATE
    SET status = 'published';

-- 若 001 种子的第二条（P-TEST v2）仍为 draft，也一并升级为 published，
-- 让下拉里两条都可驱动判定（幂等）。
UPDATE quality_rule_versions
SET status = 'published'
WHERE project_id = 'P-TEST' AND version_no = 2 AND status = 'draft';

COMMIT;

-- 打印可发现的 rule_version_id（前端下拉 / 手工建批次引用）
SELECT 'seederule=' || rule_version_id
FROM quality_rule_versions
WHERE status = 'published'
ORDER BY version_no;
'@

Write-Host "==> 执行 seed_demo ..." -ForegroundColor Cyan
# 用临时变量文件，避免把多行 here-string 直接塞进 psql -c（引号转义易错）
$seedFile = Join-Path $env:TEMP "zhl_seed_demo_$([guid]::NewGuid().ToString('N')).sql"
# CRLF 转 LF，避免 psql 报 syntax
$seedSql.Replace("`r`n", "`n") | Set-Content -Path $seedFile -Encoding utf8 -NoNewline
& psql "$pgConn" -v ON_ERROR_STOP=1 -f $seedFile
$code = $LASTEXITCODE
Remove-Item $seedFile -Force -ErrorAction SilentlyContinue

if ($code -ne 0) {
    Write-Host "!! seed 执行失败（退出码 $code）。确认 001_init.sql 已建表。" -ForegroundColor Red
    exit $code
}

Write-Host ""
Write-Host "==> 完成。seed 已幂等写入。上方 seederule= 行即前端可用的 rule_version_id。" -ForegroundColor Green
Write-Host "    前端工作台刷新后，规则版本下拉将出现 published 项。" -ForegroundColor Green
