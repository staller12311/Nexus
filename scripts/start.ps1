#Requires -Version 5.1
# MyAi 一键启动器：后端（uvicorn）+ 前端（Vite）+ 自动开浏览器 + 退出清理。
# 由 start.bat 调用；请不要直接双击本文件（缺少 bat 的路径上下文也没关系，脚本自己定位仓库根）。

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

$systemPython = 'D:\python3.11\python.exe'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$sitePackages = Join-Path $root '.venv\Lib\site-packages'
$dataDir = Join-Path $root 'data'
$frontendDir = Join-Path $root 'frontend'

function Write-Info([string]$message) { Write-Host "[MyAi] $message" -ForegroundColor Cyan }
function Write-Warn([string]$message) { Write-Host "[MyAi] $message" -ForegroundColor Yellow }
function Write-Err([string]$message) { Write-Host "[MyAi] $message" -ForegroundColor Red }

if (-not (Test-Path -LiteralPath $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}

# ---- 选择 Python：优先系统 Python（可用外部工作区），退回 venv ----
if (Test-Path -LiteralPath $systemPython) {
    $python = $systemPython
} elseif (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
    Write-Warn '未找到 D:\python3.11\python.exe，改用 .venv Python（写外部工作区可能受完整性级别限制）'
} else {
    Write-Err '找不到 Python：D:\python3.11\python.exe 与 .venv\Scripts\python.exe 都不存在'
    exit 1
}
if (-not (Test-Path -LiteralPath $sitePackages)) {
    Write-Warn "未找到 $sitePackages，PYTHONPATH 可能不完整"
}
$env:PYTHONPATH = $sitePackages

if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    Write-Err '找不到 npm.cmd：请确认 Node.js 已安装且在 PATH 中'
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'node_modules'))) {
    Write-Warn 'frontend\node_modules 不存在；首次使用请先在 frontend 目录执行 npm install'
}

# ---- 端口占用检测（TCP 连接探测，不依赖 PowerShell 网络模块） ----
function Test-PortListening([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

foreach ($port in 8000, 5173) {
    if (Test-PortListening $port) {
        Write-Err "端口 $port 已被占用：请先关闭已经在运行的服务（任务管理器结束对应 node/python 进程）后再启动"
        exit 1
    }
}

# ---- 启动后端（同一控制台、后台静默，日志进 data\） ----
$backendOut = Join-Path $dataDir 'backend.log'
$backendErr = Join-Path $dataDir 'backend.err.log'
$frontendOut = Join-Path $dataDir 'frontend.log'
$frontendErr = Join-Path $dataDir 'frontend.err.log'

Write-Info "启动后端：$python -m uvicorn backend.main:app --port 8000"
$backend = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', '8000') `
    -WorkingDirectory $root -NoNewWindow -PassThru `
    -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr

Write-Info '启动前端：npm run dev（frontend）'
$frontend = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/d', '/c', 'npm run dev') `
    -WorkingDirectory $frontendDir -NoNewWindow -PassThru `
    -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr

# ---- 优雅退出：Ctrl+C / 关窗口 / 服务异常退出都会走到这里 ----
$global:MyAiStopping = $false
$global:MyAiCleaned = $false

try {
    $cancelHandler = [ConsoleCancelEventHandler] {
        param($sender, $eventArgs)
        $eventArgs.Cancel = $true   # 阻止 PowerShell 直接退出，留出清理时间
        $global:MyAiStopping = $true
    }
    [Console]::add_CancelKeyPress($cancelHandler)
} catch {
    Write-Warn '当前控制台不支持 Ctrl+C 事件挂钩，将依赖关窗口时的进程树清理'
}

function Stop-PortOwner([int]$port) {
    $ownerPids = @()
    try {
        $ownerPids = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop |
            ForEach-Object { [int]$_.OwningProcess })
    } catch {
        try {
            $ownerPids = @(& netstat.exe -ano -p tcp 2>$null |
                Select-String -Pattern ":$port\s" |
                Where-Object { $_.Line -match 'LISTENING' } |
                ForEach-Object {
                    $fields = $_.Line.Trim() -split '\s+'
                    if ($fields[-1] -match '^\d+$') { [int]$fields[-1] }
                })
        } catch { $ownerPids = @() }
    }
    foreach ($ownerPid in ($ownerPids | Sort-Object -Unique)) {
        if ($ownerPid -gt 0 -and $ownerPid -ne $PID) {
            & taskkill.exe /PID $ownerPid /T /F 2>$null | Out-Null
        }
    }
}

function Invoke-Cleanup {
    if ($global:MyAiCleaned) { return }
    $global:MyAiCleaned = $true
    Write-Info '正在停止后端与前端 ...'
    foreach ($proc in @($backend, $frontend)) {
        if ($null -ne $proc) {
            try {
                if (-not $proc.HasExited) {
                    & taskkill.exe /PID $proc.Id /T /F 2>$null | Out-Null
                }
            } catch { }
        }
    }
    # 兜底：按端口找到残留监听进程（防止 npm->node->vite 这类多级子进程漏网）
    Stop-PortOwner 8000
    Stop-PortOwner 5173
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    Write-Info '已退出：后端与前端进程已停止，端口 8000 / 5173 已释放。'
}

try {
    Write-Info '等待服务就绪 ...'
    Start-Sleep -Seconds 3
    if (-not (Test-PortListening 5173)) {
        for ($i = 0; $i -lt 24; $i++) {
            if (Test-PortListening 5173) { break }
            Start-Sleep -Milliseconds 500
        }
    }

    if (Test-PortListening 5173) {
        Write-Info '打开浏览器：http://localhost:5173'
        try { Start-Process 'http://localhost:5173' | Out-Null } catch { Write-Warn '自动打开浏览器失败，请手动访问 http://localhost:5173' }
    } else {
        Write-Warn '前端 15 秒内还没就绪，浏览器未自动打开；请查看 data\frontend.log'
    }

    Write-Host ''
    Write-Info '后端：http://127.0.0.1:8000  （日志 data\backend.log）'
    Write-Info '前端：http://localhost:5173      （日志 data\frontend.log）'
    Write-Info '停止服务：按 Ctrl+C，或直接关闭本窗口。'
    Write-Info '（若按 Ctrl+C 后出现 Terminate batch job (Y/N)?，按 Y 即可：此时服务已清理完毕）'
    Write-Host ''

    while (-not $global:MyAiStopping) {
        if ($backend.HasExited) {
            Write-Err '后端进程已退出（请看 data\backend.err.log）'
            break
        }
        if ($frontend.HasExited) {
            Write-Err '前端进程已退出（请看 data\frontend.err.log）'
            break
        }
        Start-Sleep -Milliseconds 400
    }
} finally {
    Invoke-Cleanup
}

exit 0
