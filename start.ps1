# TradingBot v3 - PowerShell Launcher
# Run: powershell -ExecutionPolicy Bypass -File start.ps1

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir  = Join-Path $ScriptDir "backend"
$FrontendDir = Join-Path $ScriptDir "frontend"
$OutputDir   = Join-Path $ScriptDir "output"
$EnvFile     = Join-Path $ScriptDir ".env"
$VenvDir     = Join-Path $ScriptDir ".venv"
$VenvPython  = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip     = Join-Path $VenvDir "Scripts\pip.exe"

Write-Host ""
Write-Host "  TradingBot v3 - Starting" -ForegroundColor Cyan
Write-Host ""

# 1. Check for .env
if (-not (Test-Path $EnvFile)) {
    $example = Join-Path $ScriptDir ".env.example"
    Copy-Item $example $EnvFile
    Write-Host "  [!!] .env created from .env.example" -ForegroundColor Yellow
    Write-Host "  [!!] Add your Alpaca keys to .env then re-run" -ForegroundColor Yellow
    notepad $EnvFile
    exit
}

# 2. Load .env into session
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $kv = $line.Split("=", 2)
        if ($kv.Length -eq 2) {
            [System.Environment]::SetEnvironmentVariable($kv[0].Trim(), $kv[1].Trim(), "Process")
        }
    }
}
Write-Host "  [OK] .env loaded" -ForegroundColor Green

# 3. Output dir
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}
Write-Host "  [OK] Output dir ready" -ForegroundColor Green

# 4. Python check
$PythonCmd = $null
foreach ($p in @("python", "python3", "py")) {
    try {
        $v = & $p --version 2>&1
        if ("$v" -match "Python 3") {
            $PythonCmd = $p
            break
        }
    } catch {}
}
if (-not $PythonCmd) {
    Write-Host "  [XX] Python 3 not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}
Write-Host "  [OK] $( & $PythonCmd --version 2>&1 )" -ForegroundColor Green

# 5. Create virtualenv if needed
if (-not (Test-Path $VenvPython)) {
    Write-Host "  [>>] Creating virtualenv..." -ForegroundColor Cyan
    & $PythonCmd -m venv $VenvDir
}
Write-Host "  [OK] Virtualenv ready" -ForegroundColor Green

# 6. Install backend deps
Write-Host "  [>>] Installing backend dependencies..." -ForegroundColor Cyan
$reqFile = Join-Path $BackendDir "requirements.txt"
& $VenvPip install --upgrade pip --quiet
& $VenvPip install -r $reqFile --quiet
Write-Host "  [OK] Backend dependencies installed" -ForegroundColor Green

# 7. Node / frontend
$SkipFrontend = $false
try {
    $nv = & node --version 2>&1
    Write-Host "  [OK] Node $nv found" -ForegroundColor Green

    $nm = Join-Path $FrontendDir "node_modules"
    if (-not (Test-Path $nm)) {
        Write-Host "  [>>] Installing frontend dependencies..." -ForegroundColor Cyan
        Push-Location $FrontendDir
        & npm install --silent
        Pop-Location
    }

    $localEnv = Join-Path $FrontendDir ".env.local"
    $localEx  = Join-Path $FrontendDir ".env.local.example"
    if (-not (Test-Path $localEnv) -and (Test-Path $localEx)) {
        Copy-Item $localEx $localEnv
    }
    Write-Host "  [OK] Frontend ready" -ForegroundColor Green
} catch {
    Write-Host "  [!!] Node not found - frontend will not start" -ForegroundColor Yellow
    $SkipFrontend = $true
}

# 8. Launch
$ApiPort = if ($env:API_PORT) { $env:API_PORT } else { "8000" }
$WsPort  = if ($env:WS_PORT)  { $env:WS_PORT  } else { "8765" }

Write-Host ""
Write-Host "  Backend API  -> http://localhost:$ApiPort" -ForegroundColor White
Write-Host "  WebSocket    -> ws://localhost:$WsPort"    -ForegroundColor White
if (-not $SkipFrontend) {
    Write-Host "  Frontend UI  -> http://localhost:3000"   -ForegroundColor White
}
Write-Host ""

# Start backend window
Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "Set-Location '$BackendDir'; Write-Host 'Starting backend...' -ForegroundColor Cyan; & '$VenvPython' botv3.py"
)

# Start frontend window
if (-not $SkipFrontend) {
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location '$FrontendDir'; Write-Host 'Starting frontend...' -ForegroundColor Cyan; npm run dev"
    )
}

Write-Host "  [OK] Launched. Opening browser in 4 seconds..." -ForegroundColor Green
Start-Sleep -Seconds 4

if (-not $SkipFrontend) {
    Start-Process "http://localhost:3000"
} else {
    Start-Process "http://localhost:$ApiPort/docs"
}
