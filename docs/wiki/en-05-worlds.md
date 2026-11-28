# 05 Worlds

[← Back to contents](English.md)

The "⑤ Worlds" tab works on the `worlds/` folder inside the server directory.

## World list

- Lists every world folder under `worlds/`
- The active world (the one `level-name` in `server.properties` points at) is marked "in use"
- Shows basic world info (name, game mode, difficulty, etc.)

## Switching worlds

1. Pick the target world and click "Switch"
2. The app updates `level-name` in `server.properties` (keeping the rest of the file and its formatting)
3. **Takes effect after restarting the server**

> Switching only changes the config; no save is moved or deleted.

## Rename / copy / delete

| Action | Description |
|---|---|
| Rename | Renames the world folder; if it is the active world, `level-name` is updated too |
| Copy | Duplicates a save for backup or testing; a suffix is added if the name already exists |
| Delete | Deletes the world folder after a second confirmation — irreversible |

Safety boundaries:

- World names are path-checked; `..`, absolute paths and separators are rejected
- The target must stay inside `worlds/` (symlink escapes are blocked too)
- Dangerous directories (such as system folders) are never deleted as worlds

## Import / export saves

### Export (📤)

- Packages `worlds/` as a ZIP download
- File name: `{server name}_worlds_{YYYYmmdd_HHMMSS}.zip`
- Streaming download, no extra temporary space required

### Import (📥)

1. Pick a ZIP save
2. The app first **backs up** the current `worlds/` to `worlds_backup_{timestamp}`
3. Type `IMPORT` to confirm
4. The ZIP structure and paths are validated before extraction
5. The world list is refreshed automatically

Four common layouts are supported: `worlds/` at the root, a single world folder, multiple worlds side by side, and one extra outer folder.

> Prefer importing/exporting while the server is **stopped**, so open file handles cannot break it.

## Difference from backups

| | Worlds | World backups |
|---|---|---|
| Object | A single world folder | The whole `worlds/` |
| Purpose | Switch / organise / migrate one save | Periodic snapshots and rollback |
| Tab | ⑤ Worlds | ⑥ World backups |

See [06 Backups](en-06-backups.md).

## FAQ

- **Still the old world after switching**: the config needs a restart — confirm it was restarted.
- **Imported world is missing**: the ZIP layout is non-standard; extract it and copy the world folder into `worlds/`, then refresh.
- **Can a deleted world be restored**: only from a backup; back up before deleting.
