@echo off
setlocal
cd /d "%~dp0"

rem Dependency inspection must work BEFORE importing the desktop bridge.
where py >nul 2>nul
if errorlevel 1 goto python_missing
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 2)" >nul 2>nul
if errorlevel 1 goto python_missing

if /I "%~1"=="doctor" (
    py -3 "%~dp0agent_setup.py" %*
    exit /b %errorlevel%
)
if /I "%~1"=="check" (
    py -3 "%~dp0agent_setup.py" %*
    exit /b %errorlevel%
)
if /I "%~1"=="setup" (
    py -3 "%~dp0agent_setup.py" %*
    exit /b %errorlevel%
)
if /I "%~1"=="help" (
    py -3 "%~dp0agent_setup.py" help
    exit /b %errorlevel%
)
if /I "%~1"=="start" (
    rem Print just one JSON object on success; print missing dependencies if not ready.
    py -3 "%~dp0agent_setup.py" check >nul
    if errorlevel 1 (
        py -3 "%~dp0agent_setup.py" check
        exit /b 2
    )
)
py -3 "%~dp0agent_cli.py" %*
exit /b %errorlevel%

:python_missing
echo {"ok":false,"error_code":"PYTHON_REQUIRED","error":"Install Python 3.10+ for Windows with the py launcher, then run agent.cmd doctor and agent.cmd setup.","python_download":"https://www.python.org/downloads/windows/","mcp_required":false}
exit /b 2
