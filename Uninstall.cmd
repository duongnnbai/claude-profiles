@echo off
rem Removes the shortcuts and the private Claude copy. Profile data is kept.
cd /d "%~dp0"
call :python -m claude_profiles --uninstall
pause
exit /b

:python
where py >nul 2>nul && (py -3 %* & exit /b)
where python >nul 2>nul && (python %* & exit /b)
echo Python 3.9 or later is needed. Install it from https://www.python.org/downloads/
exit /b 1
