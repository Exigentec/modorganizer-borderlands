"""Mod Organizer 2 plugin adding the Borderlands games.

Copy this folder into ``<MO2>/plugins/``.  MO2 calls :func:`createPlugins` and gets one
game plugin per supported title, a tool plugin for programs that mods ship and for the SDK, and a
notifications plugin for the SDK.
"""

from __future__ import annotations


def createPlugins():
    # Imported lazily so the Qt-free modules (spec, layout, steam) stay importable
    # outside the MO2 process, e.g. from the unit tests.
    from .game import BorderlandsGame
    from .spec import ALL_SPECS
    from .sdkdiagnose import SdkDiagnose
    from .toolsmenu import BorderlandsTools

    tools = BorderlandsTools()
    diagnose = SdkDiagnose(tools)
    tools.diagnose = diagnose
    return [*(BorderlandsGame(spec) for spec in ALL_SPECS), tools, diagnose]
