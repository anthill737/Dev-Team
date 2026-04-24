@echo off
REM Dev Team — one-click launcher for Windows.
REM
REM This file just hands off to the PowerShell script that does the real work.
REM PowerShell handles error cases, pretty output, and auto-opens the browser.
REM
REM If Windows warns about running this file, right-click it, choose Properties,
REM and check "Unblock" at the bottom. This is a standard Windows security
REM warning for files downloaded from the internet.

SETLOCAL

REM Get the directory this .bat lives in (handles paths with spaces).
SET "SCRIPT_DIR=%~dp0"

REM Run the PowerShell launcher with an execution policy bypass scoped to this
REM process only. This does NOT change your system's global PowerShell policy.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\launch.ps1"

REM If PowerShell exited with an error, keep the window open so the user can
REM read what went wrong.
IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo [Dev Team exited with an error. Press any key to close this window.]
    pause > nul
)

ENDLOCAL
