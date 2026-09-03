@echo off
chcp 65001 >nul
echo ========================================
echo   MbesBeacon 自签名代码证书生成工具
echo ========================================
echo.

REM 检查是否以管理员身份运行
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请以管理员身份运行此脚本！
    echo 右键点击脚本，选择"以管理员身份运行"
    pause
    exit /b 1
)

set CERT_NAME=MbesBeacon
set CERT_FILE=%~dp0% CERT_NAME%.pfx
set CER_FILE=%~dp0% CERT_NAME%.cer

REM 从环境变量获取密码，未设置则提示用户输入
if "%CERT_PASS%"=="" (
    echo [提示] 未设置 CERT_PASS 环境变量，请输入证书密码
    set /p PASSWORD=请输入证书密码: 
) else (
    set PASSWORD=%CERT_PASS%
    echo [信息] 使用 CERT_PASS 环境变量中的密码
)

echo.
echo [1/4] 正在生成自签名代码签名证书...
powershell -Command "$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=MbesBeacon, O=MbesBeacon Project' -KeyAlgorithm RSA -KeyLength 2048 -NotAfter (Get-Date).AddYears(3) -CertStoreLocation 'Cert:\CurrentUser\My'; $pwd = ConvertTo-SecureString '%PASSWORD%' -AsPlainText -Force; Export-PfxCertificate -Cert $cert -FilePath '%CERT_FILE%' -Password $pwd; Export-Certificate -Cert $cert -FilePath '%CER_FILE%'"

if %errorlevel% neq 0 (
    echo [错误] 证书生成失败！
    pause
    exit /b 1
)

echo.
echo [2/4] 正在将证书安装到受信任的根证书颁发机构...
powershell -Command "Import-Certificate -FilePath '%CER_FILE%' -CertStoreLocation 'Cert:\CurrentUser\Root'"

if %errorlevel% neq 0 (
    echo [警告] 证书安装到受信任根失败，你可以手动安装 %CER_FILE%
) else (
    echo [成功] 证书已安装到受信任的根证书颁发机构
)

echo.
echo [3/4] 正在将证书安装到受信任的发布者...
powershell -Command "Import-Certificate -FilePath '%CER_FILE%' -CertStoreLocation 'Cert:\CurrentUser\TrustedPublisher'"

if %errorlevel% neq 0 (
    echo [警告] 证书安装到受信任发布者失败
) else (
    echo [成功] 证书已安装到受信任的发布者
)

echo.
echo [4/4] 清理临时证书文件...
del "%CER_FILE%" /f /q >nul 2>&1

echo.
echo ========================================
echo   证书生成完成！
echo ========================================
echo.
echo 证书文件: %CERT_FILE%
echo 证书密码: [已隐藏]
echo 有效期: 3 年
echo.
echo 注意:
echo 1. 自签名证书仅在本机受信任
echo 2. 分发到其他电脑时，用户需要手动安装证书
echo 3. 如需完全消除 SmartScreen 警告，请申请 SignPath 免费签名
echo.
echo 运行 sign_exe.bat 即可对 EXE 进行签名
echo.
pause
