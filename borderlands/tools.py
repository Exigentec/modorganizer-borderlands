"""Find the programs that mods ship under ``Tools/`` and read where a mod came from.

Pure file handling, no MO2 or Qt, so it is testable on its own.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .spec import TOOLS_DIR

MAX_FILES_SCANNED = 5000  # per mod, a guard against a huge tree


@dataclass(frozen=True)
class Tool:
    mod: str          # mod name in MO2
    mod_path: Path    # the mod's folder
    rel: str          # path below the game folder, e.g. "Tools/PhaseLoot/PhaseLoot.exe"
    real: Path        # the file inside the mod folder

    @property
    def label(self) -> str:
        return self.rel.split("/", 1)[1] if "/" in self.rel else self.rel


def discover(mods: Iterable[tuple[str, Path]]) -> list[Tool]:
    """Programs under ``Tools/`` of the given mods, ``mods`` ordered lowest priority first.

    Mods are layered by priority, so when two mods provide the same path only the later
    (higher-priority) one is what the game folder shows, and only that one is returned.
    """
    found: dict[str, Tool] = {}
    for name, path in mods:
        tools_dir = next((p for p in _children(path) if p.is_dir() and p.name.casefold() == TOOLS_DIR), None)
        if tools_dir is None:
            continue
        scanned = 0
        for root, _dirs, files in os.walk(tools_dir):
            for file in files:
                scanned += 1
                if scanned > MAX_FILES_SCANNED:
                    break
                if not file.casefold().endswith(".exe"):
                    continue
                real = Path(root) / file
                rel = "Tools/" + real.relative_to(tools_dir).as_posix()
                found[rel.casefold()] = Tool(name, Path(path), rel, real)
    return sorted(found.values(), key=lambda t: (t.rel.casefold(), t.mod.casefold()))


def _children(path: Path):
    try:
        return list(Path(path).iterdir())
    except OSError:
        return []


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class NexusLink:
    mod_id: int
    game_name: str  # Nexus domain, e.g. "borderlands4"


def read_nexus_link(mod_path: Path) -> NexusLink | None:
    """The Nexus mod a folder was installed from, from MO2's own ``meta.ini``; None if none."""
    try:
        text = (Path(mod_path) / "meta.ini").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    mod_id = re.search(r"(?m)^modid=(\d+)\s*$", text)
    repo = re.search(r"(?m)^repository=(.*?)\s*$", text)
    game = re.search(r"(?m)^gameName=(.*?)\s*$", text)
    if not mod_id or int(mod_id.group(1)) <= 0 or not repo or repo.group(1).casefold() != "nexus" or not game:
        return None
    return NexusLink(int(mod_id.group(1)), game.group(1).casefold())
