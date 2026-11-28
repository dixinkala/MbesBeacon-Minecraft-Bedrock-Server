# 09 Networking

[← Back to contents](English.md)

## LAN play

1. After the server starts, the header shows `服务器地址: <IP>:19132`; click 📋 to copy
2. Friends on the same Wi-Fi/switch open "Servers" → "Add server" in game
3. Enter **your LAN IP** and the port (default `19132`)

> `127.0.0.1` only works on the same machine — never give the loopback address to friends.

## Public access

| Approach | Description |
|---|---|
| Router port forwarding | Forward public `UDP 19132` to the server's LAN IP on `19132` |
| NAT traversal | Use frp / n2n / a commercial tunnel to expose the UDP port |
| Cloud server | Run directly on a public machine; allow UDP in the security group |

> BDS uses **UDP**; opening only TCP will not work.

## Firewall

- Windows shows a firewall prompt on first start; tick "Private networks" and allow
- If you clicked "Cancel" by mistake, add it under "Windows Defender Firewall → Allow an app through firewall"
- Cloud servers also need the security group configured

## Ports

- Default `19132` (UDP), matching `server-port` in `server.properties`
- To change it: edit it on "② Server config", save and restart
- **Port check**: probed over UDP before starting; a conflict is reported instead of "started but unreachable"

## Finding your IP

- The header shows it and allows copying
- Or run `ipconfig` in a terminal and read "IPv4 Address"

## Connection troubleshooting

| Symptom | Check in order |
|---|---|
| Friend cannot join | ① same network ② correct LAN IP ③ firewall UDP ④ server running |
| Worked before, now not | The IP changed (DHCP); copy the address again |
| Internet players cannot join | Is the mapping UDP, did the public IP change, does the ISP block the port |
| Connects but cannot enter | Allowlist / online mode / max players; see [02](en-02-config.md), [04](en-04-players-permissions.md) |
| Version mismatch | Client and server must match; see [01](en-01-install-update.md) |

## Remote management (LAN web access)

By default only **this machine** can open the management UI. To manage from a phone or another PC:

1. On the machine running the app open "🌐 Remote Management" at the bottom of the "① Install Server" tab
2. Tick "Enable remote access", set a password (at least 8 characters) and save
3. **Restart the app** (the bind address changes from `127.0.0.1` to `0.0.0.0`)
4. On the other device open `http://<LAN IP>:19100/` and log in

Security rules:

- The password is stored salted with PBKDF2-SHA256; no plaintext is kept
- A successful login sets an `HttpOnly` + `SameSite=Strict` session cookie that slides for 12 hours
- Five failures from one IP within 5 minutes lock it for 10 minutes
- The Host header is still allowlisted (local interface IPs, hostname, user-configured domains) against DNS rebinding
- Sessions live in memory only, so a restart requires logging in again

> Traffic is plain HTTP — **trusted LAN only**. For internet access use a tunnel or a reverse proxy with TLS,
> and switch to a strong password first. Remote settings can only be changed on the machine running the app.

## Public access (NAT traversal)

BDS uses **UDP 19132**, so only traversal that supports UDP works (TCP-only options such as Cloudflare Tunnel do not).

### Option 1: UPnP automatic port mapping

1. "① Install Server" tab, bottom → "🌐 Public Access (NAT traversal)"
2. Click "Auto-map port (UDP)"; the public IP is shown on success
3. Send `public IP:19132` to friends; "Remove mapping" undoes it

If the router has UPnP disabled or unsupported, the app says so; forward `UDP 19132` to this machine manually in the router admin page.

> If the shown public IP is `10.x` / `192.168.x` / `172.16-31.x` / `100.64-127.x`,
> you are behind double NAT or carrier-grade NAT (CGNAT); internet players usually cannot connect — use option 2.

### Option 2: Built-in tunnels (recommended, no manual client install)

The bottom of the "🌐 Public Access" card offers two built-in tools; the app **downloads them from official GitHub Releases and verifies SHA256**:

| Tool | Requires | Public address |
|---|---|---|
| **playit** | A playit.gg account (one click to claim) | After claiming, create a UDP tunnel on the website; address looks like `xxx.playit.gg:port` |
| **frp** | Your own public server (frps) | `server address:remote port`, shown right after start |

playit steps:

1. Click "Download & install" (downloads playit and playitd)
2. Click "Generate claim link" → open it, log in and claim, then create a **UDP tunnel** to `19132` on the website
3. Click "Check claim status"; once claimed, click "Start" and the tunnel address appears
4. Share the address; click "Stop" when done

frp steps:

1. Click "Download & install frpc"
2. Enter the server address, server port (default 7000), an optional token, and the remote port (default 19132)
3. "Save" → "Start"; the UI shows `server address:remote port`

> Security: both tools are downloaded only from official Releases with digest verification, and can only be operated on the machine running the app.
> frp is often flagged by Windows Defender and similar AV; on "Access denied" add the `tools\frp` folder to the exclusions.
> Licences: playit is BSD-2-Clause, frp is Apache-2.0.

### Option 3: External tunnel clients

The app ships no third-party binaries; install a client yourself and enter a command (`{port}` is replaced with the server port):

| Client | Example command | Address detection |
|---|---|---|
| playit | `playit` (enable a UDP tunnel per its docs) | `xxx.playit.gg:port` in the log |
| ngrok | `ngrok tcp {port}` | `tcp://host:port` |
| frp | `frpc -c frpc.toml` | `IP:port` in the log |
| Custom | Any command | Provide a "public address regex" such as `(?P<host>...)` |

- The public address appears in the UI and recent log lines are kept for troubleshooting
- The client process is terminated when the app exits
- Security: tunnel commands can only be configured/started on the machine running the app (remote sessions cannot)

## Related

- Port config: [02 Server config](en-02-config.md)
- Allowlist and bans: [04 Players & permissions](en-04-players-permissions.md)
