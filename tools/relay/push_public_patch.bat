@echo off
setlocal enabledelayedexpansion
rem Run this on the SOURCE machine (this one) to publish clean code to the
rem public relay repo. The final "git push" step must be run BY A HUMAN --
rem Claude Code is blocked from pushing to a public destination on purpose.
rem
rem Public relay repo: https://github.com/alienid4/wbs2
rem This script only ships code (app/, tests/, top-level files). It never
rem ships config.json (machine-specific) or data/ (real records).

set "SRC=C:\AiProject\CL_WBS"
set "RELAY=%USERPROFILE%\Desktop\CL_WBS_public_relay"
set "REPO_URL=https://github.com/alienid4/wbs2.git"

if not exist "%RELAY%" (
  echo Cloning relay repo for the first time...
  git clone "%REPO_URL%" "%RELAY%"
  if errorlevel 1 goto fail
)

echo Mirroring clean source into relay working copy...
robocopy "%SRC%\app" "%RELAY%\app" /MIR /NFL /NDL /NJH /NJS
robocopy "%SRC%\tests" "%RELAY%\tests" /MIR /NFL /NDL /NJH /NJS
robocopy "%SRC%\tools" "%RELAY%\tools" /MIR /NFL /NDL /NJH /NJS /XD __pycache__
copy /Y "%SRC%\README.md" "%RELAY%\README.md" >nul
copy /Y "%SRC%\start.bat" "%RELAY%\start.bat" >nul
copy /Y "%SRC%\version.json" "%RELAY%\version.json" >nul
copy /Y "%SRC%\.gitignore" "%RELAY%\.gitignore" >nul

cd /d "%RELAY%"
git add -A
git status --short
echo.
echo Review the changes above.
echo.
set /p CONFIRM="Type YES to commit and push to the PUBLIC repo: "
if /i not "%CONFIRM%"=="YES" (
  echo Cancelled. Nothing was pushed.
  goto end
)

git commit -m "patch"
git push
if errorlevel 1 goto fail

echo.
echo Pushed. Company laptop can now run update.bat to pick this up.
goto end

:fail
echo.
echo Something failed above. Nothing was pushed if git push did not run.
exit /b 1

:end
pause
