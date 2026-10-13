# ZHL · 本地开发服务一键重启（干净端口 + uvicorn --reload）
#
# 用法：  .\scripts\restart-app.ps1
#         .\scripts\restart-app.ps1 -Port 8000
#         .\scripts\restart-app.ps1 -NoWait        # 后台起，不等就绪就返回
#
# 为什么需要这个脚本：
#   uvicorn --reload 会留下「reloader 父进程 + worker 子进程」两个 PID；
#   多次重启或窗口没关干净时，8000 端口会累积多个残留进程。Windows 上多个
#   socket 绑定同一端口时，请求会被分发给其中任意一个（实测：第 1 次 405，
#   后 4 次连接失败），表现为前端「HTTP 网络错误」——看起来像代码问题，
#   实际是端口被脏进程占着。本脚本先清干净端口，再启动，并做就绪探针。
#
# 注意：
#   - 仅限本机调试。服务无鉴权，放行/报告等写接口能改变业务状态，
#     绝不可用 --host 0.0.0.0 对外暴露。
#   - 数据库需已启动（.\scripts\start-pg.ps1）；本脚本不碰 Docker。

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$Host_ = "127.0.0.1",
    [switch]$NoWait,
    [switch]$AutoPort
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # 项目根目录

if ($Host_ -ne "127.0.0.1" -and $Host_ -ne "localhost") {
    Write-Host "!! 拒绝绑定 $Host_ —— 本服务无鉴权，写接口可改变放行等业务状态。" -ForegroundColor Red
    Write-Host "   如确需内网访问，请先加鉴权与审计，并显式修改本脚本的守卫。" -ForegroundColor Yellow
    exit 1
}

# 探测某个端口是否还能建立连接（不被任何 socket 占着）。
function Test-PortFree {
    param([int]$P)
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect("127.0.0.1", $P, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(300)) { $c.Close(); return $true }   # 连不上 = 空闲
        $c.Close()
        return $false                                                          # 连得上 = 被占
    } catch {
        return $true
    }
}

# -AutoPort：端口被占死（尤其幽灵 socket）时自动顺延到下一个空闲端口。
# Windows 上进程被杀后 socket 条目可能长期残留且tasklist 查不到，
# 此时既杀不掉也探测不通，只能换端口。
if ($AutoPort) {
    for ($p = $Port; $p -le $Port + 20; $p++) {
        if (Test-PortFree $p) {
            if ($p -ne $Port) {
                Write-Host "==> 端口 $Port 不可用（可能被幽灵 socket 占用），自动改用 $p。" -ForegroundColor Yellow
            }
            $Port = $p
            break
        }
    }
}

$base = "http://$Host_`:$Port"

# --- 1. 找项目自己的 Python 解释器 -----------------------------------------
Write-Host "==> 定位虚拟环境 ..." -ForegroundColor Cyan
$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "!! 找不到 .venv\Scripts\python.exe，请先创建虚拟环境或安装依赖。" -ForegroundColor Red
    exit 1
}
$python = (Resolve-Path $python).Path

# --- 2. 清干净端口上的残留进程 ---------------------------------------------
# 先判断端口是否真被占：未被占就别去枚举 PID，避免对着幽灵条目空转。
if (Test-PortFree $Port) {
    Write-Host "==> 端口 $Port 干净，无需清理。" -ForegroundColor Cyan
} else {
    # 按端口找 PID（而不是按命令行找 python），确保只杀真正占着端口的进程，
    # 不误伤同机器上无关的 Python 任务。
    $listeners = @()
    try {
        $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    } catch {
        # 旧版 Windows 没有 Get-NetTCPConnection，退回 netstat
        $netstat = netstat -ano | Select-String ":$Port\s+.*LISTENING"
        $listeners = $netstat | ForEach-Object {
            ($_ -split "\s+")[-1]
        } | Sort-Object -Unique
    }
    $listeners = @($listeners | Where-Object { $_ -and $_ -ne "0" })

    Write-Host "==> 端口 $Port 被占用，持有 PID：$($listeners -join ', ')" -ForegroundColor Cyan
    foreach ($procId in $listeners) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "    已停止 PID $procId" -ForegroundColor DarkGray
        } catch {
            # 分两种情况：
            #  a) 进程确实已死（幽灵 socket）—— 无从下手，只能换端口；
            #  b) 进程还活着但当前用户无权杀 —— 同样只能换端口。
            # 所以这里只记录，不当作失败（下面的端口轮询才是最终判据）。
            Write-Host "    PID $procId 无法停止（可能已死或权限不足）" -ForegroundColor DarkGray
        }
    }

    # 等待端口真正可用（最多 15s）。
    # 判据用「实际 TCP 连接 + HTTP 探针」，不信 netstat 的 LISTENING 表：
    # Windows 上进程被杀后 socket 条目可能残留，netstat 仍报 LISTENING，
    # 但实际已连不上（curl 返回 000）。只看 netstat 会误判成"没释放"。
    $released = $false
    $deadProbe = 0
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            # 能连上就说明端口还被服务占着，继续等。
            # 用 /docs（FastAPI 自带，一定存在）；连不上的话会抛异常，
            # 不能用 -ErrorAction SilentlyContinue 吞掉，否则无法区分
            # 「连不上」和「HTTP 404」。
            $null = Invoke-WebRequest -Uri "$base/docs" -UseBasicParsing -TimeoutSec 1
            $deadProbe = 0
        } catch {
            $inner = $_.Exception
            # 有 Response 说明服务器在应答（哪怕 404）→ 端口仍被占
            if ($null -ne $inner.Response) { $deadProbe = 0 } else { $deadProbe++ }
        }
        # 连续 3 次连接层失败 → 端口已空
        if ($deadProbe -ge 3) { $released = $true; break }
    }
    if (-not $released) {
        Write-Host "!! 端口 $Port 仍无法连接。多为「幽灵 socket」：进程已死但内核未回收端口条目。" -ForegroundColor Red
        Write-Host "   特征：netstat 报 LISTENING，但 tasklist / Get-CimInstance 查不到该 PID。" -ForegroundColor Yellow
        Write-Host "   这种情况本进程杀不掉（它已经不存在了），请任选其一：" -ForegroundColor Yellow
        Write-Host "     1) 换个端口： .\scripts\restart-app.ps1 -AutoPort" -ForegroundColor Green
        Write-Host "     2) 重启 Windows（唯一能真正清掉幽灵 socket 的办法）" -ForegroundColor Green
        exit 1
    }
    Write-Host "    端口 $Port 已释放。" -ForegroundColor Green
}

