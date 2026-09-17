@echo off
rem ===================================================================
rem  Study-abroad agency AI assistant - one-click launcher (Windows)
rem
rem  Double-click to start, or run with arguments:
rem      start.bat                 start backend + frontend, open browser
rem      start.bat --reseed        rebuild demo data first
rem      start.bat --smoke         run smoke test after startup
rem      start.bat --headless      no console windows, logs in .run\logs
rem      start.bat --selfcheck     start -> health check -> stop (CI mode)
rem      start.bat --dify-check    probe every Dify app key (run before live)
rem      start.bat --status        show current status
rem      start.bat stop            stop both services
rem      start.bat --help          full option list
rem
rem  This file is intentionally ASCII-only: cmd.exe reads .bat as ANSI,
rem  so non-ASCII text here would be garbled. All Chinese output comes
rem  from scripts\launcher.py, which handles UTF-8 itself.
rem ===================================================================

chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem Pause only when double-clicked (no arguments), so CLI use stays scriptable.
set "PAUSE_ON_ERR="
if "%~1"=="" set "PAUSE_ON_ERR=1"

if "%~1"=="stop" (
  set "ARGS=--stop"
) else (
  set "ARGS=%*"
)

rem ---- locate a usable Python (prefer the isolated env that has deps) ----
set "PYEXE="
set "PYARG="

set "CAND=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if exist "!CAND!" set "PYEXE=!CAND!"

if not defined PYEXE (
  where python >nul 2>nul
  if !errorlevel!==0 set "PYEXE=python"
)

if not defined PYEXE (
  where py >nul 2>nul
  if !errorlevel!==0 (
    set "PYEXE=py"
    set "PYARG=-3"
  )
)

if not defined PYEXE (
  echo.
  echo  [x] Python 3.11+ not found.
  echo      Install Python, or pass the path:
  echo        start.bat --python "C:\path\to\python.exe"
  echo.
  pause
  exit /b 2
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if defined PYARG (
  %PYEXE% %PYARG% -X utf8 "scripts\launcher.py" !ARGS!
) else (
  "%PYEXE%" -X utf8 "scripts\launcher.py" !ARGS!
)

set "RC=!errorlevel!"

rem NOTE: no parentheses in echo text below. Inside an "if ( ... )" block an
rem unescaped ")" closes the block early and cmd reports
rem "was unexpected at this time".
if not "!RC!"=="0" (
  echo.
  echo  ---- launcher exited with code !RC! - see messages above ----
)

rem Double-clicked: keep this window open so the banner / URLs stay readable.
rem Interactive use with arguments: return immediately.
if defined PAUSE_ON_ERR (
  echo.
  echo  Press any key to close this window. Services keep running.
  pause >nul
)

endlocal & exit /b %RC%
