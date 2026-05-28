@echo off
title US Visa Slot Monitor
color 0A

echo ============================================================
echo   US Visa Scheduling — Slot Monitor
echo ============================================================
echo.

:: ── Check Python ─────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo         Download it from https://www.python.org/downloads/
    echo         Make sure to tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] %PYVER% found

:: ── Install / upgrade dependencies ───────────────────────────
echo.
echo [INFO] Checking dependencies ...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)
echo [OK] Dependencies ready

:: ── Check Chrome ─────────────────────────────────────────────
set CHROME_FOUND=0
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe"       set CHROME_FOUND=1
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set CHROME_FOUND=1

if "%CHROME_FOUND%"=="0" (
    echo.
    echo [WARN] Google Chrome not found in the default location.
    echo        Download it from https://www.google.com/chrome/
    echo        Or set CHROME_PATH in .env to your chrome.exe path.
    echo.
    pause
)

:: ── Run the monitor ───────────────────────────────────────────
echo.
echo [INFO] Starting monitor ...
echo        Press Ctrl+C to stop.
echo.
python usvisa_slot_monitor.py
if errorlevel 1 (
    echo.
    echo [ERROR] The monitor exited with an error (see above).
)

echo.
pause
