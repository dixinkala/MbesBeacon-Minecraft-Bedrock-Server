@echo off
chcp 65001 >nul
echo ========================================
echo   MbesBeacon EXE 数字签名工具
echo ========================================
echo.

set EXE_FILE=%~dp0dist\MbesBeacon.exe
set CERT_FILE=%~dp0MbesBeacon.pfx
set TIMESTAMP_URL=http://timestamp.digicert.com

REM 从环境变量获取密码，未设置则提示用户输入
if "%CERT_PASS%"=="" (
    echo [提示] 未设置 CERT_PASS 环境变量，请输入证书密码
    set /p PASSWORD=请输入证书密码: 
) else (
    set PASSWORD=%CERT_PASS%
    echo [信息] 使用 CERT_PASS 环境变量中的密码
)

REM 检查 EXE 文件是否存在
if not exist "%EXE_FILE%" (
    echo [错误] 未找到 EXE 文件: %EXE_FILE%
    echo 请先运行 build.bat 构建 EXE
    pause
    exit /b 1
)

REM 检查证书文件是否存在
if not exist "%CERT_FILE%" (
    echo [错误] 未找到证书文件: %CERT_FILE%
    echo 请先运行 create_cert.bat 生成证书
    pause
    exit /b 1
)

REM 查找 signtool.exe
set SIGNTOOL=
for /f "delims=" %%i in ('where signtool 2^>nul') do set SIGNTOOL=%%i

if "%SIGNTOOL%"=="" (
    REM 尝试在 Windows Kits 中查找
    for /d %%d in ("C:\Program Files (x86)\Windows Kits\10\bin\*") do (
        if exist "%%d\x64\signtool.exe" (
            set SIGNTOOL=%%d\x64\signtool.exe
            goto :found
        )
    )
    :found
)

if "%SIGNTOOL%"=="" (
    echo [错误] 未找到 signtool.exe
    echo 请安装 Windows SDK 或 Visual Studio
    pause
    exit /b 1
)

echo [信息] 使用 signtool: %SIGNTOOL%
echo [信息] 正在对 EXE 进行数字签名...
echo.

REM 使用 SHA256 算法签名，并添加时间戳
"%SIGNTOOL%" sign /f "%CERT_FILE%" /p %PASSWORD% /fd SHA256 /tr %TIMESTAMP_URL% /td SHA256 /v "%EXE_FILE%"

if %errorlevel% neq 0 (
    echo.
    echo [错误] 签名失败！
    pause
    exit /b 1
)

echo.
echo ========================================
echo   签名成功！
echo ========================================
echo.
echo 已签名文件: %EXE_FILE%
echo 签名算法: SHA256
echo 时间戳: %TIMESTAMP_URL%
echo.

REM 验证签名
echo [信息] 正在验证签名...
"%SIGNTOOL%" verify /pa /v "%EXE_FILE%"

echo.
pause
