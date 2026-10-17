@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0install.ps1" %*
exit /b %errorlevel%
