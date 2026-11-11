@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Easel Stopper

set "EASEL_ROOT=%~dp0"
set "EASEL_PORT=7860"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop';" ^
    "$root=(Resolve-Path -LiteralPath $env:EASEL_ROOT).Path;" ^
    "$stopped=$false;" ^
    "$listener=Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1;" ^
    "if($listener){ $proc=Get-CimInstance Win32_Process -Filter ('ProcessId='+$listener.OwningProcess); $cmd=[string]$proc.CommandLine; if($cmd.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -lt 0){ Write-Host ('[ERROR] Port 7860 belongs to another program. PID='+$listener.OwningProcess); exit 2 }; Write-Host ('Stopping Easel PID '+$listener.OwningProcess+'...'); & taskkill.exe /PID $listener.OwningProcess /T /F | Out-Null; $stopped=$true };" ^
    "$wrappers=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -and $_.CommandLine.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.CommandLine -like '*easel.cli web*--port 7860*' });" ^
    "foreach($wrapper in $wrappers){ Write-Host ('Stopping launcher PID '+$wrapper.ProcessId+'...'); & taskkill.exe /PID $wrapper.ProcessId /T /F | Out-Null; $stopped=$true };" ^
    "$deadline=(Get-Date).AddSeconds(15); while((Get-Date) -lt $deadline -and (Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue)){ Start-Sleep -Milliseconds 500 };" ^
    "if(Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue){ Write-Host '[ERROR] Port 7860 is still listening.'; exit 3 };" ^
    "if($stopped){ Write-Host 'Easel stopped.' } else { Write-Host 'Easel was not running.' }; exit 0"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [ERROR] Stop failed. Exit code: %RC%
    echo.
    pause
    exit /b %RC%
)

ping 127.0.0.1 -n 3 >nul
exit /b 0
