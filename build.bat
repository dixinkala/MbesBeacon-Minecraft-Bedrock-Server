@echo off
REM ============================================
REM  MbesBeacon Build Script Launcher
REM  This batch file calls build.ps1 to avoid encoding issues
REM ============================================
cd /d "%~dp0"

echo ========================================
echo   MbesBeacon Build Script
echo ========================================
echo.
echo Launching PowerShell build script...
echo.

powershell -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo Build completed successfully.
exit /b 0
