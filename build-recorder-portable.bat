@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto failed

py -3 --version >nul 2>&1
if errorlevel 1 (
    echo Python 3 mit dem Windows-Python-Launcher ^(py^) fehlt.
    goto failed
)

echo Installiere oder pruefe die Build-Abhaengigkeiten...
py -3 -m pip install -r requirements.txt -r requirements-portable.txt
if errorlevel 1 goto failed

echo Erstelle die portable Recorder-EXE...
call "%~dp0build-recorder-portable.cmd"
if errorlevel 1 goto failed

echo.
echo Fertig. Die EXE liegt im Ordner "%~dp0portable".
echo Fuer den Testrechner nur die EXE kopieren. Dort ist kein Python erforderlich.
if /i not "%~1"=="/nopause" pause
exit /b 0

:failed
echo.
echo Der Build ist fehlgeschlagen. Details stehen oben im Fenster.
if /i not "%~1"=="/nopause" pause
exit /b 1
