# Borderlands support for Mod Organizer 2

A Python plugin for Mod Organizer 2 (tested layout: MO2 2.5.2, Python 3.12, PyQt6) that registers
five managed games:

| Game | Steam ID | Executable (relative to game dir) |
|---|---|---|
| Borderlands GOTY Enhanced | 729040 | `Binaries/Win64/BorderlandsGOTY.exe` |
| Borderlands 2 | 49520 | `Binaries/Win32/Borderlands2.exe` (32-bit) |
| Borderlands The Pre-Sequel | 261640 | `Binaries/Win32/BorderlandsPreSequel.exe` (32-bit) |
| Borderlands 3 | 397540 | `OakGame/Binaries/Win64/Borderlands3.exe` |
| Borderlands 4 | 1285190 | `OakGame/Binaries/Win64/Borderlands4.exe` |

## What it does

- Finds each game through the Steam registry key, `libraryfolders.vdf` and `appmanifest_*.acf`.
- Uses the game directory as MO2's data directory, so mods mirror the install
  (`OakGame/...`, `WillowGame/...`, `sdk_mods/...`).
- Validates mods and fixes loose files on install:
  - BL3/BL4: `.pak`, `.utoc`, `.ucas` go to `OakGame/Content/Paks/<pak_subdir>`.
  - BL4: `.sdkmod` goes to `sdk_mods/`.
  - BL2/TPS/GOTY: `.upk` goes to `WillowGame/CookedPCConsole/` (`CookedPC/` for GOTY).
- Borderlands 4: installs, verifies, auto-heals, updates and switches off the unreal SDK from its GitHub
  release (in a pinned "SDK Mod Support" mod plus the real loader files), warns when SDK mods lack it,
  marks SDK mods in the mod list, and keeps usvfs new enough for them (see "The Borderlands 4 SDK" and
  "usvfs").
- Accepts a top-level `Tools/` folder in every game (standalone helper programs shipped as mods,
  for example `Tools/PhaseLoot/PhaseLoot.exe`) and runs them from a Tools-menu entry after checking
  them (see "Running tools").
- Lists `.sav` files from `Documents/My Games/<game>` and copies the main `.ini` files into
  profiles that use local settings.
- Reports the Steam build id as the game version for BL3/BL4, whose executables carry no
  version resource.
- Adds a per-game `pak_subdir` plugin setting (BL3/BL4 only).
- Reads a mod's root `metadata.json` when it is installed. Some mods (for example Ultra Plus)
  ship one for the author's own manager. The plugin sets the mod's MO2 version and URL when
  MO2 has none yet, stores the author, id, config, dependencies, `SettingsSupport` and
  `RelativeLocation` in the mod's `meta.ini` (plugin settings `metadata_*`), and hides the file
  from the VFS. Only files with an `Id` and a `Data` list are touched.

### Where mod content is placed

Archives often wrap their content (`~Mods/<folder>/x.pak`, `MyTool/MyTool.exe`). The plugin works
out where it goes, at the archive root or in a folder you pick with **Set as <game> directory**
in MO2's install dialog. A mod that is already laid out correctly (`OakGame/...`, `sdk_mods/...`,
`Tools/...`) is left alone.

| Found | Placed in |
|---|---|
| a program (`.exe`) at the top level | everything except the game folders, into `Tools/<mod name>/` |
| a folder holding a program, at any depth | the whole folder into `Tools/` (a `.pak` template inside stays with its tool) |
| a `.pak`/`.utoc`/`.ucas` file directly in it (BL3/BL4) | `OakGame/Content/Paks/<pak_subdir>` |
| a subfolder holding paks at any depth (BL3/BL4) | the whole subfolder into `OakGame/Content/Paks/<pak_subdir>/`, structure kept |
| a subfolder named `Paks` | its children into `OakGame/Content/Paks/<pak_subdir>/` (it is the target, not a wrapper) |
| `.sdkmod` files in subfolders (BL4) | flattened into `sdk_mods/` |
| a Python package folder (`__init__.py`) (BL4) | `sdk_mods/<name>/`; `Name/Name/__init__.py` is unwrapped |

