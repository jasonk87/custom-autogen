@echo off
setlocal enabledelayedexpansion

echo Checking for the latest updates from GitHub...
git fetch --all --quiet

:: Find the branch with the most recent commit on origin
for /f "tokens=*" %%i in ('git for-each-ref --sort=-committerdate refs/remotes/origin --format="%%(refname:short)"') do (
    set "LATEST_REMOTE=%%i"
    goto :found
)

:found
:: Extract the local branch name (e.g., 'main' from 'origin/main')
set "LOCAL_BRANCH=%LATEST_REMOTE:origin/=%"

echo.
echo Latest remote branch detected: %LATEST_REMOTE%
set /p "CONFIRM=Sync local '%LOCAL_BRANCH%' to this branch? (Keys/Un-tracked files will be kept) (y/n): "

if /i "%CONFIRM%" neq "y" (
    echo Update cancelled.
    pause
    exit /b
)

echo.
echo Switching to %LOCAL_BRANCH% and pulling changes...

:: Switch to the branch, forcing the move but NOT cleaning untracked files
git checkout -f %LOCAL_BRANCH%

:: Reset tracked files to match origin exactly
git reset --hard %LATEST_REMOTE%

echo.
echo Success! Tracked files are updated to %LATEST_REMOTE%.
echo Your local API keys and untracked files were preserved.
pause