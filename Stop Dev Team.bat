@echo off
REM Dev Team — stop launcher for Windows (local mode, no Docker).
REM
REM Cleanly shuts down the backend and frontend processes. Your project
REM data is preserved — restart with "Start Dev Team.bat" to pick back up.

SETLOCAL

SET "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\stop.ps1"

echo.
echo Press any key to close this window.
pause > nul

ENDLOCAL

