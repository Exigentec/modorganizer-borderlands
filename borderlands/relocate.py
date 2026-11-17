"""Move the content of an installed mod into the game folders it belongs in.

MO2's manual install dialog only accepts ``VALID`` content and never calls ``fix()``; it
hands the chosen folder's files to the installer as they are.  So content the plugin
accepted from a chosen folder (loose paks, ``~Mods/<folder>/x.pak`` ...) is placed here,
on disk, right after the install.  The decisions come from :mod:`layout`; this module only
does the file moves.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from . import layout
from .spec import GameSpec

# MO2 writes its own meta.ini into every mod folder.
_IGNORED = {"meta.ini"}

Message = tuple[str, str]  # ("info" | "warning", text)


def signature(names) -> frozenset[str]:
    """What identifies a mod's top level: its entry names (case-folded) without MO2's own."""
    return frozenset(n.casefold() for n in names if n.casefold() not in _IGNORED)


def top_level_signature(mod_dir: Path) -> frozenset[str]:
    try:
        return signature(os.listdir(mod_dir))
    except OSError:
        return frozenset()


def nodes_from_dir(path: Path, limit: int = 20000) -> list[layout.Node] | None:
    """Copy a folder's contents into ``Node``s; None if it is too big to scan."""
    budget = [limit]

    def walk(p: Path) -> layout.Node:
        budget[0] -= 1
        if budget[0] < 0:
            raise layout.TooLarge
        if p.is_dir() and not p.is_symlink():
            return layout.Node(p.name, True, tuple(walk(c) for c in sorted(p.iterdir())))
        return layout.Node(p.name, False)

    try:
        return [walk(c) for c in sorted(path.iterdir()) if c.name.casefold() not in _IGNORED]
    except layout.TooLarge:
        return None


def _has_files(path: Path) -> bool:
    return any(not p.is_dir() or p.is_symlink() for p in path.rglob("*"))


def _place(src: Path, dest_dir: Path, shown: str, target: str, out: list[Message]) -> bool:
    dest = dest_dir / src.name
    if dest.exists():
        out.append(("warning", f"Borderlands: could not place '{shown}' into {target}: that name is already taken there."))
        return False
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return True


def safe_folder_name(name: str) -> str:
    """A mod name as a single Windows-safe folder name."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Tool"


def docs_to_hide(spec: GameSpec, mod_dir: Path, hidden_suffix: str) -> list[Path]:
    """Documentation a mod leaves in its root, which has no business in the game folder.

    * loose top-level files named like documentation (``README.txt``, ``INSTALL``, ``CHANGELOG.txt``,
      ``WHERE_TO_INSTALL.txt`` ...), and
    * every file in a top-level folder that holds nothing but documentation and pictures (``docs``,
      ``Screenshots``).

    The game's own folders, tool folders and anything else are never looked into, and files that are
    already hidden are skipped.
    """
    found: list[Path] = []
    try:
        entries = sorted(mod_dir.iterdir())
    except OSError:
        return found
    for entry in entries:
        name = entry.name
        if name.casefold() in _IGNORED or name.casefold().endswith(hidden_suffix.casefold()):
            continue
        if entry.is_symlink():
            continue
        if entry.is_file():
            if layout.is_doc_name(name):
                found.append(entry)
        elif entry.is_dir() and name.casefold() not in spec.mod_dirs:
            nodes = nodes_from_dir(entry)
            if nodes is None:
                continue
            files = [p for p in entry.rglob("*") if p.is_file() and not p.is_symlink()
                     and not p.name.casefold().endswith(hidden_suffix.casefold())]
            if files and all(layout.is_doc(p.name) for p in files):
                found.extend(files)
    return found


def relocate(spec: GameSpec, mod_dir: Path, pak_subdir: str, mod_name: str = "") -> list[Message]:
    """Place nested content and loose files of ``mod_dir`` where the game expects them.

    ``mod_name`` names the ``Tools/<mod name>/`` folder when the mod has a program at its
    top level; it defaults to the mod folder's own name (MO2 uses the mod name there).
    """
    out: list[Message] = []

    nodes = nodes_from_dir(mod_dir)
    plan = layout.plan_nested_why(spec, nodes, pak_subdir)[0] if nodes is not None else None
    if plan is not None:
        placed = 0
        for action in plan.actions:
            src = mod_dir.joinpath(*action.source)
            dest_dir = mod_dir.joinpath(*[p for p in action.target.split("/") if p])
            if src.exists() and _place(src, dest_dir, "/".join(action.source), action.target, out):
                placed += 1
        if plan.tool_folder:
            dest_dir = mod_dir / "Tools" / safe_folder_name(mod_name or mod_dir.name)
            for name in plan.tool_folder:
                src = mod_dir / name
                if src.exists() and _place(src, dest_dir, name, f"Tools/{dest_dir.name}/", out):
                    placed += 1
        for name in plan.source_dirs:  # drop what is left of the folders we emptied
            left = mod_dir / name
            if left.is_dir() and not _has_files(left):
                shutil.rmtree(left, ignore_errors=True)
        if placed:
            out.append(("info", "Borderlands: " + plan.describe()))

    loose = 0
    for entry in sorted(mod_dir.iterdir()):
        if entry.is_dir() or entry.name.casefold() in _IGNORED:
            continue
        target = layout.move_target(spec, entry.name, False, pak_subdir)
        if target is None:
            continue
        dest_dir = mod_dir.joinpath(*[p for p in target.split("/") if p])
        if _place(entry, dest_dir, entry.name, target, out):
            loose += 1
    if loose:
        out.append(("info", f"Borderlands: placed {loose} loose file(s) in the game folders they belong in."))
    return out
