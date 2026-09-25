@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0scripts\setup_windows_cdp.py" goto missing
py -3 "%~dp0scripts\setup_windows_cdp.py" uninstall
if errorlevel 1 goto failed
echo.
echo CDP launcher removal completed.
echo The original Codex application has not been removed.
pause
exit /b 0

:missing
echo The uninstaller files are missing. Keep this file inside the extracted package.
pause
exit /b 1

:failed
echo CDP launcher removal did not complete. Share the error above with your Codex agent.
pause
exit /b 1
