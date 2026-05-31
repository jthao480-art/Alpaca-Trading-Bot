@echo off
REM ============================================================
REM start.bat – One-command launcher for TradingBot v3 (Windows)
REM Usage: double-click or run in Command Prompt
REM ============================================================

setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set BACKEND_DIR=%SCRIPT_DIR%backend
set FRONTEND_DIR=%SCRIPT_DIR%frontend
set OUTPUT_DIR=%SCRIPT_DIR%output
set ENV_FILE=%SCRIPT_DIR%.env

echo.
echo  =============================================
echo   TradingBot v3 – Startup
echo  =============================================
echo.

REM ── 1. Env file ─────────────────────────────
if not exist "%ENV_FILE%" (
    echo [WARN] .env not found – copying from .env.example
    copy "%SCRIPT_DIR%.env.example" "%ENV_FILE%"
    echo [WARN] Please edit %ENV_FILE% with your Alpaca credentials.
    pause
    exit /b 1
)

REM Load env vars
for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    if not "%%A"=="" if not "%%A:~0,1%"=="#" (
        set "%%A=%%B"
    )
)

REM ── 2. Output dir ───────────────────────────
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
echo [OK] Output directory ready.

REM ── 3. Python / backend deps ────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found. Install Python 3.10+ and add to PATH.
    pause & exit /b 1
)
echo [OK] Python found.

set VENV=%SCRIPT_DIR%.venv
if not exist "%VENV%" (
    echo Creating virtualenv ...
    python -m venv "%VENV%"
)
call "%VENV%\Scripts\activate.bat"

echo Installing backend dependencies ...
pip install -q --upgrade pip
pip install -q -r "%BACKEND_DIR%\requirements.txt"
echo [OK] Backend dependencies installed.

REM ── 4. Node / frontend deps ─────────────────
where node >nul 2>&1
if errorlevel 1 (
    echo [WARN] Node.js not found. Frontend will not start.
    set SKIP_FRONTEND=1
) else (
    echo [OK] Node found.
    if not exist "%FRONTEND_DIR%\node_modules" (
        echo Installing frontend dependencies ...
        pushd "%FRONTEND_DIR%"
        call npm install --silent
        popd
    )
    if not exist "%FRONTEND_DIR%\.env.local" (
        copy "%FRONTEND_DIR%\.env.local.example" "%FRONTEND_DIR%\.env.local" >nul 2>&1
    )
    echo [OK] Frontend dependencies ready.
)

REM ── 5. Launch ───────────────────────────────
echo.
echo  Backend API  ^-^> http://localhost:%API_PORT%
echo  WebSocket    ^-^> ws://localhost:%WS_PORT%
if not defined SKIP_FRONTEND echo  Frontend UI  ^-^> http://localhost:3000
echo.

REM Start backend in new window
start "TradingBot Backend" cmd /k "cd /d %BACKEND_DIR% && call %VENV%\Scripts\activate.bat && python botv3.py"

REM Start frontend in new window
if not defined SKIP_FRONTEND (
    start "TradingBot Frontend" cmd /k "cd /d %FRONTEND_DIR% && npm run dev"
)

echo [OK] All processes launched in separate windows.
echo      Close those windows (or press Ctrl+C in each) to stop.
echo.
pause
