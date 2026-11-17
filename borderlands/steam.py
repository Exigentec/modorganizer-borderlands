"""Minimal Steam discovery without third-party dependencies (no ``vdf`` package)."""

from __future__ import annotations

import re
from pathlib import Path

_KV = r'"{key}"\s+"([^"]*)"'


def parse_field(text: str, key: str) -> str | None:
    """First value for ``"key"  "value"`` in a Valve KeyValues (.acf/.vdf) text."""
    m = re.search(_KV.format(key=re.escape(key)), text, re.IGNORECASE)
    return m.group(1) if m else None


def library_paths(libraryfolders_text: str) -> list[Path]:
    """Library roots listed in ``steamapps/libraryfolders.vdf`` (new and old format)."""
    paths: list[Path] = []
    # New format: "path"  "C:\\Apps\\Steam"
    for m in re.finditer(_KV.format(key="path"), libraryfolders_text, re.IGNORECASE):
        paths.append(Path(m.group(1).replace("\\\\", "\\")))
    # Old format: "1"  "D:\\SteamLibrary"
    # Value must look like a drive path, otherwise it would also match the new
    # format's "apps" entries ("appid"  "size").
    for m in re.finditer(r'"\d+"\s+"([A-Za-z]:[^"]*)"', libraryfolders_text):
        paths.append(Path(m.group(1).replace("\\\\", "\\")))
    return paths


def find_steam_path() -> Path | None:
    try:
        import winreg
    except ImportError:  # not Windows
        return None
    for hive, subkey, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                return Path(winreg.QueryValueEx(key, value)[0].replace("/", "\\"))
        except OSError:
            continue
    return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def find_install_dir(app_id: str, steam_path: Path | None = None) -> Path | None:
    """Install directory of a Steam app across all libraries, or None if not installed."""
    steam_path = steam_path or find_steam_path()
    if steam_path is None:
        return None
    libraries = [steam_path]
    vdf_text = _read(steam_path / "steamapps" / "libraryfolders.vdf")
    if vdf_text:
        libraries += library_paths(vdf_text)
    seen: set[str] = set()
    for lib in libraries:
        if str(lib).casefold() in seen:
            continue
        seen.add(str(lib).casefold())
        manifest = _read(lib / "steamapps" / f"appmanifest_{app_id}.acf")
        if not manifest:
            continue
        installdir = parse_field(manifest, "installdir")
        if installdir:
            candidate = lib / "steamapps" / "common" / installdir
            if candidate.is_dir():
                return candidate
    return None


def read_build_id(game_dir: Path, app_id: str) -> str | None:
    """Steam build id for an install; used as the game version when the exe has none."""
    manifest = _read(game_dir.parent.parent / f"appmanifest_{app_id}.acf")
    return parse_field(manifest, "buildid") if manifest else None
