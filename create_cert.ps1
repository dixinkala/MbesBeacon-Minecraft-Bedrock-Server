# MbesBeacon Self-Signed Certificate Generator
# Usage: .\create_cert.ps1 [-CertPass <password>] [-ValidYears <years>]

param(
    [string]$CertPass = "",
    [int]$ValidYears = 10
)

$ErrorActionPreference = "Stop"

Write-Host "========================================"
Write-Host "  MbesBeacon Certificate Generator"
Write-Host "========================================"
Write-Host ""

# Get certificate password
if (-not $CertPass) {
    $CertPass = $env:CERT_PASS
}

if (-not $CertPass) {
    Write-Host "Please enter certificate password (at least 6 characters):"
    $securePass = Read-Host "Password" -AsSecureString
    $CertPass = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePass))
}

if ($CertPass.Length -lt 6) {
    Write-Host "[ERROR] Password must be at least 6 characters"
    exit 1
}

Write-Host "Certificate Info:"
Write-Host "  Subject: CN=MbesBeacon, O=MbesBeacon Project, C=CN"
Write-Host "  Type: CodeSigningCert"
Write-Host "  Key Algorithm: RSA"
Write-Host "  Key Length: 2048"
Write-Host "  Valid Years: $ValidYears"
Write-Host "  Password: [hidden] (length: $($CertPass.Length))"
Write-Host ""

# Generate self-signed certificate
Write-Host "Generating self-signed certificate..."
try {
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject "CN=MbesBeacon, O=MbesBeacon Project, C=CN" `
        -KeyAlgorithm RSA `
        -KeyLength 2048 `
        -NotAfter (Get-Date).AddYears($ValidYears) `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -FriendlyName "MbesBeacon Code Signing Certificate"

    Write-Host "[SUCCESS] Certificate generated"
    Write-Host "  Thumbprint: $($cert.Thumbprint)"
    Write-Host "  Subject: $($cert.Subject)"
    Write-Host "  Issuer: $($cert.Issuer)"
    Write-Host "  Valid: $($cert.NotBefore) - $($cert.NotAfter)"
} catch {
    Write-Host "[ERROR] Certificate generation failed: $_"
    exit 1
}

# Export to PFX file
Write-Host ""
Write-Host "Exporting certificate to PFX file..."
$certPath = Join-Path (Get-Location) "MbesBeacon.pfx"
$securePass = ConvertTo-SecureString $CertPass -AsPlainText -Force

try {
    Export-PfxCertificate -Cert $cert -FilePath $certPath -Password $securePass | Out-Null
    Write-Host "[SUCCESS] Certificate exported to: $certPath"
} catch {
    Write-Host "[ERROR] Certificate export failed: $_"
    exit 1
}

# Export public key certificate (CER file)
$cerPath = Join-Path (Get-Location) "MbesBeacon.cer"
try {
    Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
    Write-Host "[SUCCESS] Public key certificate exported to: $cerPath"
} catch {
    Write-Host "[WARNING] Public key certificate export failed: $_"
}

# Remove temporary certificate from certificate store
Write-Host ""
Write-Host "Cleaning up temporary certificate from store..."
try {
    Remove-Item -Path "Cert:\CurrentUser\My\$($cert.Thumbprint)" -Force
    Write-Host "[SUCCESS] Temporary certificate removed from store"
} catch {
    Write-Host "[WARNING] Temporary certificate removal failed: $_"
}

# Verify certificate file
Write-Host ""
Write-Host "========================================"
Write-Host "  Certificate Generation Complete!"
Write-Host "========================================"
Write-Host ""
Write-Host "Certificate Files:"
if (Test-Path $certPath) {
    $certFile = Get-Item $certPath
    Write-Host "  PFX File: $($certFile.Name)"
    Write-Host "  Size: $($certFile.Length) bytes"
    Write-Host "  Created: $($certFile.CreationTime)"
}
Write-Host ""
Write-Host "Usage:"
Write-Host "  1. Set environment variable: `$env:CERT_PASS = `"$CertPass`""
Write-Host "  2. Run build script: .\build.bat"
Write-Host "  3. Or use sign tool: .\sign_exe.ps1 -CertPass `"$CertPass`""
Write-Host ""
Write-Host "Notes:"
Write-Host "  - This is a self-signed certificate, SmartScreen will still show warning"
Write-Host "  - Users need to manually trust this certificate to remove SmartScreen warning"
Write-Host "  - For wide distribution, consider purchasing a trusted code signing certificate"
Write-Host "  - Keep the certificate password secure, do not share with others"
Write-Host ""
