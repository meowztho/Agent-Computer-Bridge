@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Bootstrap uses stdlib only; never import the recorder before setup.
where py >nul 2>nul
if errorlevel 1 goto python_missing
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 2)" >nul 2>nul
if errorlevel 1 goto python_missing

if /I "%~1"=="--repair" (
    echo Rechecking and repairing recorder dependencies...
    py -3 "%~dp0agent_setup.py" recorder-ready --repair
) else (
    py -3 "%~dp0agent_setup.py" recorder-ready
)
if errorlevel 1 goto setup_failed

rem A safe check-only command is useful before real GUI use.
if /I "%~1"=="--check" (
    echo Recorder dependencies are ready.
    exit /b 0
)

rem First run can require installation; subsequent runs use the receipt.
where pyw >nul 2>nul
if errorlevel 1 goto console_launch
if /I "%~1"=="--repair" (
    start "" pyw -3 "%~dp0agent_computer_bridge.py" record-ui
) else (
    start "" pyw -3 "%~dp0agent_computer_bridge.py" record-ui %*
)
if errorlevel 1 goto launch_failed
exit /b 0

:console_launch
echo Pythonw is unavailable. Starting recorder with the console Python runtime.
if /I "%~1"=="--repair" (
    py -3 "%~dp0agent_computer_bridge.py" record-ui
) else (
    py -3 "%~dp0agent_computer_bridge.py" record-ui %*
)
exit /b %errorlevel%

:python_missing
echo [ERROR] Windows Python 3.10 or newer with the py launcher is required.
echo Install Python from https://www.python.org/downloads/windows/
echo Then run record.cmd again. The needed libraries will be installed automatically.
goto show_error

:setup_failed
echo [ERROR] Recorder setup could not complete.
echo Check the displayed error and your internet connection.
echo You can retry using record.cmd --repair
goto show_error

:launch_failed
echo [ERROR] Recorder GUI could not be started.
echo Try record.cmd --repair or run record.cmd --check
:show_error
echo.
pause
exit /b 2
