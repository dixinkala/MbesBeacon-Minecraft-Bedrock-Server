# MbesBeacon Build Script
# Usage: .\build.ps1 [-SkipSign] [-NoClean]
# Description: Build MbesBeacon.exe with optional digital signature

param(
    [switch]$SkipSign,
    [switch]$NoClean
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "========================================"
Write-Host "  MbesBeacon Build Script"
Write-Host "========================================"
Write-Host ""

# Step 1: Check PyInstaller
Write-Host "[1/3] Checking PyInstaller..."
try {
    $pyinstallerVersion = python -m PyInstaller --version 2>&1
    Write-Host "  PyInstaller version: $pyinstallerVersion"
} catch {
    Write-Host "  PyInstaller not found, installing..."
    python -m pip install pyinstaller
}

# Step 2: Build EXE
Write-Host ""
Write-Host "[2/3] Building EXE..."
$cleanArg = if ($NoClean) { "" } else { "--clean" }
python -m PyInstaller --noconfirm $cleanArg BedrockServerManager.spec

$exePath = Join-Path $scriptDir "dist\MbesBeacon.exe"
if (-not (Test-Path $exePath)) {
    Write-Host ""
    Write-Host "[ERROR] Build failed, please check error messages above."
    exit 1
}

# Step 3: Digital Signature
Write-Host ""
Write-Host "[3/3] Digital Signature..."

$signed = $false

if (-not $SkipSign) {
    # Get certificate password
    $certPass = $env:CERT_PASS

    # Try MbesBeacon.pfx first
    $mbesCertPath = Join-Path $scriptDir "MbesBeacon.pfx"
    if (Test-Path $mbesCertPath) {
        Write-Host "  Found self-signed certificate: MbesBeacon.pfx"
        if (-not $certPass) {
            Write-Host "  [WARNING] CERT_PASS environment variable not set, skipping signature"
            Write-Host "  [TIP] Run: `$env:CERT_PASS = `"your_password`"; .\build.ps1"
        } else {
            Write-Host "  Signing EXE..."
            try {
                $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2
                $cert.Import($mbesCertPath, $certPass, [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable)
                $signature = Set-AuthenticodeSignature -FilePath $exePath -Certificate $cert -TimestampServer "http://timestamp.digicert.com" -HashAlgorithm SHA256
                if ($signature.Status -eq "Valid" -or $signature.Status -eq "UnknownError") {
                    Write-Host "  [SUCCESS] EXE signed (SHA256 + RFC3161 timestamp)"
                    $signed = $true
                } else {
                    Write-Host "  [ERROR] Signature failed: $($signature.StatusMessage)"
                }
            } catch {
                Write-Host "  [ERROR] Signature failed: $_"
            }
        }
    }

    # Try cert.pfx if MbesBeacon.pfx not found or signing failed
    if (-not $signed) {
        $certPath = Join-Path $scriptDir "cert.pfx"
        if (Test-Path $certPath) {
            Write-Host "  Found commercial certificate: cert.pfx"
            if (-not $certPass) {
                Write-Host "  [WARNING] CERT_PASS environment variable not set, skipping signature"
                Write-Host "  [TIP] Run: `$env:CERT_PASS = `"your_password`"; .\build.ps1"
            } else {
                Write-Host "  Signing EXE..."
                try {
                    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2
                    $cert.Import($certPath, $certPass, [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable)
                    $signature = Set-AuthenticodeSignature -FilePath $exePath -Certificate $cert -TimestampServer "http://timestamp.digicert.com" -HashAlgorithm SHA256
                    if ($signature.Status -eq "Valid" -or $signature.Status -eq "UnknownError") {
                        Write-Host "  [SUCCESS] EXE signed (SHA256 + RFC3161 timestamp)"
                        $signed = $true
                    } else {
                        Write-Host "  [ERROR] Signature failed: $($signature.StatusMessage)"
                    }
                } catch {
                    Write-Host "  [ERROR] Signature failed: $_"
                }
            }
        }
    }
}

if (-not $signed) {
    Write-Host "  [TIP] No certificate found, skipping digital signature"
    Write-Host "  [TIP] SmartScreen warning will appear on first run"
    Write-Host "  [TIP] To sign, run: .\create_cert.ps1 to generate self-signed certificate"
}

# Cleanup build directory
if (-not $NoClean) {
    $buildDir = Join-Path $scriptDir "build"
    if (Test-Path $buildDir) {
        Remove-Item $buildDir -Recurse -Force
        Write-Host ""
        Write-Host "  Build directory cleaned"
    }
}

# Summary
Write-Host ""
Write-Host "========================================"
Write-Host "  Build Complete!"
Write-Host "========================================"
Write-Host ""

$exeFile = Get-Item $exePath
Write-Host "  Output: $($exeFile.Name)"
Write-Host "  Size: $([math]::Round($exeFile.Length/1MB, 2)) MB"
Write-Host "  Version: $($exeFile.VersionInfo.FileVersion)"
Write-Host "  Product: $($exeFile.VersionInfo.ProductName)"
Write-Host "  Created: $($exeFile.LastWriteTime)"

if ($signed) {
    Write-Host "  Signature: Signed"
} else {
    Write-Host "  Signature: Not signed"
}

Write-Host ""
Write-Host "Usage:"
Write-Host "  Set password: `$env:CERT_PASS = `"your_password`""
Write-Host "  Build: .\build.ps1"
Write-Host "  Build without sign: .\build.ps1 -SkipSign"
Write-Host "  Build without clean: .\build.ps1 -NoClean"
Write-Host ""
