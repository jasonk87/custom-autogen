@echo off
setlocal EnableDelayedExpansion

:: ---------------------------------------------------------
:: AUTO-SWITCH TO LATEST BRANCH (FORCE OVERWRITE MODE)
:: ---------------------------------------------------------

echo Checking for Git repository...
git rev-parse --is-inside-work-tree >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Not a git repository.
    pause
    exit /b
)

echo.
echo Fetching latest info from remote...
git fetch --all --prune 

:: --- FIND THE NEWEST BRANCH ---
for /f "delims=" %%i in ('git for-each-ref --sort=-committerdate refs/remotes/origin/ --format="%%(refname:short)" --count=1') do set "LATEST_REF=%%i"

:: Strip "origin/" to get the clean branch name
set "LATEST_BRANCH=!LATEST_REF:origin/=!"

echo.
echo ========================================================
echo  MOST RECENT BRANCH FOUND: !LATEST_BRANCH!
echo ========================================================
echo.

:: --- CONFIRMATION STEP ---
echo  [WARNING] You are about to FORCE update to this branch.
echo  Any local changes (like your API keys in config.py or config.json)
echo  WILL BE LOST and overwritten by the remote version.
echo.
set /p "CHOICE=Are you sure you want to overwrite local files? (Y/N): "

if /i not "!CHOICE!"=="Y" (
    echo.
    echo  [CANCELLED] No changes were made.
    pause
    exit /b
)

:: --- THE FORCE OVERWRITE LOGIC ---
echo.
echo  Forcing checkout of !LATEST_BRANCH!...
git checkout -f !LATEST_BRANCH!

if !errorlevel! equ 0 (
    echo  Resetting local files to match remote exactly...
    git reset --hard origin/!LATEST_BRANCH!
    
    echo.
    echo  [SUCCESS] Updated to !LATEST_BRANCH! - Local changes overwritten.
) else (
    echo.
    echo  [ERROR] Could not checkout the branch.
)

echo.
pause