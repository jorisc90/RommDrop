@echo off
rem RommDrop launcher for the Xbox app
rem Changes the working dir to this script's folder, then runs the GUI.

cd /d "%~dp0"

rem Use a bundled portable Python if present, else fall back to system python
set "PYTHON=%~dp0python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

start "" %PYTHON% "%~dp0romm_drop.py"

exit /b 0