@echo off
setlocal
title Jarvis - Final Working

cd /d "%~dp0"

echo.
echo ==========================================
echo        JARVIS - FINAL WORKING
echo ==========================================
echo.
echo Starting Jarvis_FINAL_WORKING.py...
echo.

REM Use the Windows Python launcher so this works with the
REM Python installation used for the Jarvis setup.
py -3 "%~dp0Jarvis_FINAL_WORKING.py"

if errorlevel 1 (
    echo.
    echo ==========================================
    echo Jarvis stopped with an error.
    echo ==========================================
    echo.
    echo Press any key to close this window...
    pause >nul
)

endlocal
