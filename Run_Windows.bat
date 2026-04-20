@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py freecloud_cli.py
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python freecloud_cli.py
    goto done
)

echo Python was not found.
echo Install Python 3 from https://www.python.org/downloads/

:done
echo.
echo Press any key to close this window.
pause >nul
