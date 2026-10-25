@echo off
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 agent_computer_bridge.py record-ui %*
    exit /b 0
)
py -3 agent_computer_bridge.py record-ui %*
endlocal
