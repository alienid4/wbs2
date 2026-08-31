@echo off
setlocal enabledelayedexpansion
rem Run this on the COMPANY LAPTOP to pull the latest code from the public
rem relay repo. No git needed here -- this downloads a zip over plain HTTPS,
rem which corporate firewalls usually allow (same path as browsing).
rem
rem This only replaces app/, tests/, and a few top-level files. It never
rem touches config.json or data/ -- your local settings and real records
rem are safe.

set "HERE=%~dp0"
set "APPROOT=%HERE%..\.."
set "REPO_ZIP_URL=https://github.com/alienid4/wbs2/archive/refs/heads/main.zip"
set "TMPZIP=%TEMP%\wbs2_update.zip"
set "TMPDIR=%TEMP%\wbs2_update_extract"

echo Downloading latest code...
if exist "%TMPZIP%" del /q "%TMPZIP%"
curl --ssl-no-revoke -L -o "%TMPZIP%" "%REPO_ZIP_URL%"
if errorlevel 1 goto fail

if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
mkdir "%TMPDIR%"
powershell -NoProfile -Command "Expand-Archive -Path '%TMPZIP%' -DestinationPath '%TMPDIR%' -Force"
if errorlevel 1 goto fail

set "SRCDIR=%TMPDIR%\wbs2-main"
if not exist "%SRCDIR%" goto fail

echo Applying update (config.json and data\ are left untouched)...
robocopy "%SRCDIR%\app" "%APPROOT%\app" /MIR /NFL /NDL /NJH /NJS
robocopy "%SRCDIR%\tests" "%APPROOT%\tests" /MIR /NFL /NDL /NJH /NJS
robocopy "%SRCDIR%\tools" "%APPROOT%\tools" /MIR /NFL /NDL /NJH /NJS /XD __pycache__
copy /Y "%SRCDIR%\start.bat" "%APPROOT%\start.bat" >nul
copy /Y "%SRCDIR%\version.json" "%APPROOT%\version.json" >nul

echo.
echo Update done. Restart start.bat to run the new version.
goto end

:fail
echo.
echo Update failed. Check your network connection and try again.
echo If curl keeps failing, check whether this machine's proxy needs
echo an extra flag -- tell Claude the exact error text.
exit /b 1

:end
pause