A program wins over everything else in the same folder, so PhaseLoot (an exe plus a `.pak`
template) is a tool, not a pak mod. Folders of different kinds side by side are each placed by
their own rule. A folder holding only documentation or pictures (`.txt .md .pdf .rtf .png .jpg
.jpeg .gif .bmp .webp`, or a file named like documentation, with or without a `.txt .ini .md .rtf .pdf .htm .html .nfo .url`
extension: `readme`, `read me first`, `install`, `installing`, `installation`, `instructions`, `setup`, `start`, `startup`,
`where to install`, `changelog`, `changes`, `history`, `what's new`, `license`, `credits`, `notes`, `faq`, `about`, matched on the whole name,
so `setup.exe` is still a program), or nothing, is left where it is instead of making the archive invalid; next
to a game folder it is simply accepted. Only `.exe` programs are recognised (no scripts yet).

Documentation can also be kept out of the game folder, if you turn it on (the game plugin's `hide_documentation`
setting, **off** by default, because it changes the mod's files): after an install, files in the mod's root named like
documentation (`INSTALL.txt`, `CHANGELOG.txt`, `README.md`, `WHERE_TO_INSTALL.txt`, ...) and every file in a top-level
folder that holds only documentation and pictures (`docs`, `Screenshots`) are renamed to `<name>.mohidden`, MO2's own
"hide" (the VFS skips that suffix). The mod keeps them, and the mod's Filetree tab can unhide them. Folders of the game,
tool folders and files that are not named like documentation are never touched. It only applies to mods installed
afterwards.

How it works, because MO2's manual dialog is picky (checked against the
`modorganizer-installer_manual` source): the dialog turns green only for `VALID` content and never
calls the plugin's `fix()`; it installs the files as they are. So for content the plugin knows how
to place, it answers `VALID`, remembers the top-level names, and moves the files on disk right
after the install, from MO2's "mod installed" callback (this is also when the mod name is known,
for `Tools/<mod name>/`). It only moves files when the installed top level matches what the dialog
last accepted. MO2 judges a mod's layout (the "invalid" icon) before it calls that callback, so after
moving files the plugin asks MO2 to refresh once, just after the callback returns; without it the
mod would show as invalid until the next manual refresh. Loose files at the archive root keep the usual `FIXABLE` + `fix()` route. Side
effect: an archive that is only wrappers around placeable content can be taken by the quick
installer without showing the dialog.

Why those choices: UE is expected to mount `Paks` recursively (see "Assumed" below), so paks keep
their folders instead of being flattened. The SDK loader only looks at the direct children of
`sdk_mods` (`*.sdkmod` files and folders with an `__init__.py`, and it rejects folder names
containing a dot), so those cannot stay nested.

It refuses (leaves the dialog red) rather than guess when: a folder is not recognised, identical
pak names appear in different folders (usually alternative variants of one mod, of which only the
last would take effect, so choose the one folder you want; the log pane says which two clash),
one folder holds both paks and `.sdkmod` files, two items would land on the same path, a
destination already exists in the archive, a `.sdkmod` folder holds any other file, or the tree is
very large. MO2's dialog text cannot be customised by a plugin, so the notice goes to the log pane
after the install, for example
`The chosen folder holds mod content in subfolders; placing 6 pak item(s) into OakGame/Content/Paks/.`
A destination that is already taken on disk is left where it is, with a warning.

### Running tools ("Borderlands" in the Tools menu)

A mod's programs live under `Tools/` and are started from the Tools menu. MO2's plugin API cannot
add entries to the executables list, so this menu is the way in. Every program gets an entry of its
own, **Tools > Borderlands: <name>**, that runs it directly. The name is the executable's description from its Windows version resource (for .NET
programs the assembly title), else its product name, else the file name without `.exe`; blank values
are skipped, so PhaseLoot.exe, whose description is a single space, shows as "PhaseLoot". Two
programs with the same name get their mod name added. MO2 only lets a plugin register fixed menu
entries and rebuilds the menu each time it opens, so the plugin adds these entries itself right
after that rebuild: they always match the enabled mods, and a disabled or removed mod's tools have
no entry. **Tools > Borderlands: Choose a tool...** lists them in a dialog instead.

