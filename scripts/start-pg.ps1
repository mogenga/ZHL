# ZHL · 本地 PostgreSQL 15 容器初始化（无 docker compose 插件时）
#
# 用法：  .\scripts\start-pg.ps1
# 作用：  拉取 postgres:15 → 启动 zhl-pg 容器（localhost:15432）→ 执行 001_init.sql + verify_001.sql
# 凭据：  与 .env / alembic.ini 一致（postgres:postgres @ zhl）
#
# 有 compose 的环境请改用：docker compose -f compose.yaml up -d（首启自动建表）

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # 项目根目录

Write-Host "==> 检查 Docker 引擎 ..." -ForegroundColor Cyan
$eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"; docker info *> $null; $ErrorActionPreference = $eap
if ($LASTEXITCODE -ne 0) {
    Write-Host "!! Docker 引擎不可达（Docker Desktop 未启动？）。请启动 Docker Desktop 后重试。" -ForegroundColor Red
    exit 1
}

# 拉取镜像（本地已有则跳过：国内镜像源不稳，pull 失败前会把本地 tag 的 manifest 污染成 HTML 垃圾描述符）
$eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
docker image inspect postgres:15 *> $null
$hasLocalImage = ($LASTEXITCODE -eq 0)
if (-not $hasLocalImage) {
    Write-Host "==> 拉取 postgres:15 ..." -ForegroundColor Cyan
    docker pull postgres:15 | Select-Object -Last 1
    docker image inspect postgres:15 *> $null
    if ($LASTEXITCODE -ne 0) { Write-Host "!! 拉取镜像失败且本地无 postgres:15 镜像" -ForegroundColor Red; exit 1 }
} else {
    Write-Host "==> 本地已有 postgres:15 镜像，跳过在线拉取。" -ForegroundColor Cyan
}
$ErrorActionPreference = $eap

# 启动容器（幂等：先清理同名旧容器；PS5.1 下对不存在的容器 2>$null 会因 EAP=Stop 抛错，故先查再删）
if (docker ps -a --format '{{.Names}}' | Select-String -Quiet -Pattern '^zhl-pg$') { docker rm -f zhl-pg | Out-Null }
Write-Host "==> 启动 zhl-pg 容器 (postgres:15, localhost:15432) ..." -ForegroundColor Cyan
docker run -d --name zhl-pg `
    -e POSTGRES_PASSWORD=postgres `
    -e POSTGRES_DB=zhl `
    -e POSTGRES_USER=postgres `
    -e PGCLIENTENCODING=UTF8 `
    -p 15432:5432 postgres:15 *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "!! 容器启动失败" -ForegroundColor Red; exit 1 }

# 等 pg_isready
Write-Host "==> 等待 PostgreSQL 就绪 ..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    docker exec zhl-pg pg_isready -U postgres 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Host "!! PostgreSQL 60s 内未就绪，查看日志：" -ForegroundColor Red
    docker logs zhl-pg --tail 20
    exit 1
}
Write-Host "    PostgreSQL 就绪。" -ForegroundColor Green

# 拷 DDL 进容器执行（PowerShell 不支持 stdin 重定向 docker exec，故 docker cp）
Write-Host "==> 执行 migrations/001_init.sql ..." -ForegroundColor Cyan
docker cp migrations/001_init.sql zhl-pg:/tmp/001_init.sql
docker cp migrations/verify_001.sql zhl-pg:/tmp/verify_001.sql
docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl -v ON_ERROR_STOP=1 -f /tmp/001_init.sql | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { Write-Host "!! 001_init.sql 执行失败" -ForegroundColor Red; exit 1 }
Write-Host "    DDL 执行完成（12 表 + 扩展 + 触发器 + 种子）。" -ForegroundColor Green

Write-Host "==> 执行 migrations/verify_001.sql（8 项约束断言）..." -ForegroundColor Cyan
docker exec -e PGCLIENTENCODING=UTF8 zhl-pg psql -U postgres -d zhl -f /tmp/verify_001.sql | Select-String "OK:|VERIFY DONE"
if ($LASTEXITCODE -ne 0) { Write-Host "!! verify_001.sql 存在断言失败" -ForegroundColor Red; exit 1 }
Write-Host "    约束断言全部通过。" -ForegroundColor Green

Write-Host ""
Write-Host "==> 完成。PostgreSQL 15 运行于 localhost:15432，库 zhl，账号 postgres。" -ForegroundColor Green
Write-Host "    应用连接串：postgresql+psycopg://postgres:postgres@localhost:15432/zhl（见 .env ZHL_DATABASE_URL）"
Write-Host "    停容器：docker stop zhl-pg   清容器：docker rm -f zhl-pg"
