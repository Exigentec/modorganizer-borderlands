"""MO2 glue: a generic ``IPluginGame`` driven by a :class:`GameSpec`."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import mobase
from PyQt6.QtCore import QDateTime, QDir, QFileInfo, QStandardPaths, QTimer, qInfo, qWarning
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from . import layout, modmeta, redirects, relocate, steam, tools
from .spec import GameSpec, Redirect

PAK_SUBDIR_SETTING = "pak_subdir"


class BorderlandsSaveGame(mobase.ISaveGame):
    def __init__(self, filepath: Path):
        super().__init__()
        self._filepath = filepath

    def getFilepath(self) -> str:
        return self._filepath.as_posix()

    def getName(self) -> str:
        return self._filepath.name

    def getCreationTime(self):
        return QDateTime.fromSecsSinceEpoch(int(self._filepath.stat().st_mtime))

    def getSaveGroupIdentifier(self) -> str:
        return ""

    def allFiles(self) -> list[str]:
        return [self.getFilepath()]


class BorderlandsModDataChecker(mobase.ModDataChecker):
    """Validates mod archives and relocates loose paks / .sdkmod / .upk on install."""

    def __init__(self, spec: GameSpec, pak_subdir):
        super().__init__()
        self._spec = spec
        self._pak_subdir = pak_subdir  # callable -> str, read lazily from settings
        self._last_refusal = ""
        self._pending: frozenset[str] | None = None  # see take_pending()

    def _nested_plan(self, filetree: mobase.IFileTree) -> layout.NestedPlan | None:
        """Plan for content held in subfolders (or a program at the top level).

        Applies to the archive root and to a folder the user chose with "Set as <game>
        directory" alike; a folder nobody recognises is refused, so a mod that is already
        laid out correctly never gets here.
        """
        if not self._spec.nested_rules:
            return None
        nodes = layout.nodes_from_tree(filetree)
        if nodes is None:
            return None
        plan, reason = layout.plan_nested_why(self._spec, nodes, self._pak_subdir())
        if plan is None:
            # MO2 re-checks on every tree change, so say it once per distinct reason.
            if reason and reason != self._last_refusal:
                qWarning(f"Borderlands: not placing '{filetree.name() or 'the archive'}'. {reason}")
            self._last_refusal = reason
            return None
        self._last_refusal = ""
        # A destination that already exists in the archive would collide when the content
        # is placed, and we do not overwrite: "do not guess".
        for action in plan.actions:
            if filetree.find((action.target + action.source[-1]).rstrip("/")) is not None:
                return None
        return plan

    def dataLooksValid(self, filetree: mobase.IFileTree) -> mobase.ModDataChecker.CheckReturn:
        """Verdict for a mod's top level.

        MO2's manual install dialog is green only for VALID and never calls ``fix()``, so
        content we know how to place is reported VALID: a program or folders of paks,
        ``.sdkmod`` files and programs at the archive root or in a chosen folder ("Set as
        <game> directory"; it has a parent, the archive root does not), and loose files in a
        chosen folder.  The files are moved on disk right after the install
        (:meth:`take_pending`, :mod:`relocate`).  Loose files at the archive root stay
        FIXABLE, which the quick installer handles through :meth:`fix`.
        """
        entries = [(e.name(), e.isDir()) for e in filetree]
        verdict = layout.check(self._spec, entries)
        chosen = filetree.parent() is not None
        has_exe = any(not is_dir and name.casefold().endswith(".exe") for name, is_dir in entries)
        placeable = False
        if (verdict is layout.Verdict.INVALID or has_exe) and self._nested_plan(filetree) is not None:
            placeable = True
        elif chosen and verdict is layout.Verdict.FIXABLE:
            placeable = True
        if placeable:
            self._pending = relocate.signature(name for name, _ in entries)
            return mobase.ModDataChecker.VALID
        if chosen:
            self._pending = None
        if verdict is layout.Verdict.FIXABLE:
            return mobase.ModDataChecker.FIXABLE
        if verdict is layout.Verdict.VALID:
            return mobase.ModDataChecker.VALID
        return mobase.ModDataChecker.INVALID

    def take_pending(self) -> frozenset[str] | None:
        """Top-level names of the last chosen folder accepted for placement, once."""
        pending, self._pending = self._pending, None
        return pending

    def fix(self, filetree: mobase.IFileTree) -> mobase.IFileTree:
        """Quick installer path (archive root): move loose files into their game folders."""
        pak_subdir = self._pak_subdir()
        loose = 0
        for entry in list(filetree):
            target = layout.move_target(self._spec, entry.name(), entry.isDir(), pak_subdir)
            if target is None:
                continue
            if filetree.move(entry, target) is False:
                qWarning(f"Borderlands: could not place '{entry.name()}' into {target}: that name is already taken there.")
                continue
            loose += 1
        if loose:
            qInfo(f"Borderlands: placed {loose} loose file(s) in the game folders they belong in.")
        return filetree


class BorderlandsGame(mobase.IPluginGame, mobase.IPluginFileMapper):
    """One instance per supported game; all behaviour comes from ``spec``."""

    def __init__(self, spec: GameSpec):
        mobase.IPluginGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)
        self._spec = spec
        self._gamePath = ""
        self._enforcing = False  # True while we change the mod list ourselves
        self._refresh_pending = False  # a post-install check is already scheduled
        self._organizer: mobase.IOrganizer

    # -- helpers ---------------------------------------------------------------

    def _pak_subdir(self) -> str:
        default = self._spec.default_pak_subdir or ""
        try:
            value = self._organizer.pluginSetting(self.name(), PAK_SUBDIR_SETTING)
        except Exception:
            return default
        return value if isinstance(value, str) else default

    def _hide_is_effective(self) -> bool:
        """True unless the instance's ini says ``.mohidden`` is no longer skipped."""
        ini = Path(self._organizer.basePath()) / "ModOrganizer.ini"
        try:
            return modmeta.hidden_suffix_is_skipped(ini.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return True  # no ini to read: MO2 is on its defaults

    # -- IPlugin ---------------------------------------------------------------

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        self._checker = BorderlandsModDataChecker(self._spec, self._pak_subdir)
        organizer.gameFeatures().registerFeature(self, self._checker, 0, True)
        organizer.modList().onModInstalled(self._on_mod_installed)
        if self._spec.redirects:
            # Keep the redirect mods present and last, then record deletions on exit.
            organizer.onUserInterfaceInitialized(lambda _window: self._sync_redirect_mods())
            organizer.onProfileChanged(lambda _old, _new: self._sync_redirect_mods())
            organizer.onAboutToRun(self._before_run)
            organizer.onFinishedRun(lambda _app, _code: self._after_run())
            # Hooks only run after a change, so "locked like Overwrite" is enforced by
            # undoing the change right away (the user sees it snap back).
            organizer.modList().onModMoved(self._on_mod_moved)
            organizer.modList().onModStateChanged(self._on_mod_state_changed)
        return True

    # -- redirects: IPluginFileMapper and mod list entry -----------------------

    def _redirect_state_dir(self) -> Path | None:
        """Per-instance folder (outside every mod, so never in the VFS) for manifests."""
        try:
            return Path(self._organizer.basePath()) / "plugin_state" / "borderlands"
        except Exception:
            return None

    def mappings(self) -> list[mobase.Mapping]:
        """One create-target mapping per redirect: writes go to its mod, not Overwrite."""
        if not self._spec.redirects or not self.isActive():
            return []
        result: list[mobase.Mapping] = []
        mods = Path(self._organizer.modsPath())
        for r in self._spec.redirects:
            try:
                synced = redirects.sync(r, self._gamePath, mods, self._redirect_state_dir())
            except OSError as err:
                print(f"Borderlands: could not prepare '{r.mod_name}': {err}", file=sys.stderr)
                continue
            if synced.seeded or synced.marked or synced.restored:
                print(
                    f"Borderlands: '{r.mod_name}' seeded {len(synced.seeded)}, marked "
                    f"{len(synced.marked)} deleted, restored {len(synced.restored)}.",
                    file=sys.stderr,
                )
            result.append(
                mobase.Mapping(str(synced.target), str(redirects.game_dir(self._gamePath, r)), True, True)
            )
        return result

    def _ensure_redirect_mod(self, r: Redirect) -> None:
        """Make sure the redirect's mod exists in the list, is enabled and sits last.

        The plugin API cannot restyle a mod row or turn it into a real Overwrite, so it
        is an ordinary mod kept at the highest priority (just under Overwrite).
        """
        mod_list = self._organizer.modList()
        if mod_list.getMod(r.mod_name) is None:
            # Only when absent: createMod would otherwise prompt the user to merge.
            if self._organizer.createMod(mobase.GuessedString(r.mod_name)) is None:
                print(f"Borderlands: could not create mod '{r.mod_name}'.", file=sys.stderr)
                return
        self._enforce_redirect_mod(r)

    def _enforce_redirect_mod(self, r: Redirect) -> tuple[bool, bool]:
        """Re-enable the mod and put it last (just under Overwrite) if it is not.

        Returns ``(re_enabled, re_pinned)`` so callers can tell the user what was undone.
        Our own corrections fire the move/state hooks again, hence the guard.
        """
        if self._enforcing:
            return False, False
        mod_list = self._organizer.modList()
        if mod_list.getMod(r.mod_name) is None:
            return False, False
        re_enabled = re_pinned = False
        self._enforcing = True
        try:
            if not mod_list.state(r.mod_name) & mobase.ModState.ACTIVE:
                mod_list.setActive(r.mod_name, True)
                re_enabled = True
            names = mod_list.allModsByProfilePriority()
            ranked = [n for n in names if n.casefold() != "overwrite"]
            if ranked[-1:] != [r.mod_name]:
                mod_list.setPriority(r.mod_name, len(names))  # past the end means "last"
                re_pinned = True
        finally:
            self._enforcing = False
        return re_enabled, re_pinned

    def _on_mod_moved(self, moved: str = "", *_args) -> None:
        """Any move may have displaced a redirect mod from last place."""
        if self._enforcing or not self.isActive():
            return
        for r in self._spec.redirects:
            try:
                _, re_pinned = self._enforce_redirect_mod(r)
            except Exception as err:
                print(f"Borderlands: could not re-pin '{r.mod_name}': {err}", file=sys.stderr)
                continue
            if re_pinned:
                if moved == r.mod_name:
                    cause = f"'{r.mod_name}' cannot be moved"
                elif moved:
                    cause = f"'{moved}' cannot be placed below '{r.mod_name}'"
                else:
                    cause = f"'{r.mod_name}' cannot be displaced"
                qWarning(
                    f"Borderlands: {cause}. It stays last, directly before Overwrite, so it "
                    "receives the writes redirected to it; it was moved back."
                )

    def _on_mod_state_changed(self, states) -> None:
        """Turn a redirect mod back on if the user (or anything) deactivated it."""
        if self._enforcing or not self.isActive():
            return
        for r in self._spec.redirects:
            state = states.get(r.mod_name) if hasattr(states, "get") else None
            if state is None or state & mobase.ModState.ACTIVE:
                continue
            try:
                re_enabled, _ = self._enforce_redirect_mod(r)
            except Exception as err:
                print(f"Borderlands: could not re-enable '{r.mod_name}': {err}", file=sys.stderr)
                continue
            if re_enabled:
                qWarning(
                    f"Borderlands: '{r.mod_name}' cannot be deactivated. It receives the "
                    f"files written under {r.game_dir}/, so it was re-enabled."
                )

    def _sync_redirect_mods(self) -> None:
        if not self.isActive():
            return
        for r in self._spec.redirects:
            try:
                redirects.target_dir(self._organizer.modsPath(), r).mkdir(parents=True, exist_ok=True)
                self._ensure_redirect_mod(r)
            except Exception as err:
                print(f"Borderlands: could not set up '{r.mod_name}': {err}", file=sys.stderr)

    def _before_run(self, _app: str) -> bool:
        self._sync_redirect_mods()
        return True  # never block a launch

    def _after_run(self) -> None:
        if not self.isActive():
            return
        for r in self._spec.redirects:
            try:
                done = redirects.after_run(r, self._organizer.modsPath(), self._redirect_state_dir())
            except OSError as err:
                print(f"Borderlands: could not record deletions for '{r.mod_name}': {err}", file=sys.stderr)
                continue
            if done and (done.marked or done.restored):
                print(
                    f"Borderlands: '{r.mod_name}' marked {len(done.marked)} deleted, "
                    f"restored {len(done.restored)}.",
                    file=sys.stderr,
                )

    def _on_mod_installed(self, mod: mobase.IModInterface) -> None:
        """Runs after every install: keep the redirect mods last, then read the metadata."""
        if not self.isActive():  # every game plugin gets this callback
            return
        self._relocate_content(mod)
        self._announce_programs(mod)
        if self._spec.redirects:
            self._repin_redirects(f"installing '{mod.name()}'")
            self._repin_after_next_refresh()
        self._apply_metadata(mod)

    def _relocate_content(self, mod: mobase.IModInterface) -> None:
        """Place content the install dialog accepted (see the checker's docstring).

        Only acts when the installed top level is the one the checker accepted last.
        Never raises: a failed move must not break the install.
        """
        pending = self._checker.take_pending()
        if pending is None:
            return
        try:
            mod_dir = Path(mod.absolutePath())
            if relocate.top_level_signature(mod_dir) != pending:
                return
            for level, text in relocate.relocate(self._spec, mod_dir, self._pak_subdir(), mod.name()):
                (qWarning if level == "warning" else qInfo)(text)
        except Exception as err:
            qWarning(f"Borderlands: could not place the content of '{mod.name()}': {err}")

    def _announce_programs(self, mod: mobase.IModInterface) -> None:
        """Tell the user that an installed mod ships programs, and where to start them.

        Shown once the install callback has returned, so it cannot hold up MO2's install.
        """
        try:
            found = tools.discover([(mod.name(), Path(mod.absolutePath()))])
            if not found:
                return
            shown = [f"    {t.label}" for t in found[:10]]
            if len(found) > 10:
                shown.append(f"    ... and {len(found) - 10} more")
            text = (
                f"'{mod.name()}' contains {len(found)} program(s):\n\n" + "\n".join(shown) + "\n\n"
                "Enable the mod first: programs from disabled mods are not listed. "
                "They are not started automatically. To run one, open the Tools menu and pick its "
                "'Borderlands: <name>' entry. It is checked first (against Nexus Mods, or "
                "VirusTotal if the mod did not come from Nexus Mods)."
            )
            QTimer.singleShot(0, lambda: QMessageBox.information(QApplication.activeWindow(), "Borderlands tools", text))
        except Exception as err:
            qWarning(f"Borderlands: could not list the programs of '{mod.name()}': {err}")

    def _repin_redirects(self, reason: str) -> None:
        """Put the redirect mods back last if something (an install) displaced them."""
        for r in self._spec.redirects:
            try:
                _, re_pinned = self._enforce_redirect_mod(r)
            except Exception as err:
                print(f"Borderlands: could not re-pin '{r.mod_name}': {err}", file=sys.stderr)
                continue
            if re_pinned:
                qInfo(f"Borderlands: '{r.mod_name}' was kept last, directly before Overwrite, after {reason}.")

    def _repin_after_next_refresh(self) -> None:
        """A new mod may only get its priority after the install callback has run.

        So look once more after MO2's next mod list refresh (one callback at a time).
        """
        if self._refresh_pending:
            return
        self._refresh_pending = True
        try:
            self._organizer.onNextRefresh(self._on_next_refresh, immediate_if_possible=False)
        except Exception as err:
            self._refresh_pending = False
            print(f"Borderlands: could not schedule the post-install check: {err}", file=sys.stderr)

    def _on_next_refresh(self) -> None:
        self._refresh_pending = False
        if self.isActive():
            self._repin_redirects("the mod list refresh that followed an install")

    def _apply_metadata(self, mod: mobase.IModInterface) -> None:
        """Feed a mod's own ``metadata.json`` into MO2, then hide it from the VFS.

        Never raises: a bad descriptor must not break an otherwise good install.
        """
        try:
            path = modmeta.find(Path(mod.absolutePath()))
            meta = modmeta.read(path) if path else None
            if meta is None:
                return
            # Prefer what MO2 already knows (e.g. from Nexus) over the descriptor.
            if meta.version and not mod.version().isValid():
                mod.setVersion(mobase.VersionInfo(meta.version))
            if meta.url and not mod.url():
                mod.setUrl(meta.url)
            # MO2 has no author/dependency fields; keep them in the mod's meta.ini.
            for key, value in (
                ("author", meta.author),
                ("id", meta.id),
                ("config", meta.config),
                ("dependencies", ", ".join(meta.dependencies)),
                ("settings_support", meta.settings_support),
                ("relative_location", meta.relative_location),
            ):
                if value:
                    mod.setPluginSetting(self.name(), f"metadata_{key}", value)
            if self._hide_is_effective():
                modmeta.hide(path)  # metadata.json -> metadata.json.mohidden
            else:
                print(
                    "Borderlands: MO2's skip_file_suffixes no longer lists .mohidden, so "
                    f"{path.name} in '{mod.name()}' was left unhidden.",
                    file=sys.stderr,
                )
        except Exception as err:
            print(f"Borderlands: could not process metadata.json for {mod.name()}: {err}", file=sys.stderr)

    def name(self) -> str:
        return self._spec.plugin_name

    def author(self) -> str:
        return "Borderlands MO2 plugin contributors"

    def description(self) -> str:
        return self._spec.description

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo("0.1.0")

    def isActive(self) -> bool:
        managed = self._organizer.managedGame()
        return bool(managed) and self.name() == managed.name()

    def settings(self) -> list[mobase.PluginSetting]:
        if self._spec.default_pak_subdir is None:
            return []
        return [
            mobase.PluginSetting(
                PAK_SUBDIR_SETTING,
                "Folder under OakGame/Content/Paks that loose .pak/.utoc/.ucas files "
                "in a mod are moved into when the mod is installed. Leave empty to use "
                "Paks itself.",
                self._spec.default_pak_subdir,
            )
        ]

    # -- IPluginGame: identity and discovery ----------------------------------

    def detectGame(self):
        found = steam.find_install_dir(self._spec.steam_id)
        if found is not None:
            self.setGamePath(found)

    def gameName(self) -> str:
        return self._spec.game_name

    def gameShortName(self) -> str:
        return self._spec.short_name

    def gameIcon(self) -> QIcon:
        return mobase.getIconForExecutable(self.gameDirectory().absoluteFilePath(self.binaryName()))

    def validShortNames(self) -> list[str]:
        return []

    def gameNexusName(self) -> str:
        return self._spec.nexus_name

    def nexusModOrganizerID(self) -> int:
        return 0

    def nexusGameID(self) -> int:
        return self._spec.nexus_id

    def steamAPPId(self) -> str:
        return self._spec.steam_id

    def gogAPPId(self) -> str:
        return ""

    def epicAPPId(self) -> str:
        return ""

    def eaDesktopContentId(self) -> str:
        return ""

    def getSupportURL(self) -> str:
        return ""

    def binaryName(self) -> str:
        return self._spec.binary

    def getLauncherName(self) -> str:
        return ""

    def looksValid(self, directory: QDir) -> bool:
        return directory.exists(self._spec.binary)

    def isInstalled(self) -> bool:
        return bool(self._gamePath)

    def gameVersion(self) -> str:
        exe = self.gameDirectory().absoluteFilePath(self._spec.binary)
        version = mobase.getFileVersion(exe)
        if version:
            return version
        # BL3/BL4 executables carry no version resource; fall back to the Steam build id.
        build = steam.read_build_id(Path(self._gamePath), self._spec.steam_id)
        return f"build {build}" if build else ""

    def setGameVariant(self, variant: str) -> None:
        pass

    # -- IPluginGame: paths ----------------------------------------------------

    def gameDirectory(self) -> QDir:
        return QDir(self._gamePath)

    def dataDirectory(self) -> QDir:
        # Mods mirror the game directory (OakGame/..., WillowGame/..., sdk_mods/...).
        return QDir(self._gamePath)

    def setGamePath(self, path) -> None:
        self._gamePath = str(path)

    def documentsDirectory(self) -> QDir:
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        return QDir(f"{docs}/My Games/{self._spec.docs_folder}")

    def savesDirectory(self) -> QDir:
        return self.documentsDirectory()

    # -- IPluginGame: profile, executables, saves ------------------------------

    def iniFiles(self) -> list[str]:
        return list(self._spec.ini_files)

    def initializeProfile(self, directory: QDir, settings: mobase.ProfileSetting) -> None:
        if settings & mobase.ProfileSetting.CONFIGURATION:
            for ini in self.iniFiles():
                dest = directory.absoluteFilePath(QFileInfo(ini).fileName())
                try:
                    shutil.copyfile(self.documentsDirectory().absoluteFilePath(ini), dest)
                except FileNotFoundError:
                    Path(dest).touch()

    def executables(self) -> list[mobase.ExecutableInfo]:
        def info(title: str, rel: str) -> mobase.ExecutableInfo:
            return mobase.ExecutableInfo(title, QFileInfo(self.gameDirectory().absoluteFilePath(rel)))

        execs = [info(self._spec.game_name, self._spec.binary)]
        for title, rel in self._spec.extra_executables:
            if self.gameDirectory().exists(rel):
                execs.append(info(title, rel))
        return execs

    def executableForcedLoads(self) -> list[mobase.ExecutableForcedLoadSetting]:
        return []

    def listSaves(self, folder: QDir) -> list[mobase.ISaveGame]:
        # Layouts differ (BL2: SaveData/<id>/, BL3: Saved/SaveGames/<id>/, BL4: .../Profiles/client/).
        try:
            return [BorderlandsSaveGame(p) for p in Path(folder.absolutePath()).glob("**/*.sav")]
        except OSError as err:
            print(f"Borderlands: could not list saves in {folder.absolutePath()}: {err}", file=sys.stderr)
            return []
