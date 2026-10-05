# Security Policy

Thank you for your interest in the security of MbesBeacon. We take security seriously and encourage responsible security research.

**Languages: [中文](SECURITY.md) | English**

## Supported Versions

We only support the **latest release**. Please always use the latest version to receive security fixes and feature improvements.

| Version | Supported |
| --- | --- |
| Latest (v1.0.23+) | ✅ Supported |
| Older versions | ❌ Not supported, please upgrade |

## Reporting a Vulnerability

**Please do NOT disclose security vulnerabilities in public Issues.**

Use GitHub's **private vulnerability reporting** feature instead:

1. Open the **Security** tab of the repository → **Report a vulnerability**
2. Fill in the vulnerability description, including:
   - Affected versions
   - Vulnerability type and severity
   - Reproduction steps (or a minimal reproduction)
   - Potential impact
3. After submission, maintainers are notified and will work with you privately

We will confirm receipt within **48 hours** and do our best to provide a fix plan or temporary mitigation within **7 days**.

## Security Recommendations

- Always download MbesBeacon.exe from the official [GitHub Releases](https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/releases) page; never use unverified third-party distributions
- The software only listens on the local loopback address (127.0.0.1); do not expose it to the public internet
- Do not enable "Ignore SSL verification" on untrusted networks
- Update the software and server versions regularly to receive security fixes

## Scope

The code and packaged artifacts of this project (MbesBeacon) itself. The following are **out of scope**:

- Vulnerabilities in the official Minecraft Bedrock dedicated server (bedrock_server) itself — report to the [Mojang / Microsoft Security Response Center](https://www.microsoft.com/en-us/msrc)
- Vulnerabilities in third-party dependencies — report to the respective projects

## Acknowledgements

We thank all contributors who report vulnerabilities privately and help make this project safer.