The mod must be enabled: programs from disabled mods are not listed. The program is started from
the mod's own folder, because MO2 cannot launch a file that exists only in the virtual game folder
("file does not exist"); the process is still hooked into the virtual file system, so it sees every
active mod. When two mods provide the same path, only the higher-priority one is listed. After an
install, a dialog lists any programs the mod contains and where to start them.

Each launch writes the checks to MO2's log pane: the source, each ruling returned (Nexus Mods or
VirusTotal, or why one was not consulted), whether you approved the file before, the decision and
the rule behind it, and your answer if you were asked.

Before a program starts, the plugin hashes it (SHA-256) and decides:

| Where it came from | Decision |
|---|---|
| Nexus Mods (`meta.ini` has the mod id) | Looked up against the mod's files on Nexus (below). The VirusTotal count is not consulted. |
| anywhere else | Looked up on VirusTotal if you set a key; otherwise asked once. |

Nexus lookup, using the public key-less GraphQL API and the per-file manifests that the website's
"Preview file contents" box uses (they list every file inside each archive with its SHA-256, so the
extracted exe is checked directly, no downloaded archive needed; a hash from any file or version of
the same mod is accepted):

* **Run**: the mod is `published`, the matching file is not `REMOVED`, and its scan is `VERIFIED`,
  `INTERNALLY_VERIFIED` or `MANUALLY_VERIFIED`.
* **Ask (override allowed)**: the scan is still running (`NOT_SCANNED`, `QUEUED`, `WAITING_REPORT`,
  `PARTIAL`), the hash matches nothing Nexus published (the message says so and warns not to run it
  if you did not change the file yourself), or Nexus could not be reached.
* **Refuse (no override)**: `QUARANTINED`, a mod that is gone or not `published`, a removed file,
  or any other scan status.

VirusTotal (public API v3, your own free key; advisory only, personal use, 4 lookups a minute):

* **Run**: no engine flags it. **Ask**: 1-9 engines with generic labels, or the file is unknown to
  VirusTotal, the lookup failed, or no key is set. **Refuse**: 10 or more engines, or a named
  malware family from a major engine.
* Nothing is ever uploaded. Set the key in **Settings > Plugins > Borderlands Tools >
  virustotal_api_key** (stored as plain text in `ModOrganizer.ini`). At startup a dialog with the
  sign-up links asks for it until it is set; "Don't ask again" silences it.
* A file you approved ("Run it anyway") is remembered by hash in
  `<instance>/plugin_state/borderlands/approved_tools.json` and is not asked about again, unless
  the file changes or a source now refuses it.

### Per-tool tweaks

Some programs get a small, opt-in behaviour of their own, in `borderlands/tweaks.py`. A tweak
matches on the Nexus game and mod id (read from the mod's `meta.ini`, so the mod must have been
installed from Nexus Mods) plus the executable's file name, and has its own switch under
**Settings > Plugins > Borderlands Tools** named `tool_support_<tool>` (on by default).

| Tool | Setting | What it does |
|---|---|---|
| PhaseLoot (Borderlands 4, mod 397, `PhaseLoot.exe`) | `tool_support_phaseloot` | After PhaseLoot exits, if `<mod>/Tools/PhaseLoot/Built PAK/` holds files matching `zzz_Drop_Rarity_Custom_*_P.pak` (any version number), asks whether to move them into `<mod>/OakGame/Content/Paks` (plus the `pak_subdir` setting, empty for Borderlands 4). Other files in that folder are left alone. |

The file is moved, not copied: it is a product of the tool, so nothing is left behind to ask about
again, and a rebuilt pak replaces the previous one (the question says so). Saying no leaves it in
place, and the question comes back after the next run. While a tweaked tool is running, MO2 waits
for it and shows its usual lock screen, as it does for any program it starts. The wait is made
without MO2's own post-run refresh: with it, MO2 waits in a nested event loop for the refresh to
finish, and that wait can stall until some unrelated action in MO2's window triggers another one. A tool with its
tweak switched off just starts. The tool must be set to build into its `Built PAK` folder; the
output location is the tool's own setting, and a pak built anywhere else is left alone.

