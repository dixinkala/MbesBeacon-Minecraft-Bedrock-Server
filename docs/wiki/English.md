# MbesBeacon — Full Manual (Wiki)

> This is the **complete documentation**, covering every MbesBeacon feature and its edge cases.
> The in-app "⑩ Help" tab only keeps a quick reference; see this directory for details.
>
> **Online version**: <https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/wiki>
> This directory holds the **sources** for the wiki: edit here, then publish with
> `python tools/sync_wiki.py` (page name equals file name, `README.md` maps to `Home`;
> the script rewrites links and generates the sidebar).

**中文文档 / Chinese: [README](README.md)**

## Chapters

| Chapter | Content |
|---|---|
| [01 Install & update](en-01-install-update.md) | Requirements, first-run wizard, version choice, integrity checks, server/app updates, deleting a server |
| [02 Server config](en-02-config.md) | `server.properties` common items, advanced mode, validation, config history, parallel instances |
| [03 Console & commands](en-03-console-commands.md) | Start/stop, live logs, search/export, completion and history, dangerous commands, common commands |
| [04 Players & permissions](en-04-players-permissions.md) | Online players, permission levels, kick/ban/unban, allowlist, blocklist, IP bans |
| [05 Worlds](en-05-worlds.md) | World list, switching, rename/copy/delete, import/export |
| [06 Backups](en-06-backups.md) | Manual/automatic backups, restore and rollback, retention, location and naming |
| [07 Packs](en-07-packs.md) | Resource/behavior packs, enable/disable, import (with overwrite), delete, size limits, LeviLamina loader and mods |
| [08 Monitor & scheduler](en-08-monitor-scheduler.md) | Performance metrics, audit log, scheduled restart/backup/announcements, catch-up |
| [09 Networking](en-09-network.md) | LAN play, remote management, public access (UPnP / built-in playit, frp), ports and firewall |
| [10 Tray, themes, i18n](en-10-tray-ui-i18n.md) | System tray, instance selector, 10 themes, shortcuts, Chinese/English switching |
| [11 Security & data](en-11-security-data.md) | Authentication and verification, signature policy, data locations, migration, logs |
| [12 FAQ](en-12-faq.md) | Startup, networking, config, updates, multiple servers, common errors |

## How this relates to the in-app help

- **In-app help** (the "⑩ Help" tab): a quick reference for common tasks, a few screens long
- **This wiki**: the full manual, including parameter meanings, edge cases, failure handling and troubleshooting

## Other documents

- [README](../README.md) · [README_EN](../README_EN.md)
- [Security policy](../SECURITY.md) · [Contributing](../CONTRIBUTING.md) · [Code of conduct](../CODE_OF_CONDUCT.md)
- [License](../LICENSE) (MIT)

## Scope

- Windows 10/11 (primary target)
- Ubuntu 20.04+ (server management works; the tray degrades gracefully without a desktop session)
- Server version: any Minecraft Bedrock Dedicated Server release
