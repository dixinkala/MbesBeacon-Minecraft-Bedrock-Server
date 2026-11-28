# 04 Players & permissions

[← Back to contents](English.md)

## Online players

- Requires a **running** server; click "🔄 Refresh players" to fetch the current list
- Each card shows the player name and current permission level
- Available actions:

| Action | Description | Implementation |
|---|---|---|
| Set permission | `visitor` / `member` / `operator` | Sends `permission` or writes `permissions.json`; while running, the process is updated first |
| Kick | Reason optional | `kick <player> [reason]` |
| Add to blocklist | Reason optional | `ban <player> [reason]` |

Some actions write the file first and then send the command, so the change is visible in the UI and can be undone.

## Permission levels

| Level | What it allows |
|---|---|
| `visitor` | View only, cannot change anything |
| `member` | Normal gameplay, no admin commands |
| `operator` | Full admin commands |

Permissions are stored in `permissions.json` in the server folder and can be edited manually while the player is offline.

## Allowlist

- Lists allowed players; each entry can be removed
- Type a name at the bottom to add one **without the player being online**
- Takes effect through commands while running, or by editing `allowlist.json` while stopped
- It only works once `allow-list` is enabled in [02 Server config](en-02-config.md)

## Blocklist (bans)

- Shows banned players and reasons; "Unban" removes an entry
- You can add a name + reason manually without the player being online
- Data comes from `banned-players.json`

## IP bans

- Useful against ban evasion: reads/writes `banned-ips.json`
- Add an IP with a reason manually, or lift the ban
- While running, the ban is written to the file and sent as a command, so it survives a restart

## Notes

| Case | Description |
|---|---|
| Offline mode | With `online-mode=false`, third-party/offline accounts can join; the allowlist is the only gate |
| Allowlist has no effect | Check that `allow-list=true` is saved and the server has been **restarted** |
| Commands ignored | Admin commands need a running server, but file operations still work |
| Non-ASCII names | Supported; files are written as UTF-8 |

## Related

- Player commands: [03 Console & commands](en-03-console-commands.md)
- Permission files: [11 Security & data](en-11-security-data.md)
