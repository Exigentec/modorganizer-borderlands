"""Pure logic for redirecting a game folder's writes into a dedicated overwrite-style mod.

No MO2 or Qt imports.  The MO2 side (mapping, mod list entry, run hooks) lives in
``game.py`` and only calls into this module.

How it works
------------
* MO2 maps the mod's folder over the game folder with "create target", so files the
  game creates or edits there are written into the mod.
* usvfs has no hook for deletions, so they are found by comparing the folder with a
  manifest of what was there last time (before a launch and after the game exits).  A
  vanished file ``x.json`` gets an empty marker ``x.json.deleted`` next to it; a file
  that comes back loses its marker.
* Files already present in the real game folder are copied into the mod first, so
  editing or deleting them acts on the copy and never on the original.  A file with a
  marker is not copied again.

Limit: usvfs cannot hide a real game file, so after a deletion the original reappears
to the game.  The marker records the deletion and stops it being re-seeded.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .spec import Redirect

SKIP_DIRS = frozenset({"__pycache__"})


@dataclass(frozen=True)
class SyncResult:
    target: Path
    seeded: tuple[str, ...] = ()
    marked: tuple[str, ...] = ()
    restored: tuple[str, ...] = ()


def _parts(rel: str) -> tuple[str, ...]:
    return tuple(p for p in rel.replace("\\", "/").split("/") if p)


def game_dir(game_root: Path | str, r: Redirect) -> Path:
    """The real folder inside the game directory that is being redirected."""
    return Path(game_root).joinpath(*_parts(r.game_dir))


def mod_dir(mods_root: Path | str, r: Redirect) -> Path:
    return Path(mods_root) / r.mod_name


def target_dir(mods_root: Path | str, r: Redirect) -> Path:
    """Folder inside the mod that mirrors ``game_dir`` (where writes end up)."""
    return mod_dir(mods_root, r).joinpath(*_parts(r.game_dir))


def is_marker(name: str, r: Redirect) -> bool:
    return name.casefold().endswith(r.deleted_suffix.casefold())


def marker_path(root: Path, rel: str, r: Redirect) -> Path:
    return root / (rel + r.deleted_suffix)


def files_under(root: Path, r: Redirect) -> set[str]:
    """Relative posix paths of real files under ``root``, excluding markers and caches."""
    found: set[str] = set()
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not is_marker(name, r):
                found.add((Path(dirpath) / name).relative_to(root).as_posix())
    return found


# -- manifest -----------------------------------------------------------------


def manifest_path(state_dir: Path, r: Redirect) -> Path:
    return state_dir / f"{r.slug}.manifest.json"


def read_manifest(path: Path) -> set[str] | None:
    """Files recorded last time, or None if there is no usable record."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        return None
    return set(data)


def write_manifest(path: Path, files: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(sorted(files), indent=1), encoding="utf-8")
    os.replace(tmp, path)


# -- the three steps ------------------------------------------------------------


def reconcile(target: Path, previous: set[str] | None, r: Redirect) -> tuple[list[str], list[str]]:
    """Record deletions as markers and clear markers of files that came back.

    Returns ``(marked, restored)``.  With no ``previous`` record nothing can be
    called deleted, so only stale markers are cleared.
    """
    current = files_under(target, r)
    marked: list[str] = []
    for rel in sorted((previous or set()) - current):
        marker = marker_path(target, rel, r)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        marked.append(rel)
    restored: list[str] = []
    for rel in sorted(current):
        marker = marker_path(target, rel, r)
        if marker.exists():
            marker.unlink()
            restored.append(rel)
    return marked, restored


def seed(game_root: Path, mods_root: Path, r: Redirect) -> list[str]:
    """Copy files from the real game folder into the mod; never overwrite or resurrect."""
    if not r.seed_existing:
        return []
    source, target = game_dir(game_root, r), target_dir(mods_root, r)
    copied: list[str] = []
    for rel in sorted(files_under(source, r)):
        dest = target / rel
        if dest.exists() or marker_path(target, rel, r).exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dest)
        copied.append(rel)
    return copied


def sync(r: Redirect, game_root: Path | str, mods_root: Path | str, state_dir: Path | None) -> SyncResult:
    """Before a launch: make the mod folder, mark deletions, seed, refresh the manifest.

    A missing mod folder means a fresh start (or the user reset it), so the old
    manifest is ignored instead of marking every file as deleted.
    """
    game_root, mods_root = Path(game_root), Path(mods_root)
    target = target_dir(mods_root, r)
    fresh = not target.is_dir()
    target.mkdir(parents=True, exist_ok=True)

    manifest = manifest_path(state_dir, r) if state_dir else None
    previous = None if fresh or manifest is None else read_manifest(manifest)
    marked, restored = reconcile(target, previous, r)
    seeded = seed(game_root, mods_root, r)
    if manifest is not None:
        write_manifest(manifest, files_under(target, r))
    return SyncResult(target, tuple(seeded), tuple(marked), tuple(restored))


def after_run(r: Redirect, mods_root: Path | str, state_dir: Path | None) -> SyncResult | None:
    """When the game exits: record what it deleted.  Does nothing without a mod folder."""
    target = target_dir(mods_root, r)
    if state_dir is None or not target.is_dir():
        return None
    manifest = manifest_path(state_dir, r)
    marked, restored = reconcile(target, read_manifest(manifest), r)
    write_manifest(manifest, files_under(target, r))
    return SyncResult(target, (), tuple(marked), tuple(restored))
