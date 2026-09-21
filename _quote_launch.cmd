@echo off
del "%TEMP%\recorder-quote-test.txt" 2>nul
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath $env:ComSpec -ArgumentList '/c','\"\"%~dp0_quote_test.cmd\"\" elevated' -Wait -WindowStyle Hidden"
echo LAUNCH_EXIT=%ERRORLEVEL%
type "%TEMP%\recorder-quote-test.txt"