### The Borderlands 4 SDK

Most Borderlands 4 mods are SDK mods, which need the [unreal SDK](https://github.com/bl-sdk/oak2-mod-manager)
(its loader and Python runtime). The plugin installs and maintains it.

**How it is installed** (found by trying it in MO2 with usvfs 0.5.7.x): the loader and runtime under
`OakGame/Binaries/` must be real files in the game folder. A fully virtual install did not load, and
neither did a real `dsound.dll` alone; a real `OakGame\Binaries\Win64\*` did. Everything else, the SDK's own
mods in `sdk_mods/`, can be virtual. So:

* the **whole release archive** goes into a mod of its own, **SDK Mod Support**. It is handled like
  "SDK Mod Settings" (kept enabled, and put back if moved or deactivated, with a log line), except it is
  pinned at the **lowest** priority, so every other mod can override its files. The plugin owns its
  contents: anything missing or changed is restored without asking;
* the files under `OakGame/Binaries/` are **also copied as real files** into the game folder, and
  removed from there when the SDK is switched off. Those are checked strictly: at startup, **before
  MO2 starts any program**, after the game exits, and when the profile changes. Missing or changed
  ones are put back (the plugin cannot while the game is running, since the files are locked, and
  says so in the log). The check takes about a quarter of a second cold and nothing when warm, because
  hashes are remembered per file size and time.
* The SDK's own mod files are only in the mod. MO2 sees files restored there from its next refresh
  (the plugin asks for one), so a file restored *during* a launch shows from the next launch.

The file list and SHA-256 hashes are learned from a cached copy of the release archive
(`<instance>/plugin_state/borderlands/sdk/oak2-sdk.zip`); no file name or folder is hard-coded.

* **Tools menu** (Borderlands 4): **Borderlands SDK: Set up... / Enable / Disable** (the label follows the
  state) and **Borderlands SDK: Check for updates**.
  * *Set up / Enable* downloads the latest release if there is no cached copy (after asking), fills the
    SDK mod, and writes the real files. Real files that differ from the release are listed and replaced
    only if you agree (this one time; later checks restore them without asking).
  * *Disable* removes the SDK's real files from the game folder, one by one, each only if its hash still
    matches (a file you changed is kept and listed). The SDK mod stays, but without the loader in the
    game folder the SDK does not start, so the game runs stock (for matchmaking, for example).
    *Enable* puts the files back from the cached copy. The game must be closed; the plugin checks.
* **Updates.** At startup, if the SDK is enabled, the plugin asks GitHub for the latest release (one
  request, off the UI thread; switch it off with `sdk_update_check` in Settings > Plugins > Borderlands
  Tools). A newer release is offered: **Update**, **Skip this version** or **Not now**. An update
  downloads the new archive and checks it first (GitHub's published SHA-256, then the archive's own
  structure), and only then removes the old version's files by hash from the game folder and from the
  SDK mod, replaces the cached copy and installs the new files. Nothing is touched if the download fails.
* **Notifications** (the same kind as "Overwrite contains files"), each with a one-click fix: SDK mods
  are enabled but the SDK is not set up or is switched off; the SDK's files are missing or changed and
  could not be put right; a newer release exists; usvfs is too old (see below). Installing an SDK mod
  while the SDK is not going to load it also writes a warning to the log.
* **SDK mod flag.** Mods that contain SDK mods get MO2's settings-button icon in the mod list's
  **Content** column. That column is hidden by default: right-click the list's header to show it.
  The SDK's own files (and the SDK Mod Support mod) are not counted as SDK mods.
* **Safety.** An archive is refused if any entry is absolute, climbs out with `..`, has a drive letter
  or backslash, repeats a name ignoring case, or would replace the game's own executable. Files are
  written through a temporary name that is hashed before it replaces anything, only below the game folder
  or the SDK mod, and only the files being installed. The download address must be on the project's GitHub
  releases and https.

### usvfs (MO2's virtual file system)

SDK mods supplied through MO2 only work with **usvfs 0.5.7.1 or newer**. The SDK's embedded Python 3.14
checks whether a file exists with `GetFileInformationByName` and believes a "not found" answer; usvfs
before 0.5.7.1 does not redirect that call (checked in its source and in the binaries), so the loader
lists your MO2 mods but then cannot find them. MO2 2.5.2 ships usvfs 0.5.6.1.

* **At startup** (Borderlands 4) the plugin reads the version of the four `usvfs_*` files next to
  `ModOrganizer.exe` and of the library MO2 has actually loaded. If it is current it logs
  `MO2 is using usvfs <version>` at info level. If it is older it logs a warning and asks to upgrade
  (**Upgrade usvfs / Not now / Don't ask again**), saying that it is unlikely but that upgrading the
  library could change how MO2 handles other games, since usvfs is shared by every instance.
* **Upgrading** first saves the four current files to `<MO2 folder>\usvfs_versions\usvfs_<version>.zip` (backups an earlier version of the plugin left loose next to `ModOrganizer.exe` are moved there at startup) (logged with the
  new version and the backup path), downloads the official `ModOrganizer2/usvfs` release 0.5.7.2 (the
  archive's SHA-256 and each binary's SHA-256 are pinned in the plugin), unpacks only those four files
  with Windows' own `tar.exe` (always `System32\tar.exe`: the GNU `tar` that Git for Windows or Cygwin put first on
  PATH cannot read 7z), and replaces the loose files. A loaded DLL cannot be overwritten but can
  be renamed, so each old file is moved aside as `<name>.replaced` (deleted at the next start) and the
  swap is rolled back if anything fails. Then it offers to **restart MO2**, by starting a helper that
  waits for MO2 to exit and relaunches the same instance and profile. MO2 itself is closed the way the
  window's X does it (so it still asks about downloads and running programs); if you cancel that, the
  helper gives up after 90 seconds and nothing restarts.
* **SDK mod and pak mod flags**: MO2's Flags column is fixed, so mods that hold SDK mods get an icon (MO2's settings glyph),
  and mods that hold `.pak/.utoc/.ucas` files get MO2's open package box (a mod with both gets both; Borderlands 3 gets
  only the pak icon), in the **Content** column, which MO2 hides by default. With the SDK functions on, the plugin un-hides that column once, finding it by its
  header text (position 3 is only the fallback if no header reads "Content", as in a translated MO2);
  (`plugin_state/borderlands/ui_state.json` records it); hide it again from the header's right-click menu and it stays hidden.
