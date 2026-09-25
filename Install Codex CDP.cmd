@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0scripts\setup_windows_cdp.py" goto missing
py -3 -c "import sys; assert sys.version_info >= (3, 10), sys.version"
if errorlevel 1 goto python_missing
py -3 "%~dp0scripts\setup_windows_cdp.py" app
if errorlevel 1 goto setup_failed
echo.
echo Codex CDP shortcut is ready on your desktop.
echo Fully quit Codex Desktop, then open the Codex CDP shortcut.
pause
exit /b 0

:missing
echo The installer files are missing. Keep this file inside the extracted package.
pause
exit /b 1

:python_missing
echo Python 3.10 or newer is required. Install Python and try again.
pause
exit /b 1

:setup_failed
echo CDP shortcut setup failed. Share the error above with your Codex agent.
pause
exit /b 1
