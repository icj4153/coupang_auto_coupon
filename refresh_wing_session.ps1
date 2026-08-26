param(
    [string]$NasHost = $env:COUPON_NAS_HOST,
    [int]$NasPort = $(if ($env:COUPON_NAS_PORT) { [int]$env:COUPON_NAS_PORT } else { 2022 }),
    [string]$NasUser = $env:COUPON_NAS_USER,
    [string]$NasKey = $env:COUPON_NAS_KEY,
    [string]$NasStatePath = $env:COUPON_NAS_STATE_PATH,
    [string]$LocalConfig = $env:COUPON_LOCAL_CONFIG,
    [int]$SetupTimeoutMinutes = $(if ($env:COUPON_SETUP_LOGIN_TIMEOUT_MINUTES) { [int]$env:COUPON_SETUP_LOGIN_TIMEOUT_MINUTES } else { 30 }),
    [string]$ChromeSessionProfileDir = $env:COUPON_CHROME_SESSION_PROFILE_DIR,
    [int]$ChromeRemoteDebuggingPort = $(if ($env:COUPON_CHROME_REMOTE_DEBUGGING_PORT) { [int]$env:COUPON_CHROME_REMOTE_DEBUGGING_PORT } else { 9223 })
)

$ErrorActionPreference = "Stop"

function Use-Default {
    param([string]$Value, [string]$Default)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $Default
    }
    return $Value
}

function Find-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @($py.Source, "-3")
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @($python.Source)
    }
    $python3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($python3) {
        return @($python3.Source)
    }
    throw "Python을 찾을 수 없습니다. Windows에 Python 3를 설치한 뒤 다시 실행하세요."
}

function Invoke-Python {
    param([string[]]$PythonCommand, [string[]]$Arguments)
    $exe = $PythonCommand[0]
    $prefixArgs = @()
    if ($PythonCommand.Count -gt 1) {
        $prefixArgs = $PythonCommand[1..($PythonCommand.Count - 1)]
    }
    & $exe @prefixArgs @Arguments
}

function Invoke-NasSsh {
    param([string]$RemoteCommand)
    & ssh -i $NasKey -p $NasPort -o BatchMode=yes "$NasUser@$NasHost" $RemoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "NAS SSH 명령이 실패했습니다."
    }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$HomeDir = [Environment]::GetFolderPath("UserProfile")
$NasHost = Use-Default $NasHost "icj7297.synology.me"
$NasUser = Use-Default $NasUser "joon_admin"
$NasKey = Use-Default $NasKey (Join-Path $HomeDir ".ssh\coupang_coupon_nas_actions")
$NasStatePath = Use-Default $NasStatePath "/volume1/docker/coupang_coupon/data/wing_storage_state.server.json"
$LocalConfig = Use-Default $LocalConfig "browser_coupon_config.json"
$ChromeSessionProfileDir = Use-Default $ChromeSessionProfileDir ".chrome-wing-login-profile"

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "Windows OpenSSH 클라이언트 ssh를 찾을 수 없습니다. Windows 선택적 기능에서 OpenSSH Client를 설치하세요."
}
if (-not (Test-Path -LiteralPath $NasKey)) {
    throw "NAS SSH 키를 찾을 수 없습니다: $NasKey"
}
if (-not (Test-Path -LiteralPath $LocalConfig)) {
    throw "설정 파일을 찾을 수 없습니다: $LocalConfig"
}
if (-not (Test-Path -LiteralPath "export_chrome_wing_session.py")) {
    throw "export_chrome_wing_session.py 파일을 찾을 수 없습니다. 프로젝트 폴더에서 실행하세요."
}

$PythonCommand = Find-Python
Invoke-Python -PythonCommand $PythonCommand -Arguments @("-c", "import playwright.sync_api") 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[session-refresh] Python Playwright 패키지를 설치합니다."
    Invoke-Python -PythonCommand $PythonCommand -Arguments @("-m", "pip", "install", "-r", "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Playwright 패키지 설치에 실패했습니다."
    }
}

$LocalStatePath = Invoke-Python -PythonCommand $PythonCommand -Arguments @(
    "-c",
    "import json, sys; from pathlib import Path; p=Path(sys.argv[1]).expanduser().resolve(); c=json.loads(p.read_text(encoding='utf-8')); s=Path(c.get('storage_state_path','wing_storage_state.json')).expanduser(); print((s if s.is_absolute() else p.parent / s).resolve())",
    $LocalConfig
)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($LocalStatePath)) {
    throw "storage state 경로를 계산하지 못했습니다."
}
$LocalStatePath = $LocalStatePath.Trim()

Write-Host "[session-refresh] Step 1/3: Windows Chrome에서 쿠팡 WING 로그인 세션을 새로 만듭니다."
Write-Host "[session-refresh] 브라우저가 열리면 WING 로그인을 완료한 뒤 이 창에서 Enter를 누르세요."
Invoke-Python -PythonCommand $PythonCommand -Arguments @(
    "export_chrome_wing_session.py",
    "--config", $LocalConfig,
    "--profile-dir", $ChromeSessionProfileDir,
    "--port", "$ChromeRemoteDebuggingPort",
    "--timeout-seconds", "$($SetupTimeoutMinutes * 60)"
)
if ($LASTEXITCODE -ne 0) {
    throw "Chrome 세션 추출에 실패했습니다."
}

if (-not (Test-Path -LiteralPath $LocalStatePath)) {
    throw "Storage state가 생성되지 않았습니다: $LocalStatePath"
}
if ((Get-Item -LiteralPath $LocalStatePath).Length -le 0) {
    throw "Storage state 파일이 비어 있습니다: $LocalStatePath"
}

$RemoteTmp = "$NasStatePath.tmp.$([DateTimeOffset]::Now.ToUnixTimeSeconds())"

Write-Host "[session-refresh] Step 2/3: 세션 파일을 NAS로 업로드합니다."
& cmd.exe /c "type `"$LocalStatePath`"" | ssh -i $NasKey -p $NasPort -o BatchMode=yes "$NasUser@$NasHost" "cat > '$RemoteTmp'"
if ($LASTEXITCODE -ne 0) {
    throw "세션 파일 업로드에 실패했습니다."
}

Invoke-NasSsh "set -e; mv '$RemoteTmp' '$NasStatePath'; chmod 600 '$NasStatePath' || true; rm -f /volume1/docker/coupang_coupon/data/logs/automation_paused.json; /usr/local/bin/docker start coupang-coupon-scheduler >/dev/null 2>&1 || true; ls -lh '$NasStatePath'"

Write-Host "[session-refresh] Step 3/3: 업로드 완료."
Write-Host "[session-refresh] NAS 자동화 일시정지를 해제했고, 스케줄러를 다시 켰습니다."
Write-Host "[session-refresh] NAS 자동화는 다음 실행부터 새 세션을 사용합니다."

$RunRecovery = Read-Host "[session-refresh] 실패/미완료 쿠폰을 지금 복구 실행할까요? [y/N]"
if ($RunRecovery -match "^(y|Y|yes|YES)$") {
    Write-Host "[session-refresh] NAS에서 실패/미완료 쿠폰 복구를 실행합니다."
    Invoke-NasSsh "set -e; /usr/local/bin/docker exec coupang-coupon-scheduler python3 recover_failed_runner.py"
} else {
    Write-Host "[session-refresh] 복구 실행은 건너뜁니다. 웹폼에서도 나중에 실행할 수 있습니다."
}

Write-Host "[session-refresh] 완료."
