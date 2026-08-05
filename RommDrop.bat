@echo off
:: %~dp0 is a special variable that means "the folder this bat is in"
set SCRIPT_PATH=%~dp0romm_drop.py
title RomM Drop

:: Prefer a bundled portable Python if present, else fall back to system python
set PYTHON_PATH=%~dp0python\python.exe
if not exist "%PYTHON_PATH%" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [RomM Drop] No Python found. Install Python 3.11+ and re-run.
        pause
        exit /b 1
    )
    set PYTHON_PATH=python
)

cd /d %~dp0
echo Starting RomM Drop...
"%PYTHON_PATH%" "%SCRIPT_PATH%"
pause