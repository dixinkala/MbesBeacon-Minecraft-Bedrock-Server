# 06 Backups

[← Back to contents](English.md)

The "⑥ World Backups" tab manages snapshots of the server's `worlds/`.

## Back up now

- Copies the current `worlds/` into the backup directory as a timestamped snapshot
- The backup directory is `_worlds_backups/` next to the server directory by default
- The list shows time and size, sortable by time

## Automatic backups

Trigger points:

| Scenario | Description |
|---|---|
| Before a server update | Back up first, then download and install |
| Before deleting a server | Back up first, then delete (both modes) |
| Before importing worlds | Snapshot the current `worlds/` first |
| Before restoring a backup | Snapshot the current worlds so the restore can be undone |

## Retention

- Keeps the latest **10** backups by default and removes the oldest beyond that
- The limit is configurable, up to **100**
- Cleanup only matches folders named `{server name}_worlds_{YYYYmmdd_HHMMSS}` and **never deletes your own folders**

## Restore

1. Select a backup and click "Restore"
2. A running server is stopped automatically first
3. The current `worlds/` is renamed aside (not deleted)
4. The backup content is copied into place
5. Any failure **rolls back**: the partial copy is removed and the original `worlds/` is restored

## Delete a backup

- Select and delete with a second confirmation
- Only the backup folder is removed; the live worlds are untouched

## ZIP export

- Any backup can be downloaded as a ZIP for archiving or migration
- The file name contains the server name and timestamp

## What a backup contains

| Included | Not included |
|---|---|
| All world folders under `worlds/` | `server.properties`, allowlist, permissions (config, see [02](en-02-config.md)) |
| `level.dat`, `db/` etc. inside each world | Resource/behavior packs (see [07](en-07-packs.md)) |

> For a full migration, copy `%APPDATA%\MbesBeacon` and the server directory together; see [11](en-11-security-data.md).

## If a restore fails

Restore has rollback protection; if it still fails, check:

1. Free disk space (worlds can be large)
2. That the server is really stopped (open handles make file operations fail)
3. That the backup folder was not moved or renamed manually

Error details are written to the audit log and the app log.
