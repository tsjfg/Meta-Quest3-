@echo off
cd /d "%~dp0"
title QuestCast - Meta Quest 3 Cast

if exist "python\python.exe" (
    "python\python.exe" "questcast.py"
    goto end
)

where python >nul 2>nul
if not errorlevel 1 (
    python "questcast.py"
    goto end
)

echo.
echo  [!] Python runtime not found.
echo      Use the original machine, or install Python 3.9+ from python.org
echo.
pause

:end