# --- 3. 启动 ----------------------------------------------------------------
# .env 显式读进本进程环境（app/main.py 自己也调 load_dotenv 兜底，
# 显式读是为了让变量在任何依赖环境变量的间接路径上都能拿到）。
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable(
                $matches[1].Trim(), $matches[2].Trim().Trim('"').Trim("'"))
        }
    }
    Write-Host "==> 已从 .env 载入环境变量。" -ForegroundColor DarkGray
} else {
    Write-Host "    未找到 .env（app.main 会自行 load_dotenv；缺 ZHL_DATABASE_URL 将启动失败）。" -ForegroundColor Yellow
}

Write-Host "==> 启动 uvicorn（--reload，改代码自动生效）..." -ForegroundColor Cyan
Write-Host "    $python -m uvicorn app.main:app --host $Host_ --port $Port --reload" -ForegroundColor DarkGray
Write-Host "    停止服务：Ctrl+C" -ForegroundColor DarkGray

if ($NoWait) {
    # 后台模式。注意：某些受管控的执行环境（如 CI 沙箱、容器）会连带
    # 回收 Start-Process 起的子进程，此时下方就绪探针会超时失败。
    # 本地开发请优先用默认的前台模式，日志直接可见、Ctrl+C 即可停。
    Start-Process -FilePath $python `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", $Host_, "--port", "$Port", "--reload" `
        -WorkingDirectory (Get-Location).Path
    Write-Host "    已后台启动。" -ForegroundColor Green
} else {
    # 前台运行：日志实时可见，Ctrl+C 停止。这是最常用的开发姿势。
    # uvicorn --reload 会自行管理 reloader 父进程与 worker 子进程。
    & $python -m uvicorn app.main:app --host $Host_ --port $Port --reload
    exit $LASTEXITCODE
}

# --- 4. 就绪探针 + 关键端点自检 ---------------------------------------------
Write-Host ""
Write-Host "==> 等待服务就绪 ..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $resp = Invoke-WebRequest -Uri "$base/docs" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}

if (-not $ready) {
    Write-Host "!! 服务 20s 内未就绪。若上方已打印启动错误，请先修错误。" -ForegroundColor Red
    Write-Host "   若端口被占：netstat -ano | findstr :$Port" -ForegroundColor Yellow
    exit 1
}
Write-Host "    服务已就绪：$base" -ForegroundColor Green

# 关键端点自检：新增接口是否真的挂上了（防止改完代码忘重启的假绿）
Write-Host "==> 关键端点自检 ..." -ForegroundColor Cyan
$checks = @(
    @{ Path = "/api/batches"; Name = "批次列表" },
    @{ Path = "/api/quality-rule-versions"; Name = "规则版本" },
    @{ Path = "/api/incidents"; Name = "异常列表" }
)
$allOk = $true
foreach ($c in $checks) {
    $code = "ERR"
    try {
        $r = Invoke-WebRequest -Uri "$base$($c.Path)" -UseBasicParsing -TimeoutSec 5
        $code = $r.StatusCode
    } catch {
        # 4xx 也说明路由存在（只是数据不满足），连接层错误才算失败
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    }
    $ok = ($code -ge 200 -and $code -lt 500)
    if (-not $ok) { $allOk = $false }
    $color = if ($ok) { "Green" } else { "Red" }
    Write-Host ("    {0,-12} {1,-42} [{2}]" -f $c.Name, $c.Path, $code) -ForegroundColor $color
}

Write-Host ""
if ($allOk) {
    Write-Host "==> 完成。前端首页：http://$Host_`:$Port/static/index.html" -ForegroundColor Green
    Write-Host "    接口文档：http://$Host_`:$Port/docs" -ForegroundColor Green
} else {
    Write-Host "!! 部分端点不可达。若刚改过后端代码，确认启动的是最新代码（--reload 已开）。" -ForegroundColor Red
    exit 1
}