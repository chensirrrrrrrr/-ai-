@echo off
rem Stop both services started by start.bat.
rem Separate double-clickable entry; just forwards to start.bat.
chcp 65001 >nul
call "%~dp0start.bat" --stop
echo.
echo  Press any key to close this window.
pause >nul
