# Borderlands support for Mod Organizer 2

A plugin that lets [Mod Organizer 2](https://www.modorganizer.org/) (MO2) manage Borderlands games:
it finds the game, puts mod files where the game expects them, runs mod-shipped tools after checking
them, and for Borderlands 4 installs and maintains the [unreal SDK](https://github.com/bl-sdk/oak2-mod-manager)
so SDK mods work through MO2's virtual file system.

Full documentation lives in the **[wiki](https://github.com/Exigentec/modorganizer-borderlands/wiki)**.

## Compatibility

Developed and tested against **MO2 2.5.2** (Python 3.12, PyQt6) on Windows 11, Steam installs only.

| Game | Steam ID | Placement of mod files | SDK / usvfs handling | Status |
|---|---|---|---|---|
| Borderlands 4 | 1285190 | `.pak/.utoc/.ucas`, `.sdkmod`, Python packages, `Tools/` | yes | **play-tested** |
| Borderlands 3 | 397540 | `.pak/.utoc/.ucas` into `~mods` (setting), `Tools/` | no | implemented, unit-tested only |
| Borderlands 2 | 49520 | `.upk`, `Tools/` | no | implemented, unit-tested only |
| Borderlands The Pre-Sequel | 261640 | `.upk`, `Tools/` | no | implemented, unit-tested only |
| Borderlands GOTY Enhanced | 729040 | `.upk`, `Tools/` | no | implemented, unit-tested only |

Epic and GOG installs are not detected.

## Install

1. Close MO2.
2. Copy the `borderlands` folder into `<MO2>\plugins\` (or run
   `.\scripts\Install-Plugin.ps1 -Mo2Path C:\Apps\MO2`).
3. Start MO2, create (or switch to) an instance and pick the Borderlands game.

Details and removal: [Installation](https://github.com/Exigentec/modorganizer-borderlands/wiki/Installation).

## Use

- **Install mods as usual.** Loose paks, `.sdkmod` files and wrapped folders are placed for you; if the
  install dialog shows a wrapper folder, pick it with **Set as <game> directory**.
- **Borderlands 4 SDK:** on first start with SDK mods enabled the plugin offers to set the SDK up. Manage it
  under **Tools > Borderlands SDK**.
- **usvfs:** if MO2's usvfs is too old for SDK mods the plugin offers to upgrade it (with a backup) and to
  restart MO2. Manage it under **Tools > Borderlands usvfs**.
- **Mod programs:** programs shipped under `Tools/` appear as **Tools > Borderlands: <name>**.
- **Mod list:** show MO2's **Content** column to see which mods hold SDK mods or paks (the plugin un-hides
  it once).

## Features

- Game support
  - Steam discovery, game folder as MO2's data directory, save-game listing, profile ini copies
  - Per-game `pak_subdir` setting (BL3/BL4)
- Mod placement ([details](https://github.com/Exigentec/modorganizer-borderlands/wiki/Mod-Placement))
  - Loose and wrapped paks, `.sdkmod` files, Python packages, `.upk` files and `Tools/` programs
  - Refuses rather than guesses when a layout is ambiguous
  - Reads `metadata.json`; optional hiding of README/INSTALL-style files
- Borderlands 4 SDK ([details](https://github.com/Exigentec/modorganizer-borderlands/wiki/OAK2-SDK-Support))
  - Set up, verify, auto-heal, update, switch off and on (hash-checked, from the project's GitHub releases)
  - Pinned `SDK Mod Support` mod (lowest priority) and `SDK Mod Settings` mod (highest priority)
  - Notifications, Content-column icons for SDK and pak mods
  - `sdk_mods/settings` and `sdk_mods/keybinds` writes land in `SDK Mod Settings`, not Overwrite
- usvfs swap-out ([details](https://github.com/Exigentec/modorganizer-borderlands/wiki/usvfs-Swapout))
  - Version check at start-up, pinned upgrade to 0.5.7.2 with a backup, revert, restart helper
- Tools ([details](https://github.com/Exigentec/modorganizer-borderlands/wiki/Tools-and-Trust))
  - One Tools-menu entry per mod program, checked against Nexus Mods or VirusTotal before it runs
  - Opt-in per-tool tweaks (PhaseLoot)

## Settings

| Plugin | Setting | Default | Purpose |
|---|---|---|---|
| Borderlands Tools | `sdk_support` | on | all Borderlands 4 SDK functions |
| Borderlands Tools | `sdk_update_check` | on | look for a newer SDK release at start-up |
| Borderlands Tools | `virustotal_api_key` | empty | your own free VirusTotal key |
| Borderlands Tools | `tool_support_phaseloot` | on | PhaseLoot tweak |
| each game plugin | `pak_subdir` | `~mods` (BL3), empty (BL4) | folder under `Paks` for loose paks |
| each game plugin | `hide_documentation` | off | hide README/INSTALL-style files on install |

## Documentation

[Home](https://github.com/Exigentec/modorganizer-borderlands/wiki) ·
[Installation](https://github.com/Exigentec/modorganizer-borderlands/wiki/Installation) ·
[Mod placement](https://github.com/Exigentec/modorganizer-borderlands/wiki/Mod-Placement) ·
[OAK2 SDK support](https://github.com/Exigentec/modorganizer-borderlands/wiki/OAK2-SDK-Support) ·
[usvfs swap-out](https://github.com/Exigentec/modorganizer-borderlands/wiki/usvfs-Swapout) ·
[Potential issues](https://github.com/Exigentec/modorganizer-borderlands/wiki/Potential-Issues) ·
[FAQ](https://github.com/Exigentec/modorganizer-borderlands/wiki/FAQ)

## Development

```powershell
python -m unittest discover -s tests -t .
```

The tests stub `mobase` and Qt, so they do not prove the plugin loads inside MO2; anything that touches
MO2's start-up, dialogs or processes has to be checked in a running MO2.
