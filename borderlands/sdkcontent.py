"""Marks SDK mods and pak mods in MO2's mod list.

MO2's "Flags" column is fixed, but games can supply "content" icons, shown in the mod list's
Content column (hidden by default: right-click the column header to show it).  A mod that holds SDK
mods gets MO2's settings glyph, a mod that holds pak files gets MO2's open "package" box; a mod with
both (a pak mod that also ships an SDK mod) gets both.
"""

from __future__ import annotations

import json
from pathlib import Path

import mobase
from PyQt6.QtCore import Qt, qInfo, qWarning
from PyQt6.QtWidgets import QTreeView

from . import sdk

ICON = ":/MO/gui/settings"  # the glyph of MO2's Settings button
PAK_ICON = ":/MO/gui/package"  # MO2's open cardboard box (a pak is a package); ":/MO/gui/installer" is the
                               # "install a mod from an archive" button's disc-and-box if you prefer that
COL_CONTENT = 3             # ModList::COL_CONTENT: Name, Conflicts, Flags, Content, ... (only the fallback)
CONTENT_TITLE = "content"   # the column's header text (MO2 translates it, so this is tried first, not trusted)


def tree_has_suffix(tree, suffixes) -> bool:
    """Whether an MO2 ``IFileTree`` holds a file with one of ``suffixes`` at any depth."""
    wanted = tuple(s.casefold() for s in suffixes)
    for child in tree:
        if child.isDir():
            if tree_has_suffix(child, wanted):
                return True
        elif child.name().casefold().endswith(wanted):
            return True
    return False


class SdkModContent(mobase.ModDataContent):
    SDK_MOD = 1
    PAK_MOD = 2
    # Names the SDK itself ships under sdk_mods; a mod holding only those is not an "SDK mod".
    base: frozenset[str] = frozenset()

    def __init__(self, sdk: bool = True, pak_suffixes=()):
        super().__init__()
        self._sdk = sdk
        self._pak_suffixes = tuple(pak_suffixes)

    def getAllContents(self):
        contents = []
        if self._sdk:
            contents.append(mobase.ModDataContent.Content(self.SDK_MOD, "SDK mod", ICON))
        if self._pak_suffixes:
            contents.append(mobase.ModDataContent.Content(self.PAK_MOD, "Pak mod", PAK_ICON))
        return contents

    def getContentsFor(self, filetree):
        found = []
        if self._sdk and sdk.tree_has_sdk_mods(filetree, SdkModContent.base):
            found.append(self.SDK_MOD)
        if self._pak_suffixes and tree_has_suffix(filetree, self._pak_suffixes):
            found.append(self.PAK_MOD)
        return found


def set_base(names) -> None:
    SdkModContent.base = frozenset(names)


def content_section(view, header) -> int:
    """The Content column's position: found by its header text, else MO2's usual position (3).

    A newer MO2 could reorder its columns; a localised one has a different header text.  Looking at the
    titles first survives the first, the fallback the second.
    """
    try:
        model = view.model()
        for section in range(header.count()):
            title = model.headerData(section, Qt.Orientation.Horizontal)
            if isinstance(title, str) and title.strip().casefold() == CONTENT_TITLE:
                return section
    except Exception:
        pass
    return COL_CONTENT


def show_column_once(window, marker: Path) -> bool:
    """Un-hide MO2's Content column the first time, so the SDK mods' icon is visible without a hunt.

    Only once (``marker`` records it): after that the column belongs to the user, who may hide it again.
    Returns True if the column was shown now.
    """
    try:
        if json.loads(Path(marker).read_text(encoding="utf-8")).get("content_column_shown"):
            return False
    except (OSError, ValueError, AttributeError):
        pass
    try:
        find = getattr(window, "findChild", None)
        view = find(QTreeView, "modList") if find else None
        header = view.header() if view is not None else None
        if header is None:
            return False
        section = content_section(view, header)
        if header.isSectionHidden(section):
            header.setSectionHidden(section, False)
            qInfo("Borderlands: showed MO2's Content column, where mods that hold SDK mods get an icon "
                  "(right-click the column header to hide it again).")
        Path(marker).parent.mkdir(parents=True, exist_ok=True)
        Path(marker).write_text(json.dumps({"content_column_shown": True}), encoding="utf-8")
        return True
    except Exception as err:
        qWarning(f"Borderlands: could not show the Content column: {err}")
        return False
