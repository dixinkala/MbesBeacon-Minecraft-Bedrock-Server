# 01 Install & update

[← Back to contents](English.md)

## Requirements

| Item | Requirement |
|---|---|
| OS | Windows 10/11; Ubuntu 20.04+ |
| Disk space | The install check requires ≥ **1200 MB** (the staging directory and the unpacked server exist at the same time, so the peak is roughly twice the final size; 2 GB recommended) |
| Network | Access to `minecraft.net` (official CDN) or a custom download URL; the version list uses GitHub |
| Permissions | A normal user is enough; no administrator rights (except when installing into a protected directory) |

## First run: three-step wizard

1. **Choose an install folder**: type a path or click "📁 Browse" and pick an empty folder (e.g. `D:\MinecraftServer`)
2. **Choose a version**: click ▼ next to the version box, or type a version (e.g. `1.21.0.03`)
3. **Start installing**: the server starts automatically afterwards if the checkbox is ticked

You can skip the wizard and work manually on the "① Install Server" tab.

## Existing servers: automatic detection

On startup the app scans common locations (Desktop, Documents, Downloads, C/D/E drives) for installed servers:

- One found → managed automatically, no dialog
- Several found → a dialog asks which one to use
- None found → a guide banner is shown; use "📁 Browse" to pick a folder manually

Manually selected folders are remembered and can be switched from the "Current server" dropdown next time.

## Choosing a version

| Source | Description |
|---|---|
| Version list | Fetched from the self-hosted manifest [`dixinkala/mbesbeacon-bds-versions`](https://github.com/dixinkala/mbesbeacon-bds-versions) (updated hourly by GitHub Actions); falls back to the community list, then to the built-in snapshot |
| "Detect latest" | Reads the current stable version from the official Mojang API |
| Manual input | Any version string; the download URL is built with the official CDN rule |
| Custom URL | Paste a third-party/mirror zip link; leave empty to use the official CDN |

**Version types**:

- **Stable** (default): released versions
- **Preview**: contains test features and may be unstable; the advanced option "include stable" lists both

## Download and install flow

```
download → local verification → extract to staging → atomically merge file by file into the server folder
```

Key points:

- Extraction happens in `.install_staging` inside the server folder, so **a failed merge never damages the existing server**
- Merging replaces files atomically: if any step fails, the old `bedrock_server.exe` is still there
- User data is preserved: `worlds/`, `server.properties`, `allowlist.json`, `permissions.json`, `banned-*.json` etc. are not overwritten by the official archive
- Official content folders (`resource_packs/`, `behavior_packs/`, `definitions/`) follow the selected version
- After installing, `.install_manifest.json` records which files were written; the next update only removes server artifacts that were installed last time and are gone from the new archive — **user files are never deleted**

## Integrity checks (performed on every download)

| Check | Description |
|---|---|
| File size | Rejected if below the minimum (incomplete download) or outside a sane range |
| ZIP structure | Must open with `zipfile` |
| Version match | The version must appear in the official version list (skipped when the list is unavailable) |
| SHA256 (TOFU) | The first download records the hash; later downloads of the same version must match or are aborted. The manifest and its HMAC signature are stored together, so deleting the signature cannot bypass the check |
| PE signature | On Windows the signature of `bedrock_server.exe` is verified; digest mismatch or a forged certificate **always fails**, while unsigned/unofficial files only warn by default |

**Strict mode**: set `MBES_REQUIRE_SIGNATURE=1` to abort on unsigned files as well.

> The official source publishes no SHA256, so TOFU only guarantees "same version stays consistent"; the trust of the first download depends on where you got it.

## Server updates

- A new version is checked once after selecting a server; a dialog appears when an update exists and stays silent otherwise
- The "🔄 Server update" button in the header checks manually at any time
- Update flow: **stop server → back up worlds → download and verify → staging merge**
- A failed update leaves the server usable (the old program is not deleted first)
- The server must be started manually after updating

## App updates (MbesBeacon itself)

- Checked automatically 3 seconds after startup; a dialog appears only when a new version exists
- The "📦 App update" button in the header checks manually
- The dialog shows the current version, newest version, publish time and release notes
- "Download update" opens the GitHub Release page
- The result is cached for 1 hour; the primary path parses the release web page and does not consume API quota

## Deleting a server

The red "Delete Server" card at the bottom of the page offers two modes:

| Mode | Behaviour |
|---|---|
| Program only | Keeps `worlds/`, `server.properties`, allowlist, permissions, etc., so another version can be installed |
| Delete the whole directory | Removes world saves too — **irreversible** |

- Worlds are backed up before deleting
- You must tick "I confirm the delete operation"
- A running server is stopped automatically first

## Parallel instances

- The top "Instance" dropdown lists registered server folders: `●` running / `○` stopped / `✓` active
- "➕ Add current folder" registers the current folder; "▶ Start / ■ Stop" act on the selected instance, and several can run at once
- Switching changes only the active instance and **does not stop the others**; each instance has its own folder, `worlds/` and console
- See the "Parallel instances" section of [02 Server config](en-02-config.md) for ports and caveats

## Related

- Saves: [05 Worlds](en-05-worlds.md) · [06 Backups](en-06-backups.md)
- Install troubleshooting: [12 FAQ](en-12-faq.md)
