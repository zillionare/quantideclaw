@echo off
REM ============================================================================
REM build-wsl2.bat - Build OpenClaw Debian environment using WSL2 on Windows
REM
REM This script:
REM 1. Installs/updates WSL2 with Debian distribution
REM 2. Configures Debian environment
REM 3. Copies guest/ and runs the single provision entrypoint
REM 4. Exports as WSL2 tarball for distribution
REM
REM Prerequisites:
REM - Windows 10/11 with WSL2 support
REM - Administrator privileges (first time only)
REM
REM Usage:
REM   scripts\build-wsl2.bat
REM ============================================================================
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set ROOT_DIR=%SCRIPT_DIR%..
set GUEST_DIR=%ROOT_DIR%\guest
set ASSETS_DIR=%ROOT_DIR%\assets

REM Configuration
set WSL_DISTRO=Debian
set WSL_INSTANCE_NAME=openclaw-debian-13
set BUILD_USER=quantide
set BUILD_USER_PASSWORD=quantide
set WSL_IMPORT_DIR=%USERPROFILE%\openclaw-wsl\%WSL_INSTANCE_NAME%
set WSL_BASE_EXPORT=%TEMP%\debian-openclaw-base.tar

echo ==========================================
echo OpenClaw Debian Builder (WSL2/Windows)
echo ==========================================
echo.

REM Check if running as administrator
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Not running as administrator. Some features may not work.
    echo [WARN] Please run as administrator for best results.
    echo.
)

REM Check WSL installation
echo [INFO] Checking WSL installation...
wsl --status >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] WSL not detected. Installing WSL...
    wsl --install
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install WSL. Please install manually:
        echo [ERROR]   1. Run: wsl --install
        echo [ERROR]   2. Reboot when prompted
        echo [ERROR]   3. Run this script again
        pause
        exit /b 1
    )
    echo [INFO] WSL installed. Please reboot and run this script again.
    pause
    exit /b 0
)

REM Install Debian distribution if not present
echo [INFO] Checking for Debian WSL distribution...
wsl --list --quiet | findstr /i "debian" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing Debian WSL distribution...
    wsl --install -d Debian
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install Debian. Try manually:
        echo [ERROR]   wsl --install -d Debian
        pause
        exit /b 1
    )
    echo [INFO] Debian installed. Please complete initial setup in the WSL window.
    echo [INFO] Then press any key to continue...
    pause
)

echo [INFO] Verifying Debian can start...
wsl -d %WSL_DISTRO% -u root -- sh -lc "echo ready" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Debian has not completed first-run initialization yet.
    echo [ERROR] Please launch `wsl -d %WSL_DISTRO%` once, finish the account setup, then rerun this script.
    pause
    exit /b 1
)

REM Create a dedicated instance for OpenClaw
echo [INFO] Setting up Openclaw WSL instance...
if not exist "%WSL_IMPORT_DIR%" mkdir "%WSL_IMPORT_DIR%"

wsl --list --quiet | findstr /i "^%WSL_INSTANCE_NAME%$" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Exporting base Debian distribution...
    if exist "%WSL_BASE_EXPORT%" del /f /q "%WSL_BASE_EXPORT%"
    wsl --export %WSL_DISTRO% "%WSL_BASE_EXPORT%"
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to export base Debian distribution.
        pause
        exit /b 1
    )

    echo [INFO] Importing dedicated OpenClaw instance...
    wsl --import %WSL_INSTANCE_NAME% "%WSL_IMPORT_DIR%" "%WSL_BASE_EXPORT%" --version 2
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to import %WSL_INSTANCE_NAME%.
        pause
        exit /b 1
    )
)

for /f %%I in ('powershell -NoProfile -Command "$p = (Resolve-Path '%ROOT_DIR%').Path; $drive = $p.Substring(0,1).ToLower(); $rest = $p.Substring(2).Replace('\\','/'); Write-Output ('/mnt/' + $drive + $rest)"') do set ROOT_DIR_WSL=%%I

echo [INFO] Staging workspace resources into WSL...
wsl -d %WSL_INSTANCE_NAME% -u root -- bash -lc "rm -rf /root/openclaw-image && mkdir -p /root/openclaw-image && cp -r '%ROOT_DIR_WSL%/guest' /root/openclaw-image/guest"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to stage workspace resources into WSL.
    pause
    exit /b 1
)

REM Run provision script
echo [INFO] Running provision-manual.sh...
wsl -d %WSL_INSTANCE_NAME% -u root -- bash -lc "TARGET_ARCH=amd64 TARGET_PLATFORM=wsl2 BUILD_USER=%BUILD_USER% BUILD_USER_PASSWORD=%BUILD_USER_PASSWORD% bash /root/openclaw-image/guest/scripts/provision-manual.sh"

if %errorlevel% neq 0 (
    echo [ERROR] provision-manual.sh failed!
    pause
    exit /b 1
)

echo [OK] provision-manual.sh completed

REM Export the WSL instance
echo.
echo [INFO] Exporting WSL instance...
set OUTPUT_DIR=%ROOT_DIR%\output
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

wsl --export %WSL_INSTANCE_NAME% "%OUTPUT_DIR%\%WSL_INSTANCE_NAME%.tar"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to export WSL instance
    pause
    exit /b 1
)

REM Compress for distribution
echo [INFO] Compressing export...
powershell -Command "Compress-Archive -Path '%OUTPUT_DIR%\%WSL_INSTANCE_NAME%.tar' -DestinationPath '%OUTPUT_DIR%\%WSL_INSTANCE_NAME%.zip' -Force"

echo.
echo ==========================================
echo Build completed successfully!
echo ==========================================
echo.
echo Output: %OUTPUT_DIR%\%WSL_INSTANCE_NAME%.zip
echo.
echo To import on another machine:
echo   wsl --import OpenClaw ^<path^> ^<path^>\%WSL_INSTANCE_NAME%.tar
echo.
pause
