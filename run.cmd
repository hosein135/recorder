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

REM powershell is often missing from PATH. Use the Windows PowerShell 5.1 install path.
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" set "PS_EXE=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" set "PS_EXE=%SystemRoot%\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" (
    for /f "delims=" %%P in ('where powershell 2^>nul') do (
        if not exist "%PS_EXE%" set "PS_EXE=%%P"
    )
)
if not exist "%PS_EXE%" (
    echo [run.cmd] ERROR: powershell.exe was not found.
    echo Expected:
    echo   %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe
    pause
    exit /b 1
)

net session >nul 2>&1
if %errorlevel% neq 0 (
    if /I "%~1"=="elevated" (
        echo [run.cmd] This window is still not running as administrator.
        echo Right-click run.cmd and choose Run as administrator.
        pause
        exit /b 1
    )
    echo Requesting administrator privileges...
    echo Approve the User Account Control prompt if Windows shows one.
    REM No extra arguments: an empty -ArgumentList makes Start-Process fail and this window closes.
    "%PS_EXE%" -NoProfile -Command "Start-Process -FilePath $env:ComSpec -ArgumentList '/k','\"\"%~f0\"\" elevated %*' -Verb RunAs"
    if errorlevel 1 (
        echo.
        echo [run.cmd] Could not open an administrator window.
        pause
        exit /b 1
    )
    exit /b 0
)

if /I "%~1"=="elevated" shift

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*

echo.
echo ============================================================
echo  run.ps1 has finished. Press any key to close this window.
echo ============================================================
pause >nul
