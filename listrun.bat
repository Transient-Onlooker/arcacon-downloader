@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not exist "list.txt" (
    echo [ERROR] list.txt was not found in this folder.
    echo Put one Arcacon URL on each line, then run this file again.
    pause
    exit /b 1
)

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

set /a TOTAL=0
set /a SUCCEEDED=0
set /a FAILED=0

for /f "usebackq delims= eol=#" %%L in ("list.txt") do (
    set /a TOTAL+=1
    echo.
    echo [!TOTAL!] Downloading: %%L
    python arcacon.py "%%L"
    if errorlevel 1 (
        set /a FAILED+=1
        echo [!TOTAL!] FAILED. Continuing with the next URL.
    ) else (
        set /a SUCCEEDED+=1
        echo [!TOTAL!] Done.
    )
)

echo.
echo Finished. Success: !SUCCEEDED!  Failed: !FAILED!  Total: !TOTAL!
pause
exit /b !FAILED!
