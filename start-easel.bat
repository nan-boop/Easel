@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Easel Launcher

set "EASEL_PORT=7860"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "LOGDIR=%~dp0outputs\_web"
set "LOGFILE=%LOGDIR%\web.log"

if not exist "%PYTHON%" (
    echo [ERROR] Python environment not found:
    echo         %PYTHON%
    echo         Run setup.ps1 first.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$url='http://127.0.0.1:7860/api/update';" ^
    "try { $s=Invoke-RestMethod -Uri $url -TimeoutSec 2; if($null -ne $s.version){ Write-Host ('Easel is already running: '+$s.version); exit 10 } } catch {};" ^
    "$listener=Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1;" ^
    "if($listener){ Write-Host ('[ERROR] Port 7860 is already used by PID '+$listener.OwningProcess+'.'); exit 20 }; exit 0"

set "RC=%ERRORLEVEL%"
if "%RC%"=="10" goto open_browser
if not "%RC%"=="0" (
    echo.
    echo [ERROR] Cannot start Easel. Exit code: %RC%
    echo.
    pause
    exit /b %RC%
)

if not exist "%LOGDIR%" mkdir "%LOGDIR%"
start "Easel Web" /min cmd /c ""%PYTHON%" -m easel.cli web --port 7860 >> "%LOGFILE%" 2>&1"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$url='http://127.0.0.1:7860/api/update';" ^
    "$deadline=(Get-Date).AddSeconds(90);" ^
    "while((Get-Date) -lt $deadline){ try { $s=Invoke-RestMethod -Uri $url -TimeoutSec 2; if($s.version){ Write-Host ('Easel is ready: '+$s.version); exit 0 } } catch {}; Start-Sleep -Milliseconds 500 };" ^
    "Write-Host '[ERROR] Easel did not become ready within 90 seconds.'; Write-Host 'See outputs\_web\web.log'; exit 4"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [ERROR] Startup failed. Exit code: %RC%
    echo Check: %LOGFILE%
    echo.
    pause
    exit /b %RC%
)

:open_browser
start "" "http://127.0.0.1:7860"
exit /b 0
