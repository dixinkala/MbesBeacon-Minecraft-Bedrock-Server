# 08 Monitor & scheduler

[← Back to contents](English.md)

## Performance monitor (⑦)

| Metric | Description |
|---|---|
| CPU | CPU usage of the server process (non-blocking; the first sample may read 0) |
| Memory | Process memory usage (MB) |
| Online players | Current player count |
| Status | Running / stopped |
| TPS | Bedrock servers do not expose TPS, so the UI shows 0 (reserved) |

- Auto-refreshes every 5 seconds by default; auto-refresh can be turned off
- Values come from system process info and are empty while the server is stopped

## Audit log

Records all **dangerous/key operations**:

| Category | Examples |
|---|---|
| Server | Start / stop / restart / delete / switch |
| Config | Save config, config rollback |
| Players | Kick / OP / permission change / ban / unban / allowlist changes |
| Worlds | Switch / rename / delete / copy / import |
| Backups | Create / pack / restore / delete |

- Each entry has a timestamp, action, arguments and result
- Rotated by size under a lock; entries are not lost during rotation
- Readable from the log file or via diagnostics; see [11 Security & data](en-11-security-data.md)

## Scheduled tasks (⑨)

Three task types:

| Type | Configuration | Purpose |
|---|---|---|
| Scheduled restart | Daily at a time / fixed interval | Restart during off-peak hours |
| Scheduled backup | Daily at a time / every N hours | Automatic world snapshots |
| Scheduled announcement | Daily at a time + message | Sends a `say` broadcast |

### Behaviour

- **Only runs while the server is running**; otherwise it is skipped (no queue buildup)
- Changes take effect immediately, no app restart needed
- Tasks can be enabled / disabled / deleted
- The list shows the last run and the next scheduled time

### Catch-up

If the app was not running at the scheduled moment, **missed daily tasks run once on the next start** (one catch-up per task type, never a burst).

### State persistence

- Execution state is stored in the scheduler state file, so a restart does not re-run the same task
- State is bound to the server folder it belongs to: switching folders never writes the old folder's state to the new one

## FAQ

| Problem | Fix |
|---|---|
| Task did not run | The server was not running at that moment, or the task is disabled |
| Task runs again after restart | State is persisted; if it still happens, check whether the state file was deleted manually |
| Announcement not sent | Check the server is online and the message is not empty |
| Backup task produced no file | Check free disk space and the retention limit (the oldest is removed when full) |
