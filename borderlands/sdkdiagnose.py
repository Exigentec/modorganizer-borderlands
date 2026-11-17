"""MO2 notifications for the SDK: the same kind as "Overwrite contains files" or the FNIS check.

A plugin of its own, so the tools plugin stays a plain tool plugin (the same shape as MO2's bundled
checkers).  It owns no state: the problems come from the tools plugin's :class:`SdkManager`.
"""

from __future__ import annotations

import mobase
from PyQt6.QtCore import qWarning

from .spec import ALL_SPECS


class SdkDiagnose(mobase.IPluginDiagnose):
    def __init__(self, tools):
        super().__init__()
        self._tools = tools

    def init(self, organizer: mobase.IOrganizer) -> bool:
        return True

    def name(self) -> str:
        return "Borderlands SDK Notifications"

    def localizedName(self) -> str:
        return "Borderlands SDK Notifications"

    def author(self) -> str:
        return "Borderlands MO2 plugin contributors"

    def description(self) -> str:
        return "Warns when SDK mods are installed but the SDK is missing or off, when its files differ, and when an update exists."

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo("0.1.0")

    def requirements(self):
        return [mobase.PluginRequirementFactory.gameDependency({s.game_name for s in ALL_SPECS if s.sdk})]

    def settings(self) -> list[mobase.PluginSetting]:
        return []

    def invalidate(self) -> None:
        """Ask MO2 to ask again which problems are active."""
        try:
            self._invalidate()
        except Exception:
            pass

    def activeProblems(self) -> list[int]:
        try:
            mgr = self._tools.sdk_manager()
            return mgr.problems() if mgr is not None else []
        except Exception as err:
            qWarning(f"Borderlands: could not work out the SDK notifications: {err}")
            return []

    def shortDescription(self, key: int) -> str:
        mgr = self._tools.sdk_manager()
        return mgr.short_description(key) if mgr is not None else ""

    def fullDescription(self, key: int) -> str:
        mgr = self._tools.sdk_manager()
        return mgr.full_description(key) if mgr is not None else ""

    def hasGuidedFix(self, key: int) -> bool:
        return True

    def startGuidedFix(self, key: int) -> None:
        mgr = self._tools.sdk_manager()
        if mgr is not None:
            mgr.fix(key)
