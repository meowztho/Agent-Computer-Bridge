@echo off
setlocal
cd /d "%~dp0"
call "%~dp0agent.cmd" setup --with-mcp
exit /b %errorlevel%
