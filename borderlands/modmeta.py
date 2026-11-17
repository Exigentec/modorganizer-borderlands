"""Reader for the ``metadata.json`` descriptor some Borderlands mods ship at their root.

The file is consumed by the author's own manager (Ultra+ Manager for the Ultra Plus
mod) and has no published schema, so the field meanings below are inferred from a real
file.  Example::

    {"Name": "UltraPlus", "Id": "ultraplus-Borderlands4", "Author": "...",
     "Version": "0.2.7", "Config": "UltraPlusConfig.ini", "Dependencies": [],
     "Permalink": "", "WikiLink": "", "SettingsSupport": "false",
     "RelativeLocation": "", "Data": [{"file": "OakGame/Content/Paks/x.pak"}]}

Pure Python (no MO2 / Qt) so it can be unit-tested outside MO2.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

FILENAME = "metadata.json"
HIDDEN_SUFFIX = ".mohidden"  # MO2's own "hide file" convention; the VFS skips these
MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class ModMeta:
    name: str
    id: str
    author: str
    version: str
    config: str
    permalink: str
    wiki_link: str
    dependencies: tuple[str, ...]
    files: tuple[str, ...]
    # "true"/"false": whether the author's manager can configure this mod for the game.
    # The author's site says Borderlands 4 has no custom settings, matching "false".
    settings_support: str = ""
    # Prefix the author's manager applies to ``Data`` paths; empty for Borderlands 4.
    relative_location: str = ""

    @property
    def url(self) -> str:
        return self.permalink or self.wiki_link


def _text(fields: dict, key: str) -> str:
    value = fields.get(key)
    return value.strip() if isinstance(value, str) else ""


def parse(text: str) -> ModMeta | None:
    """Parse descriptor text; None unless it looks like this descriptor.

    A file only counts when it has an ``Id`` and a ``Data`` list, so unrelated
    ``metadata.json`` files are never touched.  Keys are matched case-insensitively.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fields = {str(k).casefold(): v for k, v in data.items()}
    if not _text(fields, "id") or not isinstance(fields.get("data"), list):
        return None

    deps: list[str] = []
    for item in fields.get("dependencies") or []:
        if isinstance(item, str) and item.strip():
            deps.append(item.strip())
        elif isinstance(item, dict):
            label = item.get("Id") or item.get("id") or item.get("Name") or item.get("name")
            if isinstance(label, str) and label.strip():
                deps.append(label.strip())
    files = tuple(
        item["file"] for item in fields["data"] if isinstance(item, dict) and isinstance(item.get("file"), str)
    )
    return ModMeta(
        name=_text(fields, "name"),
        id=_text(fields, "id"),
        author=_text(fields, "author"),
        version=_text(fields, "version"),
        config=_text(fields, "config"),
        permalink=_text(fields, "permalink"),
        wiki_link=_text(fields, "wikilink"),
        dependencies=tuple(deps),
        files=files,
        settings_support=_text(fields, "settingssupport"),
        relative_location=_text(fields, "relativelocation"),
    )


def find(mod_dir: Path) -> Path | None:
    """The mod's top-level descriptor file (case-insensitive), or None."""
    try:
        for name in os.listdir(mod_dir):
            if name.casefold() == FILENAME:
                path = Path(mod_dir) / name
                return path if path.is_file() else None
    except OSError:
        return None
    return None


def read(path: Path) -> ModMeta | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return None


def hidden_suffix_is_skipped(ini_text: str) -> bool:
    """Whether MO2's ``skip_file_suffixes`` setting still covers ``.mohidden``.

    MO2's "Hide" action only renames a file to ``<name>.mohidden``; what keeps it out
    of the VFS is usvfs skipping that suffix, driven by ``[Settings] skip_file_suffixes``
    in ModOrganizer.ini (default ``.mohidden``).  If a user removed it, a hidden file
    would be mapped into the game directory again.
    """
    m = re.search(r"^skip_file_suffixes[ \t]*=[ \t]*(.*?)[ \t]*$", ini_text, re.MULTILINE)
    if m is None:
        return True  # key absent -> MO2 uses its default, which includes .mohidden
    value = m.group(1)
    if value.startswith("@"):
        return True  # QSettings binary encoding we cannot read; assume the default
    suffixes = {part.strip().strip('"').casefold() for part in value.split(",")}
    return HIDDEN_SUFFIX in suffixes


def hide(path: Path) -> Path:
    """Rename ``path`` to ``<name>.mohidden`` so MO2 leaves it out of the VFS."""
    hidden = path.with_name(path.name + HIDDEN_SUFFIX)
    os.replace(path, hidden)
    return hidden
