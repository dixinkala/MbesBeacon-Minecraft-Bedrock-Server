# 12 FAQ

[← Back to contents](English.md)

## Startup and running

**Q: On first run Windows says "Windows protected your PC"?**
A: SmartScreen warns about new programs without many downloads. Click "More info" → "Run anyway". The app is an open-source local tool and uploads no data.

**Q: Is the server still running after I close the browser tab?**
A: Yes. The page is only the UI; the server process runs independently. Reopen it from the tray or by double-clicking the exe.

**Q: How do I exit completely?**
A: Console "Stop server" → header "Exit program".

**Q: The tray icon disappeared?**
A: Click the `▲` in the notification area to show hidden icons; you can drag the icon onto the taskbar to pin it.

**Q: Starting the server does nothing?**
A: Check the Performance tab / console messages. Common causes: port in use, incomplete folder (missing `bedrock_server.exe`), antivirus blocking.

## Networking

**Q: Friends cannot join?**
A: Check in order: same network → you gave the LAN IP (not `127.0.0.1`) → firewall allows UDP 19132 → server is running → allowlist / online mode.

**Q: How do I open it to the internet?**
A: Forward the UDP port on the router, or use a tunnel; cloud servers also need the security group opened.

**Q: Server download fails / SSL error?**
A: ① Retry; ② tick "Skip SSL verification on network errors"; ③ use a custom download URL pointing at a mirror; ④ confirm `minecraft.net` is reachable.

**Q: The version list will not load?**
A: The list comes from the [self-hosted manifest](https://github.com/dixinkala/mbesbeacon-bds-versions); if unreachable it falls back to the community list and then the built-in snapshot. You can also type a version manually or use a custom URL.

## Config and data

**Q: Config changes have no effect?**
A: Save and restart the server.

**Q: Where is user data?**
A: `%APPDATA%\MbesBeacon\`; see [11 Security & data](en-11-security-data.md).

**Q: Where are the saves? How do I back them up?**
A: Under `worlds/` in the server folder; "⑥ World Backups" supports manual and scheduled backups, and updates/deletions back up automatically.

**Q: Port already in use?**
A: Change `server-port` (e.g. 19133) and restart; a UDP conflict check runs before starting.

## Updates and versions

**Q: How do I update the server?**
A: It is checked automatically after selecting a server, or click "🔄 Server update" in the header; updates stop the server and back up worlds first, and a failure leaves the current server usable.

**Q: How do I update the app itself?**
A: "📦 App update" in the header → "Download update" opens the Release page; replace the exe (config and saves are unaffected).

**Q: Where are the official hashes?**
A: The official source publishes no SHA256; the app combines the official version list, PE signature and a TOFU baseline. See [11](en-11-security-data.md).

## Multiple servers

**Q: My server was not detected?**
A: On the "① Install Server" tab click "📁 Browse" to pick the folder; it is remembered afterwards.

**Q: Can I manage/run several at once?**
A: Yes. Use the top "Instance" dropdown and "➕ Add current folder" to register several folders, then press "▶ Start" to run them in parallel; switching instances does not stop the others. Ports and consoles are independent — see [02 Server config](en-02-config.md).

**Q: Do switching/deleting lose saves?**
A: Switching does not. Deleting has two modes: "program only" keeps saves, "delete the whole directory" removes them too.

## Other

**Q: Is Linux supported?**
A: Ubuntu 20.04+ (server management works; the tray degrades gracefully without a desktop session).

**Q: What licence is used?**
A: MIT — see [LICENSE](../LICENSE).

**Q: Something not covered here?**
A: Open an issue with logs: [New issue](https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server/issues/new). Attaching the relevant files from `%APPDATA%\MbesBeacon\logs` speeds up diagnosis.
