@echo off
setlocal
cd /d "%~dp0"

if "%LOCALAPPDATA%"=="" (
    set "FREECLOUD_STATE=%USERPROFILE%\AppData\Local\FreeCloud"
) else (
    set "FREECLOUD_STATE=%LOCALAPPDATA%\FreeCloud"
)
if not exist "%FREECLOUD_STATE%" mkdir "%FREECLOUD_STATE%"
set "UI_LOG=%FREECLOUD_STATE%\ui_error.log"
break > "%UI_LOG%"

set "PYTHON_CMD="

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=py"
    goto python_found
)

where python >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=python"
    goto python_found
)

echo Python was not found.
echo Install Python 3 from https://www.python.org/downloads/
goto done

:python_found
%PYTHON_CMD% -c "import tkinter" >nul 2>nul
if %errorlevel%==0 (
    %PYTHON_CMD% freecloud_ui.py >> "%UI_LOG%" 2>&1
    set "STATUS=%errorlevel%"
    goto finished
)

echo Tkinter is not available for this Python install.
echo Starting command-line sync instead.
%PYTHON_CMD% freecloud_cli.py
set "STATUS=%errorlevel%"
goto finished

:finished
if not "%STATUS%"=="0" (
    echo FreeCloud exited with an error.
    echo See: %UI_LOG%
)

:done
echo.
echo Press any key to close this window.
pause >nul