* **Pinned mods are never created over an existing folder**: MO2's `createMod` asks Merge / Replace / Rename when
  the folder exists, and Replace sets the old one aside as `<name>_backup`. The plugin creates a missing pinned
  mod with `createMod` before making any folder, and if the folder is already on disk but MO2 has not listed it
  it refreshes the mod list (once, in the real event loop) and pins the mod it then finds.
* **Restart helper log**: the helper appends each step (waiting, MO2 exited, files removed, started or the
  error) to `<instance>\plugin_state\borderlands\usvfs_restart.log`, and after MO2 has exited it deletes the
  `usvfs_*.replaced` files an upgrade set aside (they cannot be deleted while MO2 still has the DLL loaded).
* **Restart helper**: it is started with `CREATE_NO_WINDOW`, never `DETACHED_PROCESS` (with that, PowerShell
  started and never ran its command, so MO2 closed and nothing reopened it), and the instance and profile
  are passed as one quoted string, because `Start-Process -ArgumentList` would split `Borderlands 4` in two.
* **SDK switch**: the plugin setting `sdk_support` (Tools plugin settings, default **on**) turns every SDK
  function on or off: set-up offer, file healing, launch hooks, notifications, the SDK menu entries and the
  pin of the `SDK Mod Support` mod. The usvfs handling does not depend on it. Restart MO2 after changing it.
