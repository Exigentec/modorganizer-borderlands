"""MO2-facing management of the Borderlands 4 SDK: set up, enable/disable, update, repair, auto-heal.

How the SDK is installed (found by testing it in MO2 with the newer usvfs):

* the loader and its Python runtime under ``OakGame/Binaries/`` have to be **real files** in the game
  folder (the game loads them before the virtual file system is in place);
* everything else, the SDK's own mods in ``sdk_mods/``, can be virtual, so the **whole archive** goes
  into a mod of its own, "SDK Mod Support", kept at the lowest priority and owned by this plugin.

The plugin keeps a copy of the release archive and learns the file list and hashes from it
(:mod:`sdk`).  Both places are checked at startup, before MO2 starts any program, and after the game
exits; anything missing or changed is put right without asking (the mod is the plugin's own, and the
real files are checked strictly).  Switching the SDK off removes the real files so the game runs stock.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, qInfo, qWarning

from . import netio, sdk, sdkcontent, ui
from .spec import GameSpec

TITLE = "Borderlands SDK"
PROBLEM_NOT_ACTIVE = 0   # SDK mods are installed, but the SDK is not set up or is switched off
PROBLEM_FILES = 1        # the SDK's files are missing or changed (and could not be put right)
PROBLEM_UPDATE = 2       # a newer release exists
PROBLEM_USVFS = 3        # usvfs is too old for SDK mods supplied through MO2, or needs a restart
MAX_LISTED = 8


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def _listing(names: list[str], limit: int = MAX_LISTED) -> str:
    shown = [f"    {n}" for n in names[:limit]]
    if len(names) > limit:
        shown.append(f"    ... and {len(names) - limit} more")
    return "\n".join(shown)


class _Bridge(QObject):
    """Hands a worker thread's result back to the thread that created it (the UI thread)."""

    done = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.done.connect(lambda callback, result: callback(result))


@dataclass
class Health:
    """How the SDK's files stand: in the SDK mod, and in the game folder."""

    mod: sdk.Report
    real: sdk.Report
    enabled: bool
    mod_name: str

    def issues(self) -> list[str]:
        out = [f"{rel} (missing from '{self.mod_name}')" for rel in self.mod.missing]
        out += [f"{rel} (changed in '{self.mod_name}')" for rel in self.mod.modified]
        if self.enabled:
            out += [f"{rel} (missing from the game folder)" for rel in self.real.missing]
            out += [f"{rel} (changed in the game folder)" for rel in self.real.modified]
        else:
            out += [f"{rel} (still in the game folder)" for rel in self.real.ok]
        return out

    @property
    def complete(self) -> bool:
        return not self.issues()


@dataclass
class HealResult:
    written_mod: list[str] = field(default_factory=list)
    written_real: list[str] = field(default_factory=list)
    removed_real: list[str] = field(default_factory=list)
    kept_modified_real: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped_running: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.written_mod or self.written_real or self.removed_real)


