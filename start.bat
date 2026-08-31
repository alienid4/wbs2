@echo off
setlocal
cd /d "%~dp0"
title WBS Tracker
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem ---- locate python (py launcher first, then PATH) ----
set "PY="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto checkver

python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto checkver

goto nopython

:checkver
rem ---- found an interpreter, but is it new enough? (needs 3.8+) ----
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 goto oldpython
goto run

:run
echo Using %PY%
echo Starting server... your browser will open in a moment.
echo Close this window (or press Ctrl+C) to stop.
echo.
%PY% -m app.server
echo.
echo [ server stopped ]
pause
exit /b 0

:nopython
echo.
echo  [X] Python not found on this machine.
echo.
echo  This tool needs Python 3.8 or newer. Nothing else -- no extra
echo  packages, no internet access while running.
echo.
echo  If you can install software yourself:
echo    1. Download Python 3.8+:  https://www.python.org/downloads/
echo    2. IMPORTANT - tick "Add python.exe to PATH" during setup.
echo    3. Close this window, open a NEW one, and run start.bat again.
echo.
echo  If this is a work computer and you need IT/admin approval first,
echo  ask them to install "Python 3.8 or newer, 64-bit, standard
echo  python.org installer, with 'Add to PATH' checked". No admin
echo  rights are strictly required -- the python.org installer also
echo  offers a "Install for me only" (per-user) option that does not
echo  need an administrator password, if your policy allows that.
echo.
echo  If Python IS installed but this still shows, open cmd here and
echo  run:  py -3 --version    then tell Claude what it prints.
echo.
pause
exit /b 1

:oldpython
echo.
echo  [X] Found a Python, but it is older than 3.8 -- too old for this tool.
echo.
%PY% --version
echo.
echo  Install Python 3.8 or newer alongside it (the "py -3" launcher and
echo  python.org's installer both let multiple versions coexist, this
echo  will not remove what you already have):
echo    https://www.python.org/downloads/
echo.
pause
exit /b 1