* **Start-up timing**: MO2 calls a plugin's start-up callback from inside the main window's constructor,
  before its event loop runs. Every prompt, upgrade and restart therefore waits (`ui.when_running`) until
  the real loop is running, the window is shown and no other dialog is open. Done earlier, a restart's quit
  request is cleared by `exec()` and MO2 is left running with a wait cursor. After a restart has been
  requested, the SDK and VirusTotal prompts are left for the new session.
* **Tools menu**: **Borderlands usvfs: Upgrade to 0.5.7.2** (when the files are older), and for each
  saved backup **Revert to <version> (from backup)** or **Upgrade to <version> (from backup)**. Switching
  saves the current files first, so you can switch back.
* A notification stays until usvfs is new enough and MO2 has been restarted to use it.

### Redirected folders ("SDK Mod Settings")

For Borderlands 4, everything pyunrealsdk mods write under `sdk_mods/settings/`, and everything the
keybinds package writes under `sdk_mods/keybinds/`, goes to one dedicated mod called **SDK Mod Settings**
(in `sdk_mods/settings` and `sdk_mods/keybinds` inside it) instead of MO2's global Overwrite. Each folder
is one `Redirect` entry in `borderlands/spec.py`; add another and it gets the same behaviour with no
other code changes. Give it the same `mod_name` to share the mod (and a different `manifest_key`, so each
folder keeps its own record of what it held), or a new name for a mod of its own. The `keybinds` entry
uses `seed_existing=False`: that folder also holds the keybinds package itself, which must not be copied.
New files the game creates there land in the mod; the package's own files stay where the SDK's mod
supplies them.

- **Created and modified files** are written into the mod through a create-target mapping.
  Files that already exist in the real game folder are copied into the mod first (never
  overwriting), so editing them changes the copy and the originals stay untouched.
  Turn this off per redirect with `seed_existing=False`.
- **Deleted files** cannot be intercepted by usvfs. The plugin compares the mod folder with a
  manifest before each launch and after the game exits, and for each vanished file `x.json`
  touches an empty `x.json.deleted` next to it. A file that reappears loses its marker, and a
  marked file is not copied in again. The manifest lives in
  `<instance>/plugin_state/borderlands/`, outside every mod.
- **Resetting:** delete the "SDK Mod Settings" mod folder; the next launch starts fresh and
  re-copies the originals.
- **Mod list:** the plugin API cannot restyle a mod row or create a real Overwrite, so this is
  an ordinary mod. The plugin keeps it enabled and at the highest priority (just under
  Overwrite) by undoing changes right after they happen: unchecking it re-checks it, and
  dragging it, or dragging another mod past it, puts it back last. The user sees it snap back;
  the plugin cannot veto the drag itself, hide its priority number, hide conflict icons or
  change its row style. Each undone action writes a warning to MO2's log pane, for example
  `'SDK Mod Settings' cannot be moved. It stays last, directly before Overwrite ...`,
  `'SDK Mod Settings' cannot be deactivated ...` or
  `'<mod>' cannot be placed below 'SDK Mod Settings' ...`. Changes the plugin makes itself at
  startup are not logged.
- **Installs:** MO2 adds a newly installed mod at the end of the list, which would put it below
  the redirect mod. The plugin re-pins right in MO2's "mod installed" callback and once more
  after the next mod list refresh (in case the new mod only gets its priority after the
  callback), and logs an informational line, for example
  `'SDK Mod Settings' was kept last, directly before Overwrite, after installing '<mod>'.`
  It also re-pins just before every launch. It does not register an installer plugin to hook
  MO2's installation manager directly: that would put a second plugin in the install path. (The "Overwrite contains files" warning comes from MO2's own diagnosis
  of the real Overwrite folder, so this mod does not trigger it.)

Limit: usvfs cannot hide a real game file, so after a deletion the original (if the real game
folder still has it) is visible to the game again. The marker records the deletion and keeps
the file from being re-copied.

### How hiding works