class SdkManager:
    def __init__(self, organizer, spec: GameSpec, active_mods, parent, *, update_check=lambda: True,
                 on_change=lambda: None, fetch=netio.request_json, download=netio.download,
                 run_async=None, game_running=sdk.game_running, usvfs_manager=None, sdk_active=lambda: True):
        self._organizer = organizer
        self._spec = spec
        self._sdk = spec.sdk
        self._active_mods = active_mods      # callable -> [(name, Path)], lowest priority first
        self._parent = parent                # callable -> the main window, or None
        self._update_check = update_check    # callable -> bool, the "check for updates" setting
        self._on_change = on_change
        self._fetch, self._download, self._game_running = fetch, download, game_running
        self._run_async = run_async or self._thread_async
        self.usvfs = usvfs_manager
        self._sdk_active = sdk_active        # callable -> bool: False leaves only the usvfs functions on
        self._bridge = None
        self._hasher = sdk.Hasher()
        self._manifest: sdk.Manifest | None = None
        self._manifest_key = None
        self._manifest_error = ""
        self._refresh_scheduled = False
        self.health: Health | None = None
        self.release: sdk.Release | None = None
        self.state = sdk.SdkState(self.directory / "state.json")

    # -- where things are ------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return Path(self._organizer.basePath()) / "plugin_state" / "borderlands" / "sdk"

    @property
    def zip_path(self) -> Path:
        return self.directory / self._sdk.asset

    @property
    def game_dir(self) -> Path:
        return Path(self._organizer.managedGame().dataDirectory().absolutePath())

    @property
    def mod_dir(self) -> Path:
        return Path(self._organizer.modsPath()) / self._sdk.mod_name

    def _log(self, message: str) -> None:
        qInfo(f"Borderlands: {message}")

    # -- the cached archive ----------------------------------------------------------------

    def manifest(self) -> sdk.Manifest | None:
        """Every SDK file with its hash, learned from the cached archive; None if unusable."""
        try:
            stat = self.zip_path.stat()
        except OSError:
            self._manifest = self._manifest_key = None
            self._manifest_error = ""
            sdkcontent.set_base(frozenset())
            return None
        key = (stat.st_size, stat.st_mtime_ns)
        if key != self._manifest_key:
            self._manifest_key = key
            try:
                self._manifest = sdk.read_manifest(self.zip_path, protected=[self._spec.binary])
                self._manifest_error = ""
            except sdk.UnsafeArchive as err:
                self._manifest, self._manifest_error = None, str(err)
            sdkcontent.set_base(sdk.base_names(self._manifest) if self._manifest else frozenset())
        return self._manifest

    def real_part(self, manifest: sdk.Manifest) -> sdk.Manifest:
        """The files that must also be real files in the game folder."""
        prefixes = tuple(p.casefold() for p in self._sdk.real_prefixes)
        return {rel: entry for rel, entry in manifest.items() if rel.casefold().startswith(prefixes)}

    # -- checking and healing --------------------------------------------------------------

    def refresh(self) -> None:
        """Re-check both places (cheap: hashes are remembered per file) and update the notification."""
        manifest = self.manifest()
        if manifest and self.state.configured:
            self.health = Health(
                sdk.verify(manifest, self.mod_dir, self._hasher),
                sdk.verify(self.real_part(manifest), self.game_dir, self._hasher),
                bool(self.state.enabled), self._sdk.mod_name,
            )
        else:
            self.health = None
        self._on_change()

    def heal(self, overwrite_real: bool = True) -> HealResult | None:
        """Put the SDK mod and the real files right.  None if the SDK is not set up.

        * The SDK mod is the plugin's own: missing or changed files are simply restored.
        * With the SDK on, the real files are restored too (changed ones only if ``overwrite_real``).
        * With the SDK off, the real files are removed (only those that still match the release).
        * If the game is running its files are locked, so the real ones are left for next time.
        """
        manifest = self.manifest()
        if manifest is None or not self.state.configured:
            return None
        result = HealResult()

        mod_report = sdk.verify(manifest, self.mod_dir, self._hasher)
        wanted = mod_report.missing + mod_report.modified
        if wanted:
            self.mod_dir.mkdir(parents=True, exist_ok=True)
            done = sdk.extract(self.zip_path, manifest, self.mod_dir, wanted)
            result.written_mod, result.failed = done.written, done.failed

        real = self.real_part(manifest)
        report = sdk.verify(real, self.game_dir, self._hasher)
        if self.state.enabled:
            wanted = report.missing + (report.modified if overwrite_real else [])
            result.kept_modified_real = [] if overwrite_real else list(report.modified)
            if wanted and self._game_running(self._spec.binary):
                result.skipped_running = True
            elif wanted:
                done = sdk.extract(self.zip_path, real, self.game_dir, wanted)
                result.written_real, result.failed = done.written, result.failed + done.failed
        elif report.ok:
            if self._game_running(self._spec.binary):
                result.skipped_running = True
            else:
                purged = sdk.purge(real, self.game_dir, self._hasher)
                result.removed_real, result.failed = purged.removed, result.failed + purged.failed
                result.kept_modified_real = purged.kept_modified
        if result.written_mod:
            self._refresh_mo2()
        return result

    def _auto_heal(self, when: str) -> None:
        """Heal quietly and say in the log what was done; never raises."""
        try:
            result = self.heal()
            self.refresh()
        except Exception as err:
            qWarning(f"Borderlands: could not check the SDK's files {when}: {err}")
            return
        if result is None:
            return
        if result.written_mod:
            note = (" (MO2 sees files restored in the SDK mod from its next refresh, which has been requested)"
                    if when.startswith("before") else "")
            self._log(f"restored {len(result.written_mod)} file(s) in '{self._sdk.mod_name}' {when}{note}")
        if result.written_real:
            self._log(f"restored {len(result.written_real)} SDK file(s) in the game folder {when}")
        if result.removed_real:
            self._log(f"removed {len(result.removed_real)} SDK file(s) from the game folder {when}: the SDK is switched off")
        if result.skipped_running:
            self._log(f"the game is running, so its SDK files were left as they are {when}")
        if result.failed:
            qWarning(f"Borderlands: {len(result.failed)} SDK file(s) could not be put right {when}: " +
                     "; ".join(f"{rel} ({why})" for rel, why in result.failed[:MAX_LISTED]))

    def _refresh_mo2(self) -> None:
        """Ask MO2 to look at the SDK mod again (once, after whatever is running has finished)."""
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True

        def refresh() -> None:
            self._refresh_scheduled = False
            try:
                mod = self._organizer.modList().getMod(self._sdk.mod_name)
                if mod is not None:
                    self._organizer.modDataChanged(mod)
            except Exception as err:
                qWarning(f"Borderlands: could not refresh MO2's view of '{self._sdk.mod_name}': {err}")

        QTimer.singleShot(0, refresh)

    def before_launch(self, app_path: str) -> None:
        """Called before MO2 starts any program: the real SDK files must be right at that moment."""
        if self.active and self.state.configured:
            self._auto_heal(f"before MO2 started {Path(str(app_path)).name or 'a program'}")

    def after_run(self, app_path: str) -> None:
        """Called when a program MO2 started has ended: the game's files are unlocked again."""
        if self.active and self.state.configured and Path(str(app_path)).name.casefold() == Path(self._spec.binary).name.casefold():
            self._auto_heal("after the game exited")

    @property
    def active(self) -> bool:
        """False while the SDK functions are switched off (the plugin setting); usvfs handling stays on."""
        try:
            return bool(self._sdk_active())
        except Exception:
            return False

    # -- what the notification needs -------------------------------------------------------

    def sdk_mods(self) -> list[str]:
        """Names of the enabled mods that contain SDK mods (the SDK's own mod does not count)."""
        base = sdkcontent.SdkModContent.base
        return [name for name, path in self._active_mods()
                if name != self._sdk.mod_name and sdk.mod_has_sdk_mods(path, base)]

    def update_available(self) -> bool:
        return (self.state.configured and self.release is not None
                and sdk.is_newer(self.release.tag, self.state.tag)
                and self.release.tag != self.state.skipped_tag)

    def _usvfs_problem(self):
        if self.usvfs is None or not (self.state.configured or self.sdk_mods()):
            return None
        return self.usvfs.problem_text()

    def problems(self) -> list[int]:
        keys = []
        if not self.active:
            return keys
        if not self.state.enabled and self.sdk_mods():
            keys.append(PROBLEM_NOT_ACTIVE)
        if self.health is not None and not self.health.complete:
            keys.append(PROBLEM_FILES)
        if self.update_available():
            keys.append(PROBLEM_UPDATE)
        if self._usvfs_problem() is not None:
            keys.append(PROBLEM_USVFS)
        return keys

    def short_description(self, key: int) -> str:
        if key == PROBLEM_NOT_ACTIVE:
            return "SDK mods are installed, but the SDK is " + ("switched off" if self.state.configured else "not set up")
        if key == PROBLEM_FILES:
            return "Some SDK files are missing or have been changed"
        if key == PROBLEM_USVFS:
            problem = self._usvfs_problem()
            return problem[0] if problem else "usvfs"
        return f"SDK update available ({self.release.tag if self.release else '?'})"

    def full_description(self, key: int) -> str:
        if key == PROBLEM_NOT_ACTIVE:
            mods = self.sdk_mods()
            listing = "<br>".join(f"&bull; {m}" for m in mods[:MAX_LISTED]) + (
                f"<br>... and {len(mods) - MAX_LISTED} more" if len(mods) > MAX_LISTED else "")
            state = "switched off" if self.state.configured else "not set up"
            return (f"These enabled mods are SDK mods:<br><br>{listing}<br><br>"
                    f"The SDK is {state}, so they will not load in the game. Use the fix to "
                    f"{'enable' if self.state.configured else 'download and install'} it, or use the Tools menu "
                    "(<i>Borderlands SDK</i>) at any time. Disable the SDK again to play the game stock.")
        if key == PROBLEM_FILES:
            issues = self.health.issues() if self.health else []
            lines = [f"&bull; {issue}" for issue in issues[:MAX_LISTED]]
            extra = len(issues) - len(lines)
            return (f"The SDK ({self.state.tag}) is {'enabled' if self.state.enabled else 'switched off'}, but its files "
                    "are not as they should be:<br><br>" + "<br>".join(lines) +
                    (f"<br>... and {extra} more" if extra > 0 else "") +
                    "<br><br>The plugin puts these right before each launch; if they are still listed, something "
                    "(a running game, a locked file) stopped it. The fix tries again.")
        if key == PROBLEM_USVFS:
            problem = self._usvfs_problem()
            return problem[1] if problem else ""
        release = self.release
        return (f"SDK {release.tag} is available (you have {self.state.tag}).<br><br>The update removes the old "
                "files, checking each against its hash, then installs the new release.") if release else ""

    def fix(self, key: int) -> None:
        if key == PROBLEM_UPDATE and self.release is not None:
            self.update(self.release)
        elif key == PROBLEM_USVFS and self.usvfs is not None:
            self.usvfs.fix()
        else:
            self.enable(repair=key == PROBLEM_FILES)

    # -- dialogs ---------------------------------------------------------------------------

    def _say(self, kind: str, text: str) -> None:
        ui.say(self._parent(), kind, TITLE, text)

    def _ask(self, text: str, buttons: list[str], default: int = 0) -> str:
        return ui.ask(self._parent(), TITLE, text, buttons, default)

    def _running(self) -> bool:
        if self._game_running(self._spec.binary):
            self._say("warning", "Borderlands 4 is running. Close the game first: its SDK files are in use "
                                 "and cannot be changed while it runs.")
            return True
        return False

    def _guard(self, label: str, work) -> None:
        try:
            work()
        except Exception as err:  # a dialog action must never take MO2 down with it
            qWarning(f"Borderlands: SDK {label} failed: {err}")
            self._say("critical", f"The SDK {label} failed: {err}")
            try:
                self.refresh()
            except Exception:
                pass

    # -- getting a release -----------------------------------------------------------------

    def _latest(self) -> sdk.Release:
        return sdk.latest_release(self._sdk.repo, self._sdk.asset, self._fetch)

    def _fetch_archive(self, release: sdk.Release, dest: Path) -> bool:
        """Download ``release`` to ``dest`` and check it; False if cancelled or refused (user told)."""
        progress = ui.Progress(self._parent(), TITLE, f"Downloading the SDK {release.tag} ({_mb(release.size)})...")
        try:
            self._download(release.url, dest, release.sha256, progress.update)
            sdk.read_manifest(dest, protected=[self._spec.binary])
        except netio.Cancelled:
            return False
        except netio.ChecksumError as err:
            self._say("critical", f"The download was discarded: {err}.")
            return False
        except netio.NetworkError as err:
            self._say("critical", f"Could not download the SDK: {err}")
            return False
        except sdk.UnsafeArchive as err:
            self._discard(dest)
            self._say("critical", f"The downloaded SDK archive was refused: {err}.")
            return False
        finally:
            progress.close()
        if not release.sha256:
            self._log(f"GitHub publishes no checksum for {release.tag}, so the download could only be checked "
                      "for being a usable archive")
        return True

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _ensure_cache(self) -> bool:
        """Make sure a copy of the release archive is cached, downloading the latest if not."""
        if self.zip_path.exists():
            return True
        try:
            release = ui.busy(self._latest)
        except (sdk.NoRelease, netio.NetworkError) as err:
            self._say("critical", f"Could not find the SDK release: {err}")
            return False
        answer = self._ask(
            f"Download the Borderlands 4 SDK {release.tag} ({_mb(release.size)}) from github.com/{self._sdk.repo} "
            "and keep a copy for this plugin to install and check?", ["Download", "Cancel"])
        if answer != "Download":
            return False
        if not self._fetch_archive(release, self.zip_path):
            return False
        self.state.update(tag=release.tag)
        self.manifest()
        self._log(f"downloaded the SDK {release.tag}")
        return True

    # -- the actions -----------------------------------------------------------------------

    def _overwrite_choice(self, manifest: sdk.Manifest) -> bool | None:
        """Whether changed real files may be replaced; None if the user cancelled."""
        changed = sdk.verify(self.real_part(manifest), self.game_dir, self._hasher).modified
        if not changed:
            return True
        answer = self._ask(
            f"{len(changed)} SDK file(s) in the game folder differ from the release:\n\n{_listing(changed)}\n\n"
            "Replace them with the release's versions?", ["Replace them", "Leave them as they are", "Cancel"])
        return None if answer == "Cancel" else answer == "Replace them"

    def enable(self, repair: bool = False) -> None:
        """Set the SDK up (first time), switch it on, or repair it."""
        self._guard("repair" if repair else "set-up", lambda: self._enable(repair))

    def _enable(self, repair: bool) -> None:
        if not self._ensure_cache():
            return
        manifest = self.manifest()
        if manifest is None:
            self._say("critical", f"The cached SDK archive cannot be used: {self._manifest_error}.")
            return
        if self._running():
            return
        overwrite = self._overwrite_choice(manifest)
        if overwrite is None:
            return
        self.state.update(enabled=True, setup_declined=False)
        progress = ui.Progress(self._parent(), TITLE, "Installing the SDK...")
        progress.update(0, 1)
        try:
            result = self.heal(overwrite_real=overwrite)
        finally:
            progress.close()
        self.refresh()
        self._log(f"SDK {self.state.tag} " + ("repaired" if repair else "enabled") +
                  f": {len(result.written_mod)} file(s) in '{self._sdk.mod_name}', {len(result.written_real)} in the game folder")
        problems = [f"{rel}: {why}" for rel, why in result.failed]
        if self.health is not None and not self.health.complete:
            problems += self.health.issues()
        if problems:
            self._say("warning", "Some SDK files could not be put in place or were left as they are:\n\n" +
                      _listing(problems))
        else:
            self._say("information", f"The SDK {self.state.tag} is installed: its mods are in '{self._sdk.mod_name}' "
                                     "and its loader is in the game's Binaries folder.")

    def disable(self) -> None:
        """Remove the SDK's real files from the game folder so the game runs stock."""
        self._guard("disable", self._disable)

    def _disable(self) -> None:
        manifest = self.manifest()
        if manifest is None:
            self._say("information", "There is no record of the SDK's files (it was never set up through this "
                                     "plugin), so there is nothing to remove.")
            return
        count = len(self.real_part(manifest))
        if self._ask(f"Remove the SDK's {count} files from the game's Binaries folder, so the game runs stock "
                     "(for matchmaking, for example)?\n\nEach file is checked against the release first; files "
                     f"you changed are kept. The '{self._sdk.mod_name}' mod stays, but without its loader in the "
                     "game folder the SDK does not start. You can enable the SDK again at any time.",
                     ["Disable", "Cancel"]) != "Disable":
            return
        if self._running():
            return
        self.state.update(enabled=False)
        result = self.heal()
        self.refresh()
        self._log(f"SDK disabled: {len(result.removed_real)} removed from the game folder, "
                  f"{len(result.kept_modified_real)} kept (modified), {len(result.failed)} failed")
        problems = []
        if result.kept_modified_real:
            problems.append(f"{len(result.kept_modified_real)} file(s) differ from the release and were kept:\n\n"
                            f"{_listing(result.kept_modified_real)}")
        if result.failed:
            problems.append(f"{len(result.failed)} file(s) could not be removed:\n\n" +
                            _listing([f"{rel}: {why}" for rel, why in result.failed]))
        summary = f"The SDK is disabled: {len(result.removed_real)} file(s) removed."
        if problems:
            self._say("warning", summary + "\n\n" + "\n\n".join(problems))
        else:
            self._say("information", summary + " The game now runs without it.")

    def check_updates(self, manual: bool = True) -> None:
        self._guard("update check", lambda: self._check_updates(manual))

    def _check_updates(self, manual: bool) -> None:
        try:
            release = ui.busy(self._latest)
        except (sdk.NoRelease, netio.NetworkError) as err:
            self._log(f"could not check for an SDK update: {err}")
            if manual:
                self._say("warning", f"Could not check for an SDK update: {err}")
            return
        self.release = release
        self._on_change()
        if not self.state.configured:
            if manual:
                self._say("information", f"The latest SDK is {release.tag}. It is not set up yet: use "
                                         "Tools > Borderlands SDK: Set up.")
        elif sdk.is_newer(release.tag, self.state.tag):
            self._offer_update(release, allow_skip=not manual)
        elif manual:
            self._say("information", f"The SDK is up to date ({self.state.tag}).")

    def _offer_update(self, release: sdk.Release, allow_skip: bool) -> None:
        body = (f"A new Borderlands 4 SDK is available: {release.tag} (you have {self.state.tag or 'none'}, "
                f"{_mb(release.size)} to download).\n\nUpdating removes the old SDK's files, checking each against "
                "its hash, then installs the new release.")
        choices = ["Update", "Skip this version", "Not now"] if allow_skip else ["Update", "Not now"]
        answer = self._ask(body, choices)
        if answer == "Update":
            self.update(release)
        elif answer == "Skip this version":
            self.state.update(skipped_tag=release.tag)
            self._on_change()

    def update(self, release: sdk.Release) -> None:
        self._guard("update", lambda: self._update(release))

    def _update(self, release: sdk.Release) -> None:
        enabled = bool(self.state.enabled)
        if enabled and self._running():
            return
        # Download and check first: nothing is touched until the new release is known to be good.
        fresh = self.zip_path.with_name(self.zip_path.name + ".new")
        if not self._fetch_archive(release, fresh):
            self._discard(fresh)
            return
        old = self.manifest()
        if old is not None:
            if enabled:
                purged = sdk.purge(self.real_part(old), self.game_dir, self._hasher)
                if purged.failed:
                    self._discard(fresh)
                    self.refresh()
                    self._say("critical", "The old SDK files could not all be removed, so the update was stopped:\n\n" +
                              _listing([f"{rel}: {why}" for rel, why in purged.failed]) +
                              "\n\nClose anything using them and use the fix on the SDK notification to repair.")
                    return
                self._log(f"update: removed {len(purged.removed)} old SDK file(s) from the game folder, kept "
                          f"{len(purged.kept_modified)} modified")
            mod_purged = sdk.purge(old, self.mod_dir, self._hasher)  # the plugin's own mod: old-only files go too
            self._log(f"update: removed {len(mod_purged.removed)} old file(s) from '{self._sdk.mod_name}'")
        os.replace(fresh, self.zip_path)
        self.state.update(tag=release.tag, skipped_tag="")
        manifest = self.manifest()
        if manifest is None:
            self._say("critical", f"The new SDK archive cannot be used: {self._manifest_error}.")
            return
        overwrite = self._overwrite_choice(manifest) if enabled else True
        if overwrite is None:
            self.refresh()
            return
        self.heal(overwrite_real=overwrite)
        self.refresh()
        self._log(f"SDK updated to {release.tag}")
        self._say("information", f"The SDK is now {release.tag}." if enabled else
                  f"The SDK copy was updated to {release.tag}. It is switched off; enable it from the Tools menu.")

    # -- startup and installs --------------------------------------------------------------

    def startup(self) -> None:
        """At MO2 startup: usvfs, a strict check of the files, set-up offer if needed, update check."""
        if self.usvfs is not None:
            try:
                self.usvfs.startup()
            except Exception as err:
                qWarning(f"Borderlands: could not check usvfs at startup: {err}")
            if getattr(self.usvfs, "restarting", False):
                return  # MO2 is about to restart: the SDK is checked in the new session
        if not self.active:
            return
        if self.state.configured:
            self._auto_heal("at startup")
        else:
            self.refresh()
        if not self.state.configured:
            mods = self.sdk_mods()
            if mods and not self.state.setup_declined:
                self._guard("set-up offer", self._offer_setup)
            else:
                # nothing is downloaded or restored until the SDK is set up: say so instead of staying silent
                self._log("SDK support is on but the SDK is not set up, so nothing is installed or restored"
                          + (f" ({len(mods)} enabled mod(s) need it; the set-up offer was declined)"
                             if mods and self.state.setup_declined else "")
                          + ". Use Tools > Borderlands SDK: Set up...")
            return
        if self.state.enabled and self._update_check():
            self._run_async(self._latest, self._startup_release)

    def _offer_setup(self) -> None:
        mods = self.sdk_mods()
        answer = self._ask(
            f"{len(mods)} enabled mod(s) need the Borderlands 4 SDK, which is not set up:\n\n{_listing(mods)}\n\n"
            f"Download it from GitHub and install it? Its mods go into a mod called '{self._sdk.mod_name}' and its "
            "loader into the game's Binaries folder.",
            ["Set up the SDK", "Not now", "Don't ask again"], default=1)
        if answer == "Set up the SDK":
            self.enable()
        elif answer == "Don't ask again":
            self.state.update(setup_declined=True)

    def _startup_release(self, result) -> None:
        if isinstance(result, Exception):
            self._log(f"could not check for an SDK update: {result}")
            return
        self.release = result
        self._on_change()
        if self.state.enabled and self.update_available():
            self._guard("update offer", lambda: self._offer_update(result, allow_skip=True))

    def on_profile_changed(self) -> None:
        if self.active and self.state.configured:
            self._auto_heal("after the profile changed")

    def on_mod_installed(self, mod_name: str, mod_path: Path) -> None:
        """Warn in the log when an SDK mod arrives and the SDK is not going to load it."""
        if not self.active or mod_name == self._sdk.mod_name:
            return
        if sdk.mod_has_sdk_mods(mod_path, sdkcontent.SdkModContent.base) and not self.state.enabled:
            qWarning(f"Borderlands: '{mod_name}' is an SDK mod, but the SDK is "
                     f"{'switched off' if self.state.configured else 'not set up'}, so it will not load. "
                     "Use Tools > Borderlands SDK, or the SDK notification's fix.")

    def menu_entries(self) -> list[tuple[str, str, object]]:
        """``(label, tooltip, callback)`` for the Tools menu."""
        if not self.active:
            return self.usvfs.menu_entries() if self.usvfs is not None else []
        if self.state.enabled:
            toggle = ("Borderlands SDK: Disable", "Remove the SDK's loader so the game runs stock", self.disable)
        elif self.state.configured:
            toggle = ("Borderlands SDK: Enable", "Put the SDK's loader back in the game folder", self.enable)
        else:
            toggle = ("Borderlands SDK: Set up...", "Download the SDK and install it", self.enable)
        entries = [toggle, ("Borderlands SDK: Check for updates", "Look for a newer SDK release",
                            lambda: self.check_updates(manual=True))]
        if self.usvfs is not None:
            entries += self.usvfs.menu_entries()
        return entries

    # -- threads ---------------------------------------------------------------------------

    def _thread_async(self, work, callback) -> None:
        """Run ``work`` on a thread; ``callback(result_or_exception)`` runs on the UI thread."""
        if self._bridge is None:
            self._bridge = _Bridge()  # created here, on the UI thread, so its signal is delivered there

        def run():
            try:
                result = work()
            except Exception as err:
                result = err
            self._bridge.done.emit(callback, result)

        threading.Thread(target=run, daemon=True).start()
