"""Static, Qt-free description of every supported Borderlands game.

Everything that differs between the games lives here as data, so the MO2 glue in
``game.py`` stays generic.  Facts below were taken from the install directories on
disk (see README "Verified vs assumed").
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Redirect:
    """Send everything written under one game folder to a dedicated overwrite-style mod.

    Files created or changed under ``game_dir`` land in the mod ``mod_name`` instead of
    MO2's global Overwrite.  Deletions are recorded as ``<file><deleted_suffix>`` markers
    in that mod.  Define one per folder; ``GameSpec.redirects`` lists them.
    """

    mod_name: str
    # Relative to the game root, forward slashes.
    game_dir: str
    # Copy files that already exist in the real game folder into the mod (never
    # overwriting), so edits to them are captured instead of changing the originals.
    seed_existing: bool = True
    deleted_suffix: str = ".deleted"

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", self.mod_name).strip("_").lower()


@dataclass(frozen=True)
class MoveRule:
    """Loose files at a mod's top level that belong somewhere else in the game dir.

    ``target`` is relative to the game root, uses forward slashes and may contain
    ``{pak_subdir}``, which is replaced by the per-game "pak_subdir" plugin setting.
    """

    suffixes: tuple[str, ...]
    target: str


@dataclass(frozen=True)
class NestedRule:
    """Content that may sit in subfolders of the folder the user picked as the game directory.

    Used only when the user (or MO2) chooses a sub-folder with "Set as <game> directory".
    ``mode`` says how matching content is placed:

    * ``"folder"``: each top-level subfolder holding files with ``suffixes`` (at any depth)
      moves whole into ``target``, keeping its structure.  A top-level folder named
      ``anchor`` *is* the target, so its children move instead.
    * ``"flatten"``: each file with ``suffixes`` moves straight into ``target``.  Only
      done when the subfolder holds nothing else and no file names collide.
    * ``"package"``: each top-level Python package folder (it has an ``__init__.py``, or
      is ``Name/Name/__init__.py`` double nested) moves into ``target``.

    ``exclusive``: a folder with matching files is handled by this rule alone; other rules
    are not asked (a program's folder may hold a ``.pak`` template that is not game content).
    ``variants``: identically named files in sibling folders are alternative variants of one
    mod, so the whole selection is refused instead of mounting all of them.
    """

    name: str
    target: str
    mode: str
    suffixes: tuple[str, ...] = ()
    anchor: str = ""
    exclusive: bool = False
    variants: bool = False


@dataclass(frozen=True)
class GameSpec:
    plugin_name: str
    game_name: str
    short_name: str
    nexus_name: str
    nexus_id: int
    steam_id: str
    # Relative to the game directory.
    binary: str
    # (title, relative path) pairs offered as extra executables in MO2.
    extra_executables: tuple[tuple[str, str], ...]
    # Folder name under "<Documents>/My Games".
    docs_folder: str
    # Relative to the documents folder.
    ini_files: tuple[str, ...]
    # Lower-case top-level folders that are valid inside a mod (relative to game root).
    mod_dirs: frozenset[str]
    move_rules: tuple[MoveRule, ...]
    # None: game has no loose-pak convention, so no "pak_subdir" setting is offered.
    default_pak_subdir: str | None = None
    description: str = ""
    redirects: tuple[Redirect, ...] = ()
    nested_rules: tuple[NestedRule, ...] = ()


# Settings written by pyunrealsdk mods.  Add siblings here later, e.g.
# Redirect("SDK Keybinds", "sdk_mods/keybinds", seed_existing=False): that folder also
# holds the keybinds package itself, which should not be copied.
SDK_MOD_SETTINGS = Redirect("SDK Mod Settings", "sdk_mods/settings")

# Standalone helper programs shipped as mods ("Tools/<name>/<name>.exe"), valid for every game.
TOOLS_DIR = "tools"
_WILLOW_DIRS = frozenset({"willowgame", "binaries", "engine", "dlc", TOOLS_DIR})
_WILLOW_INIS = (
    "WillowGame/Config/WillowEngine.ini",
    "WillowGame/Config/WillowGame.ini",
    "WillowGame/Config/WillowInput.ini",
)
_PAK_SUFFIXES = (".pak", ".utoc", ".ucas")

# UE paks mount recursively, so a mod's own subfolders are kept as they are (they often
# hold identically named paks, e.g. one folder per language).
PAK_NESTED = NestedRule(
    "pak", "OakGame/Content/Paks/{pak_subdir}", "folder", suffixes=_PAK_SUFFIXES, anchor="Paks",
    variants=True,
)
# A folder holding a program (.exe) is a tool: it moves whole into Tools/, whatever else it holds.
TOOL_NESTED = NestedRule("program", "Tools/", "folder", suffixes=(".exe",), exclusive=True)
# The pyunrealsdk loader only looks at the direct children of sdk_mods: *.sdkmod files and
# folders with an __init__.py, so these cannot stay nested.
SDK_NESTED = (
    NestedRule("sdk mod", "sdk_mods/", "flatten", suffixes=(".sdkmod",)),
    NestedRule("sdk package", "sdk_mods/", "package"),
)

BORDERLANDS_GOTY_ENHANCED = GameSpec(
    plugin_name="Borderlands GOTY Enhanced Support Plugin",
    game_name="Borderlands GOTY Enhanced",
    short_name="borderlandsgotyenhanced",
    nexus_name="borderlandsgotyenhanced",
    nexus_id=2776,
    steam_id="729040",
    binary="Binaries/Win64/BorderlandsGOTY.exe",
    extra_executables=(("Borderlands GOTY Enhanced Launcher", "Binaries/Win64/Launcher.exe"),),
    docs_folder="Borderlands Game of the Year",
    ini_files=_WILLOW_INIS,
    mod_dirs=_WILLOW_DIRS,
    move_rules=(MoveRule((".upk",), "WillowGame/CookedPC/"),),
    nested_rules=(TOOL_NESTED,),
    description="Adds support for Borderlands: Game of the Year Edition Enhanced (Steam).",
)

BORDERLANDS_2 = GameSpec(
    plugin_name="Borderlands 2 Support Plugin",
    game_name="Borderlands 2",
    short_name="borderlands2",
    nexus_name="borderlands2",
    nexus_id=428,
    steam_id="49520",
    binary="Binaries/Win32/Borderlands2.exe",
    extra_executables=(("Borderlands 2 Launcher", "Binaries/Win32/Launcher.exe"),),
    docs_folder="Borderlands 2",
    ini_files=_WILLOW_INIS,
    mod_dirs=_WILLOW_DIRS,
    move_rules=(MoveRule((".upk",), "WillowGame/CookedPCConsole/"),),
    nested_rules=(TOOL_NESTED,),
    description="Adds support for Borderlands 2 (Steam).",
)

BORDERLANDS_PRE_SEQUEL = GameSpec(
    plugin_name="Borderlands The Pre-Sequel Support Plugin",
    game_name="Borderlands The Pre-Sequel",
    short_name="borderlandspresequel",
    nexus_name="borderlandspresequel",
    nexus_id=875,
    steam_id="261640",
    binary="Binaries/Win32/BorderlandsPreSequel.exe",
    extra_executables=(
        ("Borderlands The Pre-Sequel Launcher", "Binaries/Win32/Launcher.exe"),
    ),
    docs_folder="Borderlands The Pre-Sequel",
    ini_files=_WILLOW_INIS,
    mod_dirs=_WILLOW_DIRS,
    move_rules=(MoveRule((".upk",), "WillowGame/CookedPCConsole/"),),
    nested_rules=(TOOL_NESTED,),
    description="Adds support for Borderlands: The Pre-Sequel (Steam).",
)

BORDERLANDS_3 = GameSpec(
    plugin_name="Borderlands 3 Support Plugin",
    game_name="Borderlands 3",
    short_name="borderlands3",
    nexus_name="borderlands3",
    nexus_id=2953,
    steam_id="397540",
    binary="OakGame/Binaries/Win64/Borderlands3.exe",
    extra_executables=(),
    docs_folder="Borderlands 3",
    ini_files=(
        "Saved/Config/WindowsNoEditor/Engine.ini",
        "Saved/Config/WindowsNoEditor/GameUserSettings.ini",
        "Saved/Config/WindowsNoEditor/Input.ini",
    ),
    mod_dirs=frozenset({"oakgame", "engine", TOOLS_DIR}),
    move_rules=(MoveRule(_PAK_SUFFIXES, "OakGame/Content/Paks/{pak_subdir}"),),
    # Unreal convention; not verified against this install (it has no mod paks).
    default_pak_subdir="~mods",
    description="Adds support for Borderlands 3 (Steam).",
    nested_rules=(PAK_NESTED, TOOL_NESTED),
)

BORDERLANDS_4 = GameSpec(
    plugin_name="Borderlands 4 Support Plugin",
    game_name="Borderlands 4",
    short_name="borderlands4",
    nexus_name="borderlands4",
    nexus_id=8148,
    steam_id="1285190",
    binary="OakGame/Binaries/Win64/Borderlands4.exe",
    # The 566 KB root-level exe is the Steam launch stub; the real game is the 647 MB one.
    extra_executables=(("Borderlands 4 (Steam stub)", "Borderlands4.exe"),),
    docs_folder="Borderlands 4",
    ini_files=(
        "Saved/Config/Windows/Engine.ini",
        "Saved/Config/Windows/GameUserSettings.ini",
        "Saved/Config/Windows/Input.ini",
    ),
    mod_dirs=frozenset({"oakgame", "engine", "sdk_mods", TOOLS_DIR}),
    move_rules=(
        MoveRule(_PAK_SUFFIXES, "OakGame/Content/Paks/{pak_subdir}"),
        # pyunrealsdk mods: zip archives of a Python package (see sdk_mods/ on disk).
        MoveRule((".sdkmod",), "sdk_mods/"),
    ),
    # Existing BL4 mods on disk sit directly in Content/Paks, not in a subfolder.
    default_pak_subdir="",
    description="Adds support for Borderlands 4 (Steam), including pyunrealsdk .sdkmod mods.",
    redirects=(SDK_MOD_SETTINGS,),
    nested_rules=(PAK_NESTED, *SDK_NESTED, TOOL_NESTED),
)

ALL_SPECS = (
    BORDERLANDS_GOTY_ENHANCED,
    BORDERLANDS_2,
    BORDERLANDS_PRE_SEQUEL,
    BORDERLANDS_3,
    BORDERLANDS_4,
)
