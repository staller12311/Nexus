# Nexus desktop packaging (Windows)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1 -Variant Personal
#   powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1 -Variant Distribution
#
# Personal:
#   - config.yaml + .env + full .venv
#   - run_python_code uses the bundled venv (can import what is installed there)
#
# Distribution:
#   - config.example.yaml + empty .env.example
#   - portable Python only, no venv and no real API keys
#
# Security: the Personal installer contains the local .env with real keys.
# Never publish it. Use -Variant Distribution (or -NoSecrets) for sharing.

[CmdletBinding()]
param(
    [ValidateSet('Both', 'Personal', 'Distribution')]
    [string]$Variant = 'Both',
    [switch]$SkipBackend,
    [switch]$SkipInstaller,
    [switch]$NoSecrets
)

$ErrorActionPreference = 'Stop'
# Tauri needs cargo/rustc; installer paths may not be in a fresh shell.
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $RepoRoot 'frontend'
$Packaging = Join-Path $RepoRoot 'packaging'
$VenvRoot = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvRoot 'Scripts\python.exe'
# 用 `python -m PyInstaller` 调用，而不是依赖 Scripts\pyinstaller.exe：
# 安装方式不同（例如 pip --target）会把启动器放到 site-packages\bin，
# 那样 exe 不存在但模块可用，脚本不该因此报错。
$DistBackend = Join-Path $Packaging 'dist\nexus-backend'
$ResourcesBackend = Join-Path $Frontend 'src-tauri\resources\backend'
$PythonEmbed = Join-Path $Packaging 'python-embed'
$InstallerDir = Join-Path $Frontend 'src-tauri\target\release\bundle\nsis'
# 版本号单一来源：tauri.conf.json。以前这里硬编码，升版本时容易漏改，
# 导致脚本去找一个不存在的旧文件名。
$AppVersion = (Get-Content (Join-Path $Frontend 'src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json).version

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

if (-not (Test-Path $VenvPython)) {
    throw "找不到 .venv Python：$VenvPython"
}

$variantsToBuild = switch ($Variant) {
    'Both' { @('Personal', 'Distribution') }
    default { @($Variant) }
}

# ---------------------------------------------------------------- 1. backend
Write-Step '1/4 PyInstaller backend'
if (-not $SkipBackend) {
    & $VenvPython -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw ("venv 里没有 PyInstaller。先执行：" + [Environment]::NewLine +
            "    `"$VenvPython`" -m pip install pyinstaller")
    }
    & $VenvPython -m PyInstaller -y --distpath (Join-Path $Packaging 'dist') --workpath (Join-Path $Packaging 'build') (Join-Path $Packaging 'nexus-backend.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码 $LASTEXITCODE" }
}
if (-not (Test-Path (Join-Path $DistBackend 'nexus-backend.exe'))) {
    throw "找不到打包产物：$DistBackend\nexus-backend.exe"
}

# ------------------------------------------------------- 2. portable python
Write-Step '2/4 Prepare runtimes'
if (-not (Test-Path (Join-Path $PythonEmbed 'python.exe'))) {
    $PythonZip = Join-Path $env:TEMP 'python-3.11.9-embed-amd64.zip'
    try {
        Write-Host 'downloading portable Python 3.11.9 ...'
        Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile $PythonZip
        Expand-Archive -LiteralPath $PythonZip -DestinationPath $PythonEmbed -Force
    } catch {
        Write-Warning "便携 Python 下载失败：$($_.Exception.Message)"
    }
}
if (-not (Test-Path (Join-Path $PythonEmbed 'python.exe'))) {
    throw '缺少 packaging\python-embed\python.exe，无法准备分发版的 run_python_code。'
}

function Stage-BackendResources([string]$Kind) {
    if (Test-Path $ResourcesBackend) {
        Get-ChildItem -LiteralPath $ResourcesBackend -Force | Remove-Item -Recurse -Force
    } else {
        New-Item -ItemType Directory -Force -Path $ResourcesBackend | Out-Null
    }

    Copy-Item -Path (Join-Path $DistBackend '*') -Destination $ResourcesBackend -Recurse -Force

    if ($Kind -eq 'Personal') {
        Copy-Item -LiteralPath (Join-Path $RepoRoot 'config.yaml') -Destination (Join-Path $ResourcesBackend '_internal\config.example.yaml') -Force
        $envSource = Join-Path $RepoRoot '.env'
        if ($NoSecrets -or -not (Test-Path $envSource)) {
            $envSource = Join-Path $RepoRoot '.env.example'
        }
        Copy-Item -LiteralPath $envSource -Destination (Join-Path $ResourcesBackend '.env') -Force
        if (-not $NoSecrets) {
            Write-Host 'WARNING: Personal 安装包包含 .env 真实密钥，只供自用。' -ForegroundColor Yellow
        }

        if (Test-Path $VenvRoot) {
            Copy-Item -LiteralPath $VenvRoot -Destination (Join-Path $ResourcesBackend 'venv') -Recurse -Force
        } else {
            Write-Warning '缺少 .venv；Personal 版将回退到便携 Python。'
        }
    } else {
        Copy-Item -LiteralPath (Join-Path $RepoRoot 'config.example.yaml') -Destination (Join-Path $ResourcesBackend '_internal\config.example.yaml') -Force
        # Distribution 使用空模板；用户把真实密钥写到数据目录的 .env。
        Copy-Item -LiteralPath (Join-Path $RepoRoot '.env.example') -Destination (Join-Path $ResourcesBackend '.env') -Force
    }

    # 便携 Python 始终带上；Personal 版优先用 venv。
    Copy-Item -LiteralPath $PythonEmbed -Destination (Join-Path $ResourcesBackend 'python') -Recurse -Force
    Set-Content -LiteralPath (Join-Path $ResourcesBackend 'README.md') -Value 'Build-time resources: run scripts/build-desktop.ps1 to populate.' -Encoding UTF8

    $backendSize = (Get-ChildItem $ResourcesBackend -Recurse -File | Measure-Object Length -Sum).Sum / 1048576
    $backendSizeMb = [math]::Round($backendSize, 1)
    Write-Host "$Kind backend resources: $backendSizeMb MB"
}

function Build-Variant([string]$Kind) {
    Write-Step "3/4 Stage $Kind resources"
    Stage-BackendResources $Kind

    if ($SkipInstaller) {
        Write-Host "resources staged for $Kind (installer skipped)"
        return
    }

    Write-Step "4/4 Tauri build ($Kind)"
    Push-Location $Frontend
    try {
        & npm run desktop:build
        if ($LASTEXITCODE -ne 0) { throw "tauri build 失败，退出码 $LASTEXITCODE" }
    } finally {
        Pop-Location
    }

    $rawPath = Join-Path $InstallerDir "Nexus 智枢_${AppVersion}_x64-setup.exe"
    if (-not (Test-Path $rawPath)) {
        $raw = Get-ChildItem $InstallerDir -Filter '*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $raw) { throw "找不到 NSIS 输出：$InstallerDir" }
        $rawPath = $raw.FullName
    }
    $suffix = if ($Kind -eq 'Personal') { 'personal' } else { 'distribution' }
    $target = Join-Path $InstallerDir ("Nexus 智枢_" + $AppVersion + "_x64-" + $suffix + '-setup.exe')
    Copy-Item -LiteralPath $rawPath -Destination $target -Force
    $sizeMb = [math]::Round((Get-Item -LiteralPath $target).Length / 1048576, 1)
    Write-Host ("$Kind installer: $target  ($sizeMb MB)") -ForegroundColor Green
}

foreach ($kind in $variantsToBuild) {
    Build-Variant $kind
}

Write-Step 'Done'
if (-not $SkipInstaller) {
    Get-ChildItem $InstallerDir -Filter '*-setup.exe' | Where-Object { $_.Name -match '-(personal|distribution)-' } | ForEach-Object {
        $sizeMb = [math]::Round($_.Length / 1048576, 1)
        Write-Host ($_.FullName + "  (" + $sizeMb + " MB)") -ForegroundColor Green
    }
}

exit 0