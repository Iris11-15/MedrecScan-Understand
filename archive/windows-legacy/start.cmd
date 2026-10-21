@echo off
rem ============================================================
rem  Double-click this file to start the document OCR UI.
rem  It is just a wrapper around start.ps1.
rem
rem  NOTE: keep this file pure ASCII.
rem  cmd.exe does not reliably parse multi-byte (UTF-8/Chinese)
rem  characters inside .cmd files -- the bytes get split and
rem  fragments end up being executed as commands.
rem  All Chinese output comes from start.ps1 via PowerShell.
rem ============================================================
setlocal

set "PS1=%~dp0start.ps1"

if not exist "%PS1%" (
    echo [ERROR] start.ps1 not found
    echo         expected at: %PS1%
    pause
    exit /b 1
)

rem Prefer PowerShell 7 (better UTF-8 handling); fall back to 5.1
where pwsh >nul 2>nul
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
)
set RC=%errorlevel%

rem Only hold the window on failure (or Ctrl+C), so the message
rem stays readable. On a clean exit there is nothing to read.
if not "%RC%"=="0" (
    echo.
    echo Launcher exited with code %RC%
    pause
)
