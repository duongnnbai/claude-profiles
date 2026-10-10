@echo off
rem Adds "Claude Profiles" shortcuts to the Desktop and the Start menu.
cd /d "%~dp0"
call :python -m claude_profiles --install
pause
exit /b

:python
where py >nul 2>nul && (py -3 %* & exit /b)
where python >nul 2>nul && (python %* & exit /b)
echo Python 3.9 or later is needed. Install it from https://www.python.org/downloads/
exit /b 1
