@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    echo Install Python and enable the "Add Python to PATH" option.
    pause
    exit /b 1
)

if not exist "arcacon.py" (
    echo [ERROR] arcacon.py was not found.
    pause
    exit /b 1
)

if "%~1"=="" goto prompt
python arcacon.py %*
goto finish

:prompt
set "ARCA_URL="
set /p "ARCA_URL=Enter an Arcacon URL: "
if not defined ARCA_URL (
    echo [ERROR] No URL was entered.
    pause
    exit /b 1
)

set "WORKERS="
echo Available CPU threads: %NUMBER_OF_PROCESSORS%
set /p "WORKERS=Parallel workers (1-%NUMBER_OF_PROCESSORS%, blank=auto): "
if not defined WORKERS (
    python arcacon.py "%ARCA_URL%"
) else (
    python arcacon.py "%ARCA_URL%" --workers "%WORKERS%"
)

:finish
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo [ERROR] Process failed with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
