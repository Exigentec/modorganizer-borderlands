"""A friendly name for a program, from the metadata compiled into the executable.

Windows keeps a version resource in most executables (for .NET programs it carries the
assembly's title and product).  The name shown is its FileDescription, else its ProductName,
else the file name without ``.exe``.  Blank values count as missing: some programs ship a
FileDescription of a single space.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_LENGTH = 60
_cache: dict[tuple[str, int, int], str] = {}


def _read_version_strings(path: str) -> dict[str, str]:
    """``{"FileDescription": ..., "ProductName": ...}`` from the file's version resource.

    Windows only; returns an empty dict anywhere else or when there is no resource.
    """
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version")
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return {}
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    languages = []
    if version.VerQueryValueW(data, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(length)) \
            and length.value >= 4:
        words = (ctypes.c_ushort * (length.value // 2)).from_address(ptr.value)
        languages = [(words[i], words[i + 1]) for i in range(0, len(words) - 1, 2)]
    found: dict[str, str] = {}
    for lang, codepage in languages or [(0x0409, 0x04B0)]:
        for key in ("FileDescription", "ProductName"):
            query = rf"\StringFileInfo\{lang:04x}{codepage:04x}\{key}"
            if key not in found and version.VerQueryValueW(data, query, ctypes.byref(ptr), ctypes.byref(length)) \
                    and length.value:
                found[key] = ctypes.wstring_at(ptr.value)
    return found


def _clean(value: str | None) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= MAX_LENGTH else text[: MAX_LENGTH - 3].rstrip() + "..."


def display_name(path: Path | str, reader=None) -> str:
    """The program's name; see the module docstring.  Never raises.

    ``reader`` replaces the Windows version-resource reader (tests); results from the real
    reader are cached per file, size and modification time.
    """
    path = Path(path)
    cached = reader is None
    reader = reader or _read_version_strings
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if cached and key is not None and key in _cache:
        return _cache[key]
    name = ""
    try:
        strings = reader(str(path))
        name = next((c for c in (_clean(strings.get("FileDescription")), _clean(strings.get("ProductName"))) if c), "")
    except Exception:
        name = ""
    name = name or path.stem
    if cached and key is not None:
        _cache[key] = name
    return name


def entry_names(named: list[tuple[str, str]]) -> list[str]:
    """Menu labels for ``(program name, mod name)`` pairs: the name, plus the mod when two
    programs would otherwise look the same."""
    counts: dict[str, int] = {}
    for name, _ in named:
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    return [f"{name} ({mod})" if counts[name.casefold()] > 1 else name for name, mod in named]
