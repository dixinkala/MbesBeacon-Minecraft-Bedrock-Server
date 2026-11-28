# 07 Packs

[← Back to contents](English.md)

The "⑧ Packs" tab manages resource packs and behavior packs.

## Pack list

- Scans `resource_packs/` and `behavior_packs/` inside the server folder
- Shows name, version and enabled state
- "Refresh list" rescans (use it after copying files into the folders manually)

## Enable / disable

Enabling a pack does two things:

1. Writes to the active world's `world_resource_packs.json` / `world_behavior_packs.json`
2. Registers it in `valid_known_packs.json` (BDS uses this to recognise available packs)

- **A server restart is required**
- Disabling removes it from those configs but keeps the pack files on disk

## Import

Supports `.zip` and `.mcpack`:

1. Click "Import resource pack / Import behavior pack" and choose a file
2. The app extracts it into the matching folder
3. New packs are **disabled by default**; enable them in the list

Import checks:

| Check | Description |
|---|---|
| Structure | The ZIP must open and contain at least one `manifest.json` |
| Name conflict | Import is **refused** when a pack with the same name exists; confirm overwrite and retry with `overwrite=true` |
| On-disk name | Windows replaces invalid characters with `_`; the guard uses the **actual on-disk name**, so special-character names cannot bypass it |
| Extraction size | Capped to prevent zip bombs |
| Paths | Entry paths are boundary-checked so nothing is written outside the pack folder |

Overwrite import **deletes the old folder first**, avoiding a mix of new and old files (new manifest, union of resources).

## Delete

- Click "Delete" and type `DELETE` to confirm
- Cleans up the pack folder, its `valid_known_packs.json` entry and references in the world config

## FAQ

| Problem | Cause and fix |
|---|---|
| Invisible in game after import | Packs are disabled by default; enable them and restart the server |
| Enabled but no effect | Make sure you changed the **active world's** pack config; switching worlds requires enabling again |
| Same-name import fails | By design, to avoid silent overwrites; confirm and import with overwrite |
| `.mcpack` will not open | Confirm it is a ZIP container (`.mcpack` is a zip) |

## LeviLamina mods

The bottom of the "⑧ Packs" tab offers LeviLamina support (Windows x64 only; LeviLamina publishes Windows server builds only).

### Enable / disable the loader

1. Click "Download lip" (downloaded from official GitHub Releases and SHA256-verified)
2. Click "Enable LeviLamina"; after confirmation the app runs `lip setup --game <version> --build windows --loader levilamina`
3. About 100 MB is downloaded; afterwards the server becomes `bedrock_server_mod.exe` and the app launches it automatically
4. "Disable" renames `bedrock_server_mod.exe` and restores the official `bedrock_server.exe`; `mods/` and lip data are kept

> Enabling **reinstalls server files for the compatible LeviLamina version** (for example LeviLamina 26.60.9 targets BDS 1.26.60).
> Config files are backed up first to `mbes_backup/ll_<timestamp>/`. World saves are untouched, but a backup beforehand is still recommended.

### Managing mods

| Action | Description |
|---|---|
| Install a mod (.zip) | Upload a zip; lip workspaces use `lip install`, otherwise the archive is extracted safely to `mods/<name>/` |
| lip install / uninstall | Enter a package name or URL and run `lip install` / `lip uninstall` |
| Enable / disable | Moves the entry between `mods/` and `mods.disabled/` |
| Delete | Removes the mod folder/file after confirmation |

- Name, version and type come from the mod's `manifest.json`
- `mods/LeviLamina` is the loader itself and is not listed as a mod

## Related

- Packs are overwritten by updates: see [01 Install & update](en-01-install-update.md) (`resource_packs/` is official content and follows the version)
- Switching worlds changes the "active world": see [05 Worlds](en-05-worlds.md)
