@echo off
setlocal
cd /d "%~dp0"
call "%~dp0agent.cmd" setup --with-wgc
exit /b %errorlevel%
