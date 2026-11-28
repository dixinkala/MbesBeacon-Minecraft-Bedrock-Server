# 10 Tray, themes, i18n

[← Back to contents](English.md)

## System tray

| Behaviour | Description |
|---|---|
| Stays resident | The app minimises to the tray; the icon reflects the server state |
| Context menu | Open the UI / start / stop / exit |
| Tooltip | Hover shows "MbesBeacon — … — running / stopped" |
| Closing the browser | Only the UI closes; the server keeps running |
| Reopening | Double-click the exe or click "Open UI" in the tray menu |

> Stop the server in the console before exiting the app, so saves are not interrupted.

## Instance selector

The "Instance" dropdown in the header lists registered server folders: `●` running / `○` stopped / `✓` active.
The buttons next to it are "➕ Add current folder", "✕ Remove instance", "▶ Start" and "■ Stop"; after switching, every page follows the active instance.

## Themes

"🎨 UI style" in the header:

- **10 presets**: Minecraft Dark, Light Minimal, Grass Green, Redstone Red, Diamond Blue, Forest Green, Ocean Blue, Sunset Orange, Purple Tech, Pink
- **Custom accent colour**: pick any colour
- **Light/dark mode**: adjustable on its own (presets set a matching mode)
- Saved to `theme.json` and restored on the next start

## Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+S` | Save config |
| `Ctrl+R` | Restart the server |
| `Ctrl+F` | Console search |
| `` ` `` (backtick) | Focus the console command box |
| `↑` / `↓` | Command history / completion candidates |
| `Tab` | Command completion |

## Languages

- The UI is bilingual (Chinese/English); switching applies immediately and is remembered
- The first start follows the system language
- Tray menu, notifications and tooltips follow the same language
- Console output comes from the server and is not affected by the UI language

## Display notes

- Narrow windows scroll tables horizontally; maximising is recommended
- Console highlighting keeps red/green distinction in dark themes
- If text looks small on a high-DPI display, adjust the scaling in the OS display settings
