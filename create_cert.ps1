# MbesBeacon self-signed certificate generation script
$ErrorActionPreference = "Stop"

Write-Host "Generating self-signed code signing certificate..."

# Get certificate password from environment variable or prompt user
if ($env:CERT_PASS) {
    $password = ConvertTo-SecureString $env:CERT_PASS -AsPlainText -Force
    Write-Host "Using password from CERT_PASS environment variable"
} else {
    $securePass = Read-Host "Please enter certificate password" -AsSecureString
    $password = $securePass
}

# Generate certificate
$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject "CN=MbesBeacon, O=MbesBeacon Project" `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -NotAfter (Get-Date).AddYears(3) `
    -CertStoreLocation "Cert:\CurrentUser\My"

# Export as PFX
Export-PfxCertificate -Cert $cert -FilePath "MbesBeacon.pfx" -Password $password | Out-Null

Write-Host "Certificate generated: MbesBeacon.pfx"
Write-Host "  Subject: $($cert.Subject)"
Write-Host "  Valid until: $($cert.NotAfter)"
Write-Host "  Password: [hidden]"

# Export CER file for installation
Export-Certificate -Cert $cert -FilePath "MbesBeacon.cer" | Out-Null
Write-Host "Public key exported: MbesBeacon.cer"

# Try to install to Trusted Root Certification Authorities
try {
    Import-Certificate -FilePath "MbesBeacon.cer" -CertStoreLocation "Cert:\CurrentUser\Root" | Out-Null
    Write-Host "Certificate installed to Trusted Root Certification Authorities"
} catch {
    Write-Host "Warning: Failed to install to Trusted Root (admin rights required), you can manually install MbesBeacon.cer"
}

# Try to install to Trusted Publishers
try {
    Import-Certificate -FilePath "MbesBeacon.cer" -CertStoreLocation "Cert:\CurrentUser\TrustedPublisher" | Out-Null
    Write-Host "Certificate installed to Trusted Publishers"
} catch {
    Write-Host "Warning: Failed to install to Trusted Publishers"
}

# Clean up temporary CER file
Remove-Item "MbesBeacon.cer" -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "========================================"
Write-Host "  Certificate generation complete!"
Write-Host "========================================"
Write-Host ""
Write-Host "Certificate file: MbesBeacon.pfx"
Write-Host "Password: [hidden] (set via CERT_PASS environment variable or entered interactively)"
Write-Host "Valid for: 3 years"
Write-Host ""
Write-Host "Run build.bat or sign_exe.bat to sign the EXE"
