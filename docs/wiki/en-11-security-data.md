# 11 Security & data

[← Back to contents](English.md)

## Security mechanisms

| Mechanism | Description |
|---|---|
| API token | Local web API calls need `X-API-Token`; comparison is constant-time |
| Remote sessions | With remote management enabled, non-loopback requests must exchange a session cookie via `/login` (`HttpOnly`, `SameSite=Strict`, 12 h sliding expiry) |
| Host header check | Accepts loopback and, when remote is enabled, local interface IPs / hostname / user-configured domains; mitigates DNS rebinding |
| Loopback by default | Listens on `127.0.0.1` unless "Remote Management" is enabled, which binds `0.0.0.0` and requires a password + session; trusted LAN only |
| Path boundaries | Delete/move/restore/import operations verify the target stays inside the server folder |
| Command validation | Rejects control characters; dangerous commands are normalised before matching, so `/stop`-style bypasses fail |
| Download verification | Size, ZIP structure, official version list, TOFU SHA256, PE signature |
| Login rate limit | Five failures from one IP within 5 minutes lock it for 10 minutes |
| Remote settings are local-only | Enabling or changing the password only works on the machine running the app |
| Tunnel commands are local-only | Configuring/starting/stopping external tunnel clients only works locally, so remote sessions cannot run arbitrary commands |
| Third-party tool verification | lip / playit / frp are downloaded only from official Releases and SHA256-verified; mod archives are extracted with path-traversal checks |
| Signature policy | Tampering (digest mismatch, forged certificate) always fails; unsigned files only warn unless `MBES_REQUIRE_SIGNATURE=1` |
| Audit log | Key operations are recorded and rotated by size |
| Atomic writes | Config/permission/allowlist writes use a temp file + atomic replace, so a crash never leaves a half-written file |

> MbesBeacon runs entirely locally and uploads no data.

## Data directory

User data lives in `%APPDATA%\MbesBeacon\`:

```
%APPDATA%\MbesBeacon\
├─ settings.json                app settings (server folder, installed version, language, ...)
├─ theme.json                   theme configuration
├─ settings history/config history  snapshots used for rollback
├─ logs\
│  ├─ app.log                   application log
│  ├─ crash.log                 crash log
│  └─ audit.log                 audit log (rotated by size, e.g. audit.1.log)
└─ cache\
   ├─ hashes.json               TOFU hash manifest
   ├─ hashes.json.mac           HMAC signature of the manifest
   ├─ app_update_cache.json     app update check cache (1 hour)
   └─ bds_versions.json         official version list cache
```

Files inside the server folder (belonging to that server):

```
<server folder>\
├─ bedrock_server.exe           server executable
├─ server.properties            configuration
├─ permissions.json             permissions
├─ allowlist.json               allowlist
├─ banned-players.json          banned players
├─ banned-ips.json              IP bans
├─ worlds\                      world saves
├─ resource_packs\              resource packs
├─ behavior_packs\              behavior packs
├─ .install_manifest.json       install manifest (safe stale-file cleanup)
└─ .install_staging\            download/extract staging (short-lived during install)
```

Backup folder: `_worlds_backups\` next to the server folder.

## Backup and migration

- New machine: copy `%APPDATA%\MbesBeacon\` plus the server folder (including `worlds/`)
- Saves only: export a ZIP via "⑤ Worlds", or copy `worlds/` directly
- On the first start after migrating, the server folder is detected again; use "📁 Browse" if needed

## Logs and troubleshooting

| File | Purpose |
|---|---|
| `logs\app.log` | Day-to-day application log |
| `logs\crash.log` | Crash stack traces |
| `logs\audit.log` | Who did what and when |
| Console "Export log" | Output of the current session |

## Recommendations

1. Do not expose the management UI to the internet (loopback by default)
2. Download servers from official sources; use custom URLs only for trusted mirrors
3. Enable `online-mode` or at least configure the allowlist
4. Back up `worlds/` and `%APPDATA%\MbesBeacon` regularly
5. Watch the Releases page for security updates
