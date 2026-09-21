@echo off
setlocal
echo START_ERRORLEVEL=%ERRORLEVEL%
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
echo AFTER_SET=%ERRORLEVEL% PS_EXE=%PS_EXE%
if not exist "%PS_EXE%" set "PS_EXE=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
echo AFTER_IF1=%ERRORLEVEL%
if not exist "%PS_EXE%" set "PS_EXE=%SystemRoot%\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
echo AFTER_IF2=%ERRORLEVEL%
if not exist "%PS_EXE%" (
    for /f "delims=" %%P in ('where powershell 2^>nul') do (
        if not exist "%PS_EXE%" set "PS_EXE=%%P"
    )
)
echo AFTER_FOR=%ERRORLEVEL%
if not exist "%PS_EXE%" (
    echo MISSING
    exit /b 1
)
echo AFTER_EXIST_CHECK=%ERRORLEVEL%
net session >nul 2>&1
echo AFTER_NET=%ERRORLEVEL%
if %errorlevel% neq 0 (
    echo WOULD_REQUEST_ADMIN
) else (
    echo WOULD_RUN_SCRIPT
)