This is MO2's own mechanism, not a custom one. MO2's "Hide" action renames a file to
`<name>.mohidden`; usvfs then skips any file whose name ends with a suffix listed in
`[Settings] skip_file_suffixes` in `ModOrganizer.ini` (default `.mohidden`) when it links a mod
into the game directory. The plugin does the same rename. If a user has removed `.mohidden` from
that setting, the plugin leaves the file in place and logs a warning rather than pretend it is
hidden. (MO2 also strips a top-level `meta.ini`, `readme.txt` and `fomod` from its own Data-tab
tree, but that does not affect what usvfs links, so it is not a mechanism for plugins.)

## Install

```powershell
.\scripts\Install-Plugin.ps1 -Mo2Path C:\Apps\MO2
```

This copies `borderlands/` into `<MO2>/plugins/`. Restart MO2, create or switch an instance and pick the
game.

## Tests

```powershell
python -m unittest discover -s tests -t .
```

The tests cover the layout rules, Steam discovery and the glue code. They stub `mobase` and Qt, so
they do **not** prove the plugin loads inside MO2.

## Verified vs assumed

Verified on nexusmods.com (2026-11-16, signed in): domain names and numeric game ids.

| Game | Nexus domain | Nexus game id |
|---|---|---|
| Borderlands GOTY Enhanced | `borderlandsgotyenhanced` | 2776 |
| Borderlands 2 | `borderlands2` | 428 |
| Borderlands The Pre-Sequel | `borderlandspresequel` | 875 |
| Borderlands 3 | `borderlands3` | 2953 |
| Borderlands 4 | `borderlands4` | 8148 |

Verified on disk: all paths, executables and bitness above, the Steam IDs, `Documents/My Games`
folder names, ini file names, BL4's `sdk_mods` layout (`.sdkmod` is a zip of a Python package) and
BL4's pak mods sitting directly in `Content/Paks`.

Assumed, please check:

- **BL3 `~mods`**: the BL3 install has no mod paks, so the `~mods` default is the Unreal
  convention. Change the `pak_subdir` setting if your mods expect something else.
- **BL2/TPS SDK files**: `Binaries/` is accepted as a valid mod folder, but no SDK-specific
  placement is implemented because none of those SDKs is installed here to inspect.
- **Paks in subfolders**: stock Unreal searches the `Paks` folder recursively, which is why
  `~mods` works. No Borderlands install here has a pak in a subfolder, so this is not confirmed
  for BL4 (UE5). If a mod placed in a subfolder does not load, set `pak_subdir` or move its
  paks up by hand.
- **Pak load order**: paks are not renamed by MO2 priority. Unreal orders paks by file name, so
  rely on the mod's own naming (`..._P`, numeric prefixes).
- **`metadata.json` schema**: it has no published specification. Field meanings are inferred from
  the Ultra Plus file, whose Nexus page names the Ultra+ Manager as its consumer. Other mods may
  use different keys; those files are simply left alone.
- Epic/GOG installs are not detected (Steam only).
- **SDK management**: the release lookup, download, install into the mod and the real folder, verify,
  heal, switch off and on were run live against the real v0.3 release in throwaway folders; the loaded-DLL
  rename the usvfs upgrade relies on was exercised on a real loaded DLL. The pinned mod, menu entries,
  dialogs, notifications, the Content-column flag and the restart helper have only been run against
  stand-ins for MO2's API. The notifications are a plugin of their own (the shape of MO2's bundled
  checkers) so a problem there cannot stop the tools menu from loading.
- **usvfs upgrade**: pinned to 0.5.7.2, the release you tested; pairing it with MO2 2.5.2 is untested by
  the plugin. The 0.5.7.1 threshold comes from usvfs's history (the hook was added in that release).
- **Tool checks**: the Nexus endpoints and the VirusTotal lookup were exercised live, but the menu,
  the dialogs and the install-time file moves have only been run against stand-ins for MO2's API.
  Nexus's meaning of `QUARANTINED` (held for manual review) is the user's description, not
  documented; other `Mod.status` values than `published` are from memory of the older API.
