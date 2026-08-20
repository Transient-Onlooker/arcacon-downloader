@echo off
setlocal

set "PROFILE=%LOCALAPPDATA%\arcacon-downloader\edge-profile"
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"

if not exist "%EDGE%" (
    echo [ERROR] Microsoft Edge was not found.
    pause
    exit /b 1
)

echo Opening the dedicated Arcacon Edge profile...
echo Log in and complete the Cloudflare check in the browser window.
echo Close this dedicated Edge window completely before running run.bat.
start "Arcacon Edge Profile" "%EDGE%" --user-data-dir="%PROFILE%" https://arca.live
