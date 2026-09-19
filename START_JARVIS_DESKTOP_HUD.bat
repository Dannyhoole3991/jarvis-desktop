@echo off
setlocal
title Jarvis - Desktop HUD

cd /d "%~dp0"

echo.
echo ==========================================
echo        JARVIS - DESKTOP HUD
echo ==========================================
echo.
echo Opening the Jarvis HUD window...
echo (This window will close once the HUD has started.)
echo.

REM Hardcoded to a specific interpreter rather than plain "pythonw": this
REM machine has several pythonw.exe installs on PATH and Windows resolves
REM the bare name inconsistently -- confirmed live that it sometimes picks
REM one missing pystray/pywebview, which fails silently (no console, no
REM window, no error visible) since pythonw has no console to show it on.
set "HUD_PYTHONW=C:\Users\danny\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
if not exist "%HUD_PYTHONW%" set "HUD_PYTHONW=pythonw"

start "" "%HUD_PYTHONW%" "%~dp0Jarvis_Desktop_HUD.py"

endlocal
