@echo off
REM Launcher for run.ps1. Runs in this window; does not ask for administrator rights.
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

REM powershell is often missing from PATH, so other .cmd files fail with
REM "'powershell' is not recognized". Call the Windows PowerShell 5.1 exe directly.
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" set "PS_EXE=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" set "PS_EXE=%SystemRoot%\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" (
    echo [run.cmd] ERROR: powershell.exe was not found.
    echo Expected:
    echo   %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe
    pause
    exit /b 1
)

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*

echo.
echo ============================================================
echo  run.ps1 has finished. Press any key to close this window.
echo ============================================================
pause >nul
