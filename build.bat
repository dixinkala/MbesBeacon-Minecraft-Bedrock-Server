@echo off
REM ============================================
REM  MbesBeacon - Minecraft 基岩版服务器管理工具
REM  构建脚本：打包 EXE + 可选数字签名
REM  用法：双击运行，或在命令行执行 build.bat
REM  产物：dist\MbesBeacon.exe
REM ============================================
cd /d "%~dp0"

echo ========================================
echo   MbesBeacon 构建脚本
echo ========================================
echo.

echo [1/3] 正在检查 PyInstaller ...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo 未检测到 PyInstaller，正在安装 ...
    python -m pip install pyinstaller
)

echo.
echo [2/3] 正在打包 EXE ...
python -m PyInstaller --noconfirm --clean BedrockServerManager.spec

if not exist "dist\MbesBeacon.exe" (
    echo.
    echo [错误] 打包失败，请检查上方错误信息。
    pause
    exit /b 1
)

echo.
echo [3/3] 数字签名 ...
REM ============================================
REM  数字签名（可选）
REM  支持两种证书：
REM  1. MbesBeacon.pfx（自签名证书，由 create_cert.bat 生成）
REM  2. cert.pfx（商业证书，密码通过 CERT_PASS 环境变量传入）
REM ============================================

set SIGNED=0

REM 优先使用自签名证书 MbesBeacon.pfx
if exist "%~dp0MbesBeacon.pfx" (
    echo 检测到自签名证书 MbesBeacon.pfx，正在签名 ...
    if "%CERT_PASS%"=="" (
        echo [警告] 未设置 CERT_PASS 环境变量，跳过签名
        echo [提示] 执行: set CERT_PASS=你的密码 ^&^& build.bat
    ) else (
        call :sign_exe "%~dp0MbesBeacon.pfx" "%CERT_PASS%"
        if %errorlevel% equ 0 set SIGNED=1
    )
)

REM 如果自签名证书不存在，尝试使用商业证书 cert.pfx
if %SIGNED% equ 0 (
    if exist "%~dp0cert.pfx" (
        if "%CERT_PASS%"=="" (
            echo [警告] 检测到 cert.pfx 但未设置 CERT_PASS 环境变量，跳过签名
            echo [提示] 执行: set CERT_PASS=你的密码 ^&^& build.bat
        ) else (
            echo 检测到商业证书 cert.pfx，正在签名 ...
            call :sign_exe "%~dp0cert.pfx" "%CERT_PASS%"
            if %errorlevel% equ 0 set SIGNED=1
        )
    )
)

if %SIGNED% equ 0 (
    echo [提示] 未找到证书文件，跳过数字签名
    echo [提示] 首次运行 EXE 时会有 SmartScreen 提示
    echo [提示] 如需签名，请运行 create_cert.bat 生成自签名证书
)

echo.
echo ========================================
echo   构建完成！
echo ========================================
echo.
echo 产物文件: dist\MbesBeacon.exe
if %SIGNED% equ 1 (
    echo 签名状态: 已签名
) else (
    echo 签名状态: 未签名
)
echo.

pause
exit /b 0

REM ============================================
REM  签名函数
REM  参数: %1 = 证书文件路径, %2 = 证书密码
REM ============================================
:sign_exe
setlocal
set CERT_PATH=%~1
set CERT_PASS=%~2
set EXE_PATH=%~dp0dist\MbesBeacon.exe
set TIMESTAMP_URL=http://timestamp.digicert.com

REM 查找 signtool.exe
set SIGNTOOL=
for /f "delims=" %%i in ('where signtool 2^>nul') do set SIGNTOOL=%%i

if "%SIGNTOOL%"=="" (
    for /d %%d in ("C:\Program Files (x86)\Windows Kits\10\bin\*") do (
        if exist "%%d\x64\signtool.exe" (
            set SIGNTOOL=%%d\x64\signtool.exe
            goto :signtool_found
        )
    )
    :signtool_found
)

if "%SIGNTOOL%"=="" (
    echo [错误] 未找到 signtool.exe，请安装 Windows SDK 或 Visual Studio
    endlocal
    exit /b 1
)

"%SIGNTOOL%" sign /f "%CERT_PATH%" /p %CERT_PASS% /fd SHA256 /tr %TIMESTAMP_URL% /td SHA256 "%EXE_PATH%" >nul 2>&1

if %errorlevel% equ 0 (
    echo [成功] EXE 已数字签名（SHA256 + RFC3161 时间戳）
    endlocal
    exit /b 0
) else (
    echo [错误] 签名失败
    endlocal
    exit /b 1
)
