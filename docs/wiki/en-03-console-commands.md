# 03 Console & commands

[← Back to contents](English.md)

## Start, stop and restart

| Action | Behaviour |
|---|---|
| Start | Checks the port first, then launches the server process |
| Stop | Sends `stop` so the server saves the world itself; force-kills after a timeout |
| Restart | Stop → wait for exit → start again; consecutive restarts are rate-limited to one per 15 s |

An abnormal exit is detected and recorded, and the UI state updates accordingly.

## Live log

- **SSE push**, usually < 300 ms latency, auto-scrolls to the bottom
- Keeps the latest **2000 lines** so long sessions do not grow unbounded
- Highlighting: `ERROR` / `WARN` in red, player joins in green, keywords in distinct colours
- Clear: clears the view and resets the backend cursor, so old lines do not "come back"
- Export: writes the current console content to a `.txt` file
- Search: `Ctrl+F` filters locally; clearing the log also resets the search state

## Command input

| Action | Shortcut |
|---|---|
| Focus the command box | Backtick key (`` ` ``, below Esc) |
| Send the command | Enter |
| Command history | ↑ / ↓ |
| Completion | Tab, or ↑/↓ to pick from suggestions |

- Suggestions come from the server and are throttled to one request per 120 ms
- Empty input or a stopped server shows a hint instead of failing silently

## Dangerous commands

- The dangerous-command list is served by the **backend** (`/api/commands/dangerous`); the front end no longer hard-codes it
- Commands on the list require a second confirmation before running
- It covers `stop`, `whitelist off`, `allowlist off`, permission changes, bans and similar

## Common commands

```
list                          show online players
say message                   broadcast to everyone
op <player> / deop <player>   grant / revoke operator
whitelist add <player>        add to the allowlist (same as allowlist add)
whitelist remove <player>     remove from the allowlist
kick <player> [reason]        kick
ban <player> [reason]         ban
pardon <player>               unban
stop                          stop the server
```

> Vanilla BDS accepts both `whitelist` and `allowlist` for the allowlist commands.

## Logs and troubleshooting

- Console output is also written to the app log; see [11 Security & data](en-11-security-data.md) for its location
- Crashes with automatic restart, port conflicts and failed downloads are reported on the "Performance" tab or in dialogs

## Related

- Player permissions: [04 Players & permissions](en-04-players-permissions.md)
- Permission file: `permissions.json` (see 04)
