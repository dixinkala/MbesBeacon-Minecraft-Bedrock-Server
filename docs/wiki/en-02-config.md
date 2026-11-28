# 02 Server config

[← Back to contents](English.md)

The "② Server Config" tab edits `server.properties` inside the server folder directly.

## Common items (about 17)

| Key | Purpose | Notes |
|---|---|---|
| `server-name` | Server name | Shown in the player's server list |
| `gamemode` | Game mode | `survival` / `creative` / `adventure` / `spectator` |
| `difficulty` | Difficulty | `peaceful` / `easy` / `normal` / `hard` |
| `allow-cheats` | Allow cheat commands | When off, even operators lose some commands |
| `max-players` | Maximum players | Limited by server performance |
| `online-mode` | Xbox Live authentication | When off, offline/third-party accounts can join |
| `allow-list` | Enable the allowlist | Only listed players may join when on |
| `server-port` | Port | Default `19132` (UDP) |
| `server-portv6` | IPv6 port | Usually left at the default |
| `level-name` | Active world folder | Changed automatically when switching worlds on "⑤ Worlds" |
| `level-seed` | World seed | Only affects newly generated worlds |
| `view-distance` | View distance | Bigger values use more memory |
| `tick-distance` | Simulation distance | Affects how far mobs stay active |
| `player-idle-timeout` | Minutes before kicking idle players | `0` never kicks |
| `default-player-permission-level` | Default permission | `visitor` / `member` / `operator` |
| `texturepack-required` | Force the server resource pack | |
| `language` | Server log language | Does not change the in-game language |

## Advanced mode

Tick "Advanced config" to show **all 46 keys** (including those not in the common list) in a generic `key=value` editor where any value can be changed.

## Saving and applying

- "Save config" writes the file; **a server restart is required to apply it**
- "Reload" re-reads from disk and discards unsaved edits
- When no server is installed, these actions ask you to install one first

## Format and safety

- **Format preserved**: comments, blank lines and custom keys not shown on the page are kept when saving, so hidden items are never dropped
- **Value validation**: ports, player counts and view distances are range-checked as you type, with immediate feedback
- Duplicate keys and `key = value` (spaces around `=`) are parsed correctly
- Writes use atomic replacement, so an interrupted process never leaves a half-written file

## Parallel instances

The app can manage several server folders and **run them at the same time**:

- The top "Instance" dropdown lists registered folders; `●` means running, `✓` the active instance
- "➕ Add current folder" registers the current server folder; "✕ Remove instance" removes it (stopping it first)
- "▶ Start / ■ Stop" act only on the selected instance
- Selecting another instance switches the active one: config, worlds, backups, packs and console all follow it

Key points:

| Item | Description |
|---|---|
| Port | Each instance uses its own `server.properties` port; one UDP port cannot be bound twice |
| Console | Every instance has its own buffer; the console shows the active instance after switching |
| Players/performance | Apply to the active instance only (parsed from its console) |
| Folder | An instance must be an installed server folder (official exe or LeviLamina's `bedrock_server_mod.exe`) |
| Stopping | Removing/switching does not stop running instances automatically; stop them explicitly |

## Config history and rollback

- Every save stores a history version
- "Config history" lets you inspect and roll back to any previous version
- A rollback also requires a server restart to apply

## Related pages

| Key | Page |
|---|---|
| `allow-list` | [04 Players & permissions](en-04-players-permissions.md) (allowlist) |
| `level-name` | [05 Worlds](en-05-worlds.md) (switching worlds) |
| `server-port` | [09 Networking](en-09-network.md) (port conflicts) |
