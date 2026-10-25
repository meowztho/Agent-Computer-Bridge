@echo off
setlocal
cd /d "%~dp0"
call "%~dp0agent.cmd" setup
exit /b %errorlevel%
