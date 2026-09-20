@echo off
REM Launcher for run.ps1 - elevates and bypasses execution policy.
REM Double-click this file, or run from a terminal:  run.cmd
REM Installs pinned vfox Python + Gyan FFmpeg, then opens the recorder GUI.

setlocal

set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%run.ps1"

if not exist "%PS1%" (
    echo [run.cmd] ERROR: run.ps1 not found in:
    echo   %SCRIPT_DIR%
    pause
    exit /b 1
)

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*

echo.
echo ============================================================
echo  run.ps1 has finished. Press any key to close this window.
echo ============================================================
pause >nul
