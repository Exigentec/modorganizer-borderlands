"""MO2-facing handling of usvfs: check it at startup, upgrade it, revert it, offer a restart.

SDK mods that are supplied through MO2 (the virtual file system) only work with usvfs 0.5.7.1 or
newer; see :mod:`usvfs` for why and for the file handling.  MO2 2.5.2 ships 0.5.6.1.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, qInfo, qWarning
from PyQt6.QtWidgets import QApplication

from . import netio, ui, usvfs

TITLE = "Borderlands usvfs"


class _State:
    def __init__(self, path: Path):
        self._path = Path(path)
        self.declined = False
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            self.declined = bool(isinstance(loaded, dict) and loaded.get("declined"))
        except (OSError, ValueError):
            pass

    def decline(self) -> None:
        self.declined = True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"declined": True}), encoding="utf-8")


class UsvfsManager:
    def __init__(self, organizer, parent, *, root: Path | None = None, on_change=lambda: None,
                 status_fn=None, fetch_fn=usvfs.fetch, download=netio.download, restart_fn=None,
                 reader=usvfs.file_version):
        self._organizer = organizer
        self._parent = parent            # callable -> the main window, or None
        self._root = Path(root) if root is not None else Path(QCoreApplication.applicationDirPath())
        self._on_change = on_change
        self._reader = reader
        self._status_fn = status_fn or (lambda: usvfs.status(self._root, reader))
        self._fetch, self._download = fetch_fn, download
        self._restart = restart_fn or self._default_restart
        self.restarting = False          # set once a restart of MO2 has been requested
        self.state = _State(Path(organizer.basePath()) / "plugin_state" / "borderlands" / "usvfs_state.json")

    @property
    def root(self) -> Path:
        return self._root

    def status(self) -> usvfs.Status:
        return self._status_fn()

    def _log_note(self, message: str) -> None:
        qInfo(f"Borderlands: {message}")

    # -- the check at startup -------------------------------------------------------------

    def startup(self) -> None:
        """Log what usvfs MO2 is using and, if it is too old, say so and offer to upgrade."""
        leftovers = usvfs.cleanup_replaced(self._root)
        if leftovers:
            self._log_note(f"removed {leftovers} old usvfs file(s) left by an earlier upgrade")
        try:
            moved = usvfs.migrate_backups(self._root)
        except OSError:
            moved = []
        if moved:
            self._log_note(f"moved {len(moved)} usvfs backup(s) into {self._root / usvfs.BACKUP_DIR}")
        status = self.status()
        if status.disk is None:
            qWarning("Borderlands: could not read the version of the usvfs files next to ModOrganizer.exe.")
            return
        if status.needs_upgrade:
            qWarning(
                f"Borderlands: usvfs {status.disk} is older than {usvfs.FIXED_IN}, so SDK mods that MO2 supplies "
                f"cannot be found by the SDK (its Python checks files with a call this usvfs does not redirect). "
                f"usvfs {usvfs.TARGET.version} fixes that: Tools > Borderlands usvfs: Upgrade."
            )
            if not self.state.declined:
                self._guard("upgrade offer", lambda: self.offer_upgrade(startup=True))
        elif status.pending_restart:
            qWarning(
                f"Borderlands: usvfs {status.disk} is installed next to ModOrganizer.exe, but this MO2 is still "
                f"running {status.loaded}. Restart MO2 to use the new one."
            )
        else:
            self._log_note(f"MO2 is using usvfs {status.loaded or status.disk}, which redirects the calls the SDK needs.")

    # -- dialogs --------------------------------------------------------------------------

    def _say(self, kind: str, text: str) -> None:
        ui.say(self._parent(), kind, TITLE, text)

    def _ask(self, text: str, buttons: list[str], default: int = 0) -> str:
        return ui.ask(self._parent(), TITLE, text, buttons, default)

    def _guard(self, label: str, work) -> None:
        try:
            work()
        except Exception as err:  # a dialog action must never take MO2 down with it
            qWarning(f"Borderlands: usvfs {label} failed: {err}")
            self._say("critical", f"The usvfs {label} failed: {err}")

    # -- upgrading ------------------------------------------------------------------------

    def offer_upgrade(self, startup: bool = False) -> None:
        status = self.status()
        release = usvfs.TARGET
        backup_path = self._root / usvfs.BACKUP_DIR / f"usvfs_{status.disk or 'unknown'}.zip"
        text = (
            f"MO2's virtual file system (usvfs) is version {status.disk}. SDK mods that MO2 supplies need "
            f"{usvfs.FIXED_IN} or newer: with this one the SDK's mod loader cannot see them.\n\n"
            f"Upgrade to usvfs {release.version} (the official release from github.com/ModOrganizer2/usvfs, "
            "about 26 MB)? The four current files are first saved to\n"
            f"    {backup_path}\n"
            "and can be restored from the Tools menu.\n\n"
            "It is unlikely, but upgrading this library could change how MO2 handles your other games too: "
            "usvfs is shared by every MO2 instance on this install.\n\n"
            "MO2 must be restarted afterwards to use it."
        )
        choices = ["Upgrade usvfs", "Not now", "Don't ask again"] if startup else ["Upgrade usvfs", "Cancel"]
        answer = self._ask(text, choices, default=1)
        if answer == "Upgrade usvfs":
            self.upgrade()
        elif answer == "Don't ask again":
            self.state.decline()
            self._on_change()

    def upgrade(self, release: usvfs.Release = usvfs.TARGET) -> None:
        self._guard("upgrade", lambda: self._upgrade(release))

    def _upgrade(self, release: usvfs.Release) -> None:
        old = self.status().disk or "unknown"
        backup = usvfs.backup(self._root, old)  # first: nothing is touched until the old files are saved
        qWarning(
            f"Borderlands: usvfs {old} is older than {usvfs.FIXED_IN}; upgrading it to {release.version}. The current "
            f"four files are saved in {backup}."
        )
        workdir = usvfs.new_workdir()
        try:  # the download folder holds a 26 MB archive: removed whatever happens
            progress = ui.Progress(self._parent(), TITLE, f"Downloading usvfs {release.version}...")
            try:
                files = self._fetch(release, workdir, self._download, progress.update)
            except netio.Cancelled:
                return
            except (netio.NetworkError, netio.ChecksumError, ValueError, RuntimeError) as err:
                self._say("critical", f"usvfs was not upgraded, and nothing was changed: {err}")
                return
            finally:
                progress.close()
            try:
                usvfs.replace_files(self._root, files)
            except OSError as err:
                self._say("critical", f"usvfs could not be replaced, and the old files were put back: {err}")
                return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        self._log_note(f"usvfs {release.version} replaced {old} next to ModOrganizer.exe (backup: {backup}); "
                       "MO2 uses it after a restart")
        self._on_change()
        self._offer_restart(f"usvfs {release.version} is installed.")

    # -- reverting / switching ------------------------------------------------------------

    def restore(self, backup: usvfs.Backup) -> None:
        self._guard("revert", lambda: self._restore(backup))

    def _restore(self, backup: usvfs.Backup) -> None:
        current = self.status().disk or "unknown"
        newer = (usvfs.version_key(backup.version) or ()) > (usvfs.version_key(current) or ())
        if self._ask(
            f"{'Upgrade' if newer else 'Revert'} usvfs from {current} to {backup.version}, using the saved files in "
            f"{backup.path.name}?\n\nThe current four files are saved first, so you can switch back. MO2 must be "
            "restarted afterwards.", ["Switch", "Cancel"], default=1,
        ) != "Switch":
            return
        saved = usvfs.backup(self._root, current)
        workdir = usvfs.new_workdir()
        try:
            files = usvfs.extract_backup(backup, workdir / "files", self._reader)
            usvfs.replace_files(self._root, files)
        except (ValueError, OSError) as err:
            self._say("critical", f"usvfs was not switched, and nothing was changed: {err}")
            return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        qWarning(
            f"Borderlands: usvfs was {current}; it is now {backup.version} (from {backup.path.name}). The previous "
            f"files are saved in {saved}. MO2 uses it after a restart."
        )
        self._on_change()
        self._offer_restart(f"usvfs {backup.version} is installed.")

    # -- restarting -----------------------------------------------------------------------

    def _offer_restart(self, lead: str) -> None:
        if self._ask(f"{lead} Restart Mod Organizer 2 now to use it?", ["Restart now", "Later"]) == "Restart now":
            self.restart()

    def restart(self) -> None:
        self._guard("restart", self._restart)

    def _default_restart(self) -> None:
        exe = Path(QCoreApplication.applicationFilePath())
        base = Path(self._organizer.basePath())
        instance = None if base.resolve() == self._root.resolve() else base.name  # a portable instance needs no -i
        try:
            profile = self._organizer.profile().name()
        except Exception:
            profile = None
        log = base / "plugin_state" / "borderlands" / "usvfs_restart.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            log = None
        usvfs.spawn_restart(usvfs.restart_command(exe, os.getpid(), instance, profile, log))
        self.restarting = True
        self._log_note("restarting Mod Organizer 2 to use the new usvfs"
                       + (f" (the helper writes {log})" if log else ""))
        # close the way a click on the window's X does (MO2 then asks about downloads and running programs),
        # but only from the real event loop: see ui.when_running
        ui.when_running(self._close, self._parent)

    def _close(self) -> None:
        window = self._parent()
        (window.close if window is not None else QApplication.quit)()

    # -- notifications and menu -----------------------------------------------------------

    def problem_text(self) -> tuple[str, str] | None:
        """``(short, long)`` description of the usvfs problem, or None if there is none."""
        status = self.status()
        if status.needs_upgrade:
            return (
                f"usvfs {status.disk} is too old for SDK mods supplied through MO2",
                f"MO2's usvfs is {status.disk}. SDK mods that MO2 supplies need {usvfs.FIXED_IN} or newer, otherwise "
                f"the SDK's loader cannot see them. The fix upgrades it to {usvfs.TARGET.version} (after asking), "
                "saving the old files first.",
            )
        if status.pending_restart:
            return (
                f"Restart MO2 to use usvfs {status.disk}",
                f"usvfs {status.disk} is installed, but this MO2 is still running {status.loaded}. Restart MO2.",
            )
        return None

    def fix(self) -> None:
        if self.status().needs_upgrade:
            self.offer_upgrade()
        else:
            self._offer_restart("usvfs was already upgraded.")

    def menu_entries(self) -> list[tuple[str, str, object]]:
        """Upgrade / revert entries for the Tools menu, as they apply right now."""
        status = self.status()
        current = usvfs.version_key(status.disk)
        target = usvfs.version_key(usvfs.TARGET.version)
        backups = usvfs.list_backups(self._root)
        entries: list[tuple[str, str, object]] = []
        has_target_backup = any(usvfs.version_key(b.version) == target for b in backups)
        if current is not None and current < target and not has_target_backup:
            entries.append((f"Borderlands usvfs: Upgrade to {usvfs.TARGET.version}",
                            "Download the official usvfs release (saving the current files first)",
                            lambda: self.offer_upgrade(startup=False)))
        for backup in backups:
            key = usvfs.version_key(backup.version)
            if key == current:
                continue
            verb = "Upgrade" if current is not None and key > current else "Revert"
            entries.append((f"Borderlands usvfs: {verb} to {backup.version} (from backup)",
                            f"Restore the usvfs files saved in {backup.path.name}",
                            lambda b=backup: self.restore(b)))
        return entries
