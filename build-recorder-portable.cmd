@echo off
setlocal
cd /d "%~dp0"
py -3 tools\build_portable_recorder.py
exit /b %errorlevel%
