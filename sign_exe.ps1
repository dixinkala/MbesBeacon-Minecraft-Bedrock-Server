# MbesBeacon EXE Digital Signature Tool
# Usage: .\sign_exe.ps1 [-CertPath <path>] [-ExePath <path>] [-CertPass <password>]

param(
    [string]$CertPath = "",
    [string]$ExePath = "",
    [string]$CertPass = ""
)

$ErrorActionPreference = "Stop"

# Use script directory as base for relative paths
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $CertPath) {
    $CertPath = Join-Path $scriptDir "MbesBeacon.pfx"
}
if (-not $ExePath) {
    $ExePath = Join-Path $scriptDir "dist\MbesBeacon.exe"
}
$timestampUrl = "http://timestamp.digicert.com"

Write-Host "========================================"
Write-Host "  MbesBeacon EXE Digital Signature"
Write-Host "========================================"
Write-Host ""
Write-Host "EXE: $ExePath"
Write-Host "Cert: $CertPath"
Write-Host ""

# Check files
if (-not (Test-Path $ExePath)) {
    Write-Host "[ERROR] EXE not found: $ExePath"
    exit 1
}
if (-not (Test-Path $CertPath)) {
    Write-Host "[ERROR] Cert not found: $CertPath"
    exit 1
}

# Get certificate password from environment variable or parameter
if (-not $CertPass) {
    $CertPass = $env:CERT_PASS
}

# Try to import cert
Write-Host "Importing certificate..."
$cert = $null

# Try empty password first
try {
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2
    $cert.Import($CertPath, "", [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable)
    Write-Host "[OK] Certificate imported (empty password)"
} catch {
    # Try password from environment/parameter
    if ($CertPass) {
        try {
            $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2
            $cert.Import($CertPath, $CertPass, [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable)
            Write-Host "[OK] Certificate imported (password from CERT_PASS)"
        } catch {
            Write-Host "[ERROR] Password from CERT_PASS is incorrect"
        }
    }
}

if (-not $cert) {
    Write-Host ""
    Write-Host "[ERROR] Cannot import certificate."
    Write-Host "Please set CERT_PASS environment variable or use -CertPass parameter:"
    Write-Host "  `$env:CERT_PASS = `"your_password`""
    Write-Host "  .\sign_exe.ps1 -CertPass `"your_password`""
    exit 1
}

Write-Host ""
Write-Host "Certificate Info:"
Write-Host "  Subject: $($cert.Subject)"
Write-Host "  Issuer: $($cert.Issuer)"
Write-Host "  Valid: $($cert.NotBefore) - $($cert.NotAfter)"
Write-Host "  Thumbprint: $($cert.Thumbprint)"
Write-Host ""

# Sign EXE
Write-Host "Signing EXE..."
try {
    $signature = Set-AuthenticodeSignature -FilePath $ExePath -Certificate $cert -TimestampServer $timestampUrl -HashAlgorithm SHA256
    Write-Host "Signature status: $($signature.Status)"
    if ($signature.Status -eq "Valid") {
        Write-Host "[SUCCESS] Signature successful!"
    } else {
        Write-Host "Status message: $($signature.StatusMessage)"
    }
} catch {
    Write-Host "[ERROR] Signing failed: $_"
    exit 1
}

Write-Host ""
Write-Host "========================================"
Write-Host "  Verify Signature"
Write-Host "========================================"
try {
    $verify = Get-AuthenticodeSignature -FilePath $ExePath
    Write-Host "Status: $($verify.Status)"
    Write-Host "Signer: $($verify.SignerCertificate.Subject)"
    if ($verify.Status -eq "Valid") {
        Write-Host "[SUCCESS] Signature verified!"
    }
} catch {
    Write-Host "[ERROR] Verification failed: $_"
}

Write-Host ""
Write-Host "========================================"
Write-Host "  Done"
Write-Host "========================================"
Write-Host "Signed file: $ExePath"
Write-Host "Algorithm: SHA256"
Write-Host "Timestamp: $timestampUrl"
Write-Host ""
