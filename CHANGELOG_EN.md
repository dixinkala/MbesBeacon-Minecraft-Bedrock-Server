# Changelog

**Languages: [中文](CHANGELOG.md) | English**

This project follows [Semantic Versioning](https://semver.org/). Version numbers do **not** follow Minecraft Java Edition style naming rules.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.23] - 2026-09

### ✨ Added

- Fixed software update dialog: the modal now actually appears when an update is detected (previously the dialog was created but invisible)
- Release notes text is now HTML-escaped to prevent malformed modal content
- Added English versions of project docs (README/SECURITY/CONTRIBUTING/CODE_OF_CONDUCT/PR & Issue templates)
- Added UI preview screenshot and CI/CodeQL/test-count badges at the top of README
- Extended repository Topics to 14 keywords (added `bedrock-dedicated-server`, `game-server`)

### 🔧 Changed

- EXE package name follows "app name + version" convention (`MbesBeacon1.0.23.exe`); version metadata synchronized
- Removed language-switching/i18n related code; UI uses the built-in Chinese interface

### 🐛 Fixed

- Theme saving crash (`DEFAULT_THEME` undefined)
- Backup deletion endpoint path validation bypass
- IP ban endpoint missing IP format validation
- Health check endpoint crash due to missing `psutil` dependency
- `/api/config` GET endpoint sending duplicate HTTP responses
- Performance monitor player count always 0, CPU hardcoded to 0
- Unbounded crash auto-restart loop
- Download integrity verification not blocking installation; SHA256 verification ineffective
- Backup restore/delete missing path validation
- `ignore_ssl` option settable by any API request
- Single-instance mutex residue issue
- Download hash local cache permanently broken (`register_known_hash` duplicate definition override)

### 🏗️ Architecture & Performance

- Fully adopted `AppContext` dependency injection; eliminated module-level globals
- Split giant `do_GET`/`do_POST` methods into route-table registration pattern
- Split single-file into multi-module package (config/console/utils/players/backup/server/install/web)
- Added API rate limiting, health check endpoint
- Added audit log, second confirmation for dangerous operations
- Added config numeric range validation, custom download URL security validation
- Added crash log environment info
- Fixed `extract_zip` path traversal detection flaw
- Cached `detect_servers()` results; eliminated high-frequency disk scans

---

## [1.0.22] - 2026-09

### ✨ Added

- Automatic/manual software update check (distinct from server version updates)
- Language follows system locale (detected once at first launch)
- Language switch button, world import/export
- Security policy (SECURITY.md), English docs

### 🔧 Changed

- Removed language-switching/i18n code (later version)
- Version number reverted to 1.0.x naming (not MC Java style)

### 🐛 Fixed

- UI interaction anomalies, empty tray context menu
- Stuck on loading screen at startup
- Online version list showing only one version
- Directory still displayed after deleting server
- Update check not detecting current installed version
- Exit button not closing the app properly

---

## [1.0.21] - 2026-09

### ✨ Added

- First GitHub Release (v1.0.21)
- README completion (description, main features, install, usage, input/output examples)
- System tray support, multiple theme styles
- Scheduled tasks module (restart/backup/announcement)
- Server performance monitoring panel
- Download integrity verification, dangerous-operation confirmation
- Multi-world management UI, auto-start toggle, command auto-completion

### 🏗️ Architecture & Performance

- Split single-file into multi-module package structure
- Added API rate limiting
- Added config numeric range validation

---

## [1.0.20] and earlier

Early iterations featuring: automated server installation (official + multiple download sources), player management (permissions/kick/ban/allow-list), multi-server detection & management, world backup, visual config editing with rollback, SSE live console, theme system, update check, and other core functionality.

[Unreleased]: https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/compare/v1.0.23...main
[1.0.23]: https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/releases/tag/v1.0.23
[1.0.22]: https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/releases/tag/v1.0.22
[1.0.21]: https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/releases/tag/v1.0.21
