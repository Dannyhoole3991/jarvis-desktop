@echo off
setlocal
title Jarvis v2
cd /d "%~dp0"

REM Same interpreter as the old HUD (it has pywebview installed).
set "V2_PYTHONW=C:\Users\danny\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
if not exist "%V2_PYTHONW%" set "V2_PYTHONW=pythonw"

start "" "%V2_PYTHONW%" -m jarvis_v2.desktop

endlocal
