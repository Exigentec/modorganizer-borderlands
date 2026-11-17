"""Per-tool tweaks: small, opt-in behaviours for specific programs that mods ship.

A tweak matches a program by its Nexus mod id (and Nexus game) plus the executable's file
name, and is switched on or off by a ``tool_support_<key>`` setting of the Borderlands Tools
plugin.  Pure file handling, no MO2 or Qt, so it is testable on its own.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .tools import NexusLink

SETTING_PREFIX = "tool_support_"


@dataclass(frozen=True)
class ToolTweak:
    key: str          # the setting is ``tool_support_<key>``
    title: str        # how the tool is named in messages
    game_name: str    # Nexus domain of the mod, e.g. "borderlands4"
    mod_id: int       # Nexus mod id
    exe_name: str     # the program's file name
    description: str  # shown next to the setting
    # After the program exits: the file(s) it builds, relative to the mod folder (``*`` may be
    # used in the file name), and where they are offered to be moved to, relative to the mod folder.
    built_file: str = ""
    move_to: str = ""

    @property
    def setting(self) -> str:
        return SETTING_PREFIX + self.key


PHASELOOT = ToolTweak(
    key="phaseloot",
    title="PhaseLoot",
    game_name="borderlands4",
    mod_id=397,
    exe_name="PhaseLoot.exe",
    description=(
        "PhaseLoot: after it closes, offer to move the PAK it built (Tools/PhaseLoot/Built PAK/"
        "zzz_Drop_Rarity_Custom_*_P.pak) into the mod's OakGame/Content/Paks folder."
    ),
    built_file="Tools/PhaseLoot/Built PAK/zzz_Drop_Rarity_Custom_*_P.pak",
    move_to="OakGame/Content/Paks",
)

TWEAKS: tuple[ToolTweak, ...] = (PHASELOOT,)


def match(link: NexusLink | None, exe_name: str, tweaks: tuple[ToolTweak, ...] = TWEAKS) -> ToolTweak | None:
    """The tweak for a program, or None.  Needs a Nexus link: the mod id is what identifies it."""
    if link is None:
        return None
    return next((t for t in tweaks
                 if t.mod_id == link.mod_id and t.game_name == link.game_name
                 and t.exe_name.casefold() == exe_name.casefold()), None)


def find_built(mod_dir: Path, tweak: ToolTweak) -> list[Path]:
    """The files the tool built (those matching ``built_file``), sorted by name."""
    if not tweak.built_file:
        return []
    *folders, pattern = tweak.built_file.split("/")
    folder = Path(mod_dir).joinpath(*folders)
    try:
        return sorted(p for p in folder.glob(pattern) if p.is_file())
    except OSError:
        return []


def destination(mod_dir: Path, tweak: ToolTweak, pak_subdir: str = "") -> Path:
    """Where the built file goes: ``move_to`` (plus the pak sub-folder setting for paks)."""
    parts = [p for p in tweak.move_to.split("/") if p]
    if parts[-2:] == ["Content", "Paks"]:
        parts += [p for p in pak_subdir.replace("\\", "/").split("/") if p]
    return Path(mod_dir).joinpath(*parts)


def move(src: Path, dest_dir: Path) -> tuple[Path, bool]:
    """Move ``src`` into ``dest_dir`` (made if missing), replacing a file of the same name.

    A move, not a copy: the built file is a product, so nothing is left behind to ask about
    again.  Returns ``(new path, whether an existing file was replaced)``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    replaced = dest.exists()
    if replaced and dest.is_dir():
        raise IsADirectoryError(f"{dest} is a folder")
    try:
        os.replace(src, dest)  # atomic, and replaces an existing file
    except OSError:  # another drive: copy, then remove the original
        shutil.copy2(src, dest)
        src.unlink()
    return dest, replaced


def prompt_text(tweak: ToolTweak, names: list[str], shown: str, replacing: list[str]) -> str:
    """The question asked after a tool built ``names``: move them into ``shown``?"""
    one = len(names) == 1
    it, them = ("it", "it") if one else ("them", "them")
    lines = [f"{tweak.title} built:", "", *[f"    {n}" for n in names], "",
             f"Move {it} into this mod's {shown} folder so the game loads {them}?", ""]
    lines.append("It is moved, not copied: it is a product of the tool, so it will not be offered again."
                 if one else
                 "They are moved, not copied: they are products of the tool, so they will not be offered again.")
    if replacing:
        lines += ["", f"This replaces {', '.join(replacing)}, which {'is' if len(replacing) == 1 else 'are'} "
                      "already there."]
    return "\n".join(lines)
