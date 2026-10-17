@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0uninstall.ps1" %*
exit /b %errorlevel%
