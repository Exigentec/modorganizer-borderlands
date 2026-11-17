"""The SDK manager and its MO2 wiring, driven with a fake GitHub and scripted dialogs.

The SDK lives in two places: the whole archive in the "SDK Mod Support" mod, and the files under
``OakGame/Binaries/`` also as real files in the game folder.
"""

import hashlib
import io
import threading
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests import fakes

fakes.install()

import mobase  # noqa: E402  (the fake)
from borderlands import netio, sdk, sdkcontent  # noqa: E402
from borderlands.game import BorderlandsGame  # noqa: E402
from borderlands.sdkdiagnose import SdkDiagnose  # noqa: E402
from borderlands.sdkmanager import (PROBLEM_FILES, PROBLEM_NOT_ACTIVE, PROBLEM_UPDATE, PROBLEM_USVFS,  # noqa: E402
                                    SdkManager)
from borderlands.spec import BORDERLANDS_2, BORDERLANDS_3, BORDERLANDS_4  # noqa: E402
from borderlands.toolsmenu import BorderlandsTools  # noqa: E402
from tests.fakes import FakeEntry as E, FakeTree  # noqa: E402

REPO_URL = "https://github.com/bl-sdk/oak2-mod-manager/releases/download/{}/oak2-sdk.zip"
SUPPORT = "SDK Mod Support"

V1 = {
    "OakGame/Binaries/Win64/dsound.dll": b"loader-1",
    "OakGame/Binaries/Win64/Plugins/python.dll": b"python-1",
    "OakGame/Binaries/Win64/Plugins/old_only.dll": b"only-in-1",
    "sdk_mods/__main__.py": b"main-1",
    "sdk_mods/mods_base.sdkmod": b"base-1",
    "sdk_mods/keybinds/__init__.py": b"keybinds-1",
    "sdk_mods/settings/.gitignore": b"*",
}
V2 = {
    "OakGame/Binaries/Win64/dsound.dll": b"loader-2",
    "OakGame/Binaries/Win64/Plugins/python.dll": b"python-1",   # unchanged
    "OakGame/Binaries/Win64/Plugins/new_only.dll": b"only-in-2",
    "sdk_mods/__main__.py": b"main-2",
    "sdk_mods/mods_base.sdkmod": b"base-2",
    "sdk_mods/keybinds/__init__.py": b"keybinds-1",
    "sdk_mods/settings/.gitignore": b"*",
}


def real_part(files):
    return {rel: data for rel, data in files.items() if rel.startswith("OakGame/Binaries/")}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


class Hub:
    """A fake GitHub: the releases it offers, and what was asked of it."""

    def __init__(self):
        self.latest = "v0.3"
        self.zips = {"v0.3": zip_bytes(V1), "v0.4": zip_bytes(V2)}
        self.digest_published = True
        self.fetches, self.downloads = [], []
        self.fetch_error = self.download_error = self.fetch_status = None
        self.while_downloading = None

    def payload(self, tag):
        data = self.zips[tag]
        asset = {"name": "oak2-sdk.zip", "size": len(data), "browser_download_url": REPO_URL.format(tag)}
        if self.digest_published:
            asset["digest"] = "sha256:" + sha(data)
        return {"tag_name": tag, "draft": False, "prerelease": False,
                "html_url": f"https://github.com/bl-sdk/oak2-mod-manager/releases/tag/{tag}", "assets": [asset]}

    def fetch(self, url, data=None, headers=None, timeout=8.0):
        self.fetches.append(url)
        if self.fetch_error:
            raise self.fetch_error
        if self.fetch_status:
            return self.fetch_status, None
        return 200, self.payload(self.latest)

    def download(self, url, dest, expected_sha256="", progress=None, **kwargs):
        self.downloads.append((url, Path(dest).name))
        if self.download_error:
            raise self.download_error
        if self.while_downloading:
            self.while_downloading()
        tag = url.split("/download/")[1].split("/")[0]
        data = self.zips[tag]
        if expected_sha256 and sha(data) != expected_sha256:
            raise netio.ChecksumError("the download does not match the checksum GitHub publishes for it")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(data)
        if progress:
            progress(len(data) // 2, len(data))
            progress(len(data), len(data))
        return sha(data)


class StubUsvfs:
    """Stands in for the usvfs manager: what it reports, and what was asked of it."""

    def __init__(self):
        self.problem = None
        self.started = 0
        self.fixed = 0
        self.entries = []
        self.startup_error = None
        self.restarting = False

    def startup(self):
        self.started += 1
        if self.startup_error:
            raise self.startup_error

    def problem_text(self):
        return self.problem

    def fix(self):
        self.fixed += 1

    def menu_entries(self):
        return list(self.entries)


class Base(unittest.TestCase):
    def setUp(self):
        fakes.LOG.clear()
        fakes.reset_ui()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.game_dir = self.root / "game"
        (self.game_dir / "OakGame/Binaries/Win64").mkdir(parents=True)
        (self.game_dir / "OakGame/Binaries/Win64/Borderlands4.exe").write_bytes(b"the game")
        self.mods_dir = self.root / "mods"
        self.mods_dir.mkdir()
        self.mod_dir = self.mods_dir / SUPPORT
        self.mods: list[tuple[str, Path]] = []
        self.hub = Hub()
        self.running = False
        self.changes = []
        self.org = fakes.FakeOrganizer()
        self.org.base_path = str(self.root / "instance")
        self.org.mods_path = str(self.mods_dir)
        self.game = BorderlandsGame(BORDERLANDS_4)
        self.game.setGamePath(str(self.game_dir))
        self.game.init(self.org)
        self.org.managed = self.game
        self.update_check = True
        self.async_calls = []
        sdkcontent.set_base(frozenset())
        self.addCleanup(sdkcontent.set_base, frozenset())

    def manager(self, **over):
        def run_async(work, callback):
            self.async_calls.append(work)
            try:
                result = work()
            except Exception as err:
                result = err
            callback(result)

        kwargs = dict(update_check=lambda: self.update_check, on_change=lambda: self.changes.append(1),
                      fetch=self.hub.fetch, download=self.hub.download, run_async=run_async,
                      game_running=lambda binary: self.running)
        kwargs.update(over)
        return SdkManager(self.org, BORDERLANDS_4, lambda: list(self.mods), lambda: None, **kwargs)

    # -- helpers
    def choose(self, *labels):
        fakes.UI["choices"] = list(labels)

    def asked(self):
        return [d for d in fakes.DIALOGS if d[0] == "ask"]

    def said(self, kind):
        return [d[2] for d in fakes.DIALOGS if d[0] == kind]

    def log(self):
        return "\n".join(m for _, m in fakes.LOG)

    def warnings(self):
        return [m for lvl, m in fakes.LOG if lvl == "warning"]

    def read(self, root, files):
        return {rel: sdk.path_of(root, rel).read_bytes() if sdk.path_of(root, rel).is_file() else None for rel in files}

    def in_mod(self, files=V1):
        return self.read(self.mod_dir, files)

    def in_game(self, files=V1):
        return self.read(self.game_dir, files)

    def present(self, root, files):
        return sorted(rel for rel in files if sdk.path_of(root, rel).is_file())

    def put(self, root, files):
        for rel, data in files.items():
            path = sdk.path_of(root, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def add_sdk_mod(self, name="Some Mod", active=True, files=("sdk_mods/thing.sdkmod",)):
        folder = self.mods_dir / name
        for rel in files:
            path = sdk.path_of(folder, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        if active:
            self.mods.append((name, folder))
        return folder

    def set_up(self, mgr, tag="v0.3", files=V1, enabled=True):
        """Put the SDK in place without going through the dialogs."""
        mgr.directory.mkdir(parents=True, exist_ok=True)
        mgr.zip_path.write_bytes(self.hub.zips[tag])
        self.put(self.mod_dir, files)
        if enabled:
            self.put(self.game_dir, real_part(files))
        mgr.state.update(enabled=enabled, tag=tag)
        mgr.refresh()


class SettingUp(Base):
    def test_the_first_enable_downloads_and_installs_into_both_places(self):
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertEqual(self.in_mod(), V1)                                    # the whole archive, in the mod
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))           # the loader and runtime, real
        self.assertEqual(self.present(self.game_dir, V1), sorted(real_part(V1)))   # and nothing else
        self.assertEqual((mgr.state.enabled, mgr.state.tag), (True, "v0.3"))
        self.assertTrue(mgr.zip_path.is_file())                                # a copy is kept
        self.assertTrue(mgr.health.complete)
        self.assertEqual(mgr.problems(), [])
        (question,) = self.asked()
        self.assertIn("v0.3", question[2])
        self.assertIn("github.com/bl-sdk/oak2-mod-manager", question[2])
        self.assertEqual(question[3], ["Download", "Cancel"])
        self.assertIn("'SDK Mod Support'", self.said("information")[-1])
        self.assertEqual(self.hub.downloads, [(REPO_URL.format("v0.3"), "oak2-sdk.zip")])
        self.assertTrue(all(p._closed for p in fakes.PROGRESS))
        self.assertEqual(self.changes[-1:], [1])                               # the notification was refreshed

    def test_nothing_is_pinned_the_layout_comes_from_the_archive(self):
        self.hub.zips["v0.3"] = zip_bytes({"Elsewhere/new/a.bin": b"a", "OakGame/Binaries/Win64/x.dll": b"x"})
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertEqual((self.mod_dir / "Elsewhere/new/a.bin").read_bytes(), b"a")
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/x.dll").read_bytes(), b"x")
        self.assertFalse((self.game_dir / "Elsewhere").exists())

    def test_cancelling_the_download_question_changes_nothing(self):
        mgr = self.manager()
        self.choose("Cancel")
        mgr.enable()
        self.assertEqual((self.hub.downloads, mgr.zip_path.exists(), mgr.state.configured), ([], False, False))
        self.assertEqual(self.present(self.mod_dir, V1) + self.present(self.game_dir, V1), [])

    def test_no_release_or_no_network_is_reported(self):
        for setup in (lambda h: setattr(h, "fetch_error", netio.NetworkError("offline")),
                      lambda h: setattr(h, "fetch_status", 404)):
            fakes.reset_ui()
            self.hub = Hub()
            setup(self.hub)
            self.manager().enable()
            self.assertIn("Could not find the SDK release", self.said("critical")[-1])
            self.assertEqual(self.asked(), [])

    def test_a_checksum_failure_discards_the_download(self):
        self.hub.download_error = netio.ChecksumError("the download does not match the checksum GitHub publishes for it")
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertIn("discarded", self.said("critical")[-1])
        self.assertEqual((mgr.zip_path.exists(), mgr.state.configured), (False, False))

    def test_a_download_error_is_reported(self):
        self.hub.download_error = netio.NetworkError("connection reset")
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertIn("connection reset", self.said("critical")[-1])
        self.assertFalse(mgr.state.configured)

    def test_cancelling_the_download_keeps_everything_as_it_was(self):
        self.hub.download_error = netio.Cancelled()
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertEqual((self.said("critical"), mgr.state.configured, mgr.zip_path.exists()), ([], False, False))

    def test_an_unsafe_archive_is_refused_and_deleted(self):
        self.hub.zips["v0.3"] = zip_bytes({"../escape.dll": b"x", "ok/a.bin": b"a"})
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertIn("refused", self.said("critical")[-1])
        self.assertEqual((mgr.zip_path.exists(), mgr.state.configured), (False, False))
        self.assertFalse((self.root / "escape.dll").exists())

    def test_an_archive_that_would_replace_the_game_exe_is_refused(self):
        self.hub.zips["v0.3"] = zip_bytes({"OakGame/Binaries/Win64/Borderlands4.exe": b"evil"})
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertIn("belongs to the game", self.said("critical")[-1])
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/Borderlands4.exe").read_bytes(), b"the game")

    def test_a_release_without_a_published_checksum_is_still_checked_and_noted(self):
        self.hub.digest_published = False
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertEqual(self.in_mod(), V1)
        self.assertIn("publishes no checksum", self.log())

    def test_the_game_running_blocks_it(self):
        self.running = True
        mgr = self.manager()
        self.choose("Download")
        mgr.enable()
        self.assertIn("is running", self.said("warning")[-1])
        self.assertEqual(self.present(self.game_dir, V1), [])
        self.assertFalse(mgr.state.enabled)

    def test_changed_real_files_are_replaced_only_if_asked(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"patched"})
        self.choose("Replace them")
        mgr.enable()
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))
        self.assertIn("differ from the release", self.asked()[-1][2])
        self.assertIn("dsound.dll", self.asked()[-1][2])

    def test_leaving_changed_real_files_keeps_them_and_says_what_is_still_off(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"patched"})
        self.choose("Leave them as they are")
        mgr.enable()
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").read_bytes(), b"patched")
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/Plugins/python.dll").read_bytes(), b"python-1")
        self.assertTrue(mgr.state.enabled)
        self.assertIn("dsound.dll", self.said("warning")[-1])
        self.assertEqual(mgr.problems(), [PROBLEM_FILES])

    def test_cancelling_at_the_replace_question_leaves_the_state_alone(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"patched"})
        self.choose("Cancel")
        mgr.enable()
        self.assertFalse(mgr.state.enabled)
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").read_bytes(), b"patched")

    def test_changed_files_in_the_sdk_mod_are_never_asked_about(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        self.put(self.mod_dir, {"sdk_mods/mods_base.sdkmod": b"edited by hand"})
        mgr.enable()
        self.assertEqual(self.asked(), [])                                     # the mod is the plugin's own
        self.assertEqual(self.in_mod(), V1)

    def test_an_existing_cached_copy_is_used_without_asking_or_downloading(self):
        mgr = self.manager()
        mgr.directory.mkdir(parents=True)
        mgr.zip_path.write_bytes(self.hub.zips["v0.3"])
        mgr.enable()
        self.assertEqual((self.asked(), self.hub.downloads, self.hub.fetches), ([], [], []))
        self.assertEqual(self.in_mod(), V1)

    def test_a_cached_copy_that_became_unusable_is_reported(self):
        mgr = self.manager()
        mgr.directory.mkdir(parents=True)
        mgr.zip_path.write_bytes(b"not a zip")
        mgr.enable()
        self.assertIn("cannot be used", self.said("critical")[-1])

    def test_repairing_restores_both_places(self):
        mgr = self.manager()
        self.set_up(mgr)
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        (self.mod_dir / "sdk_mods/mods_base.sdkmod").unlink()
        mgr.refresh()
        self.assertEqual(mgr.problems(), [PROBLEM_FILES])
        mgr.enable(repair=True)
        self.assertEqual((self.in_mod(), self.in_game(real_part(V1))), (V1, real_part(V1)))
        self.assertEqual(mgr.problems(), [])
        self.assertIn("repaired", self.log())

    def test_an_unexpected_failure_is_reported_not_raised(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        (self.mod_dir / "sdk_mods/mods_base.sdkmod").unlink()                  # something to write
        with mock.patch.object(sdk, "extract", side_effect=RuntimeError("disk on fire")):
            mgr.enable()
        self.assertIn("disk on fire", self.said("critical")[-1])
        self.assertTrue([m for m in self.warnings() if "disk on fire" in m])

    def test_files_that_could_not_be_written_are_listed(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        (self.game_dir / "OakGame/Binaries/Win64").mkdir(parents=True, exist_ok=True)
        real_replace = sdk.os.replace

        def replace(src, dst):
            if Path(dst).name == "dsound.dll":
                raise PermissionError("locked")
            return real_replace(src, dst)

        with mock.patch.object(sdk.os, "replace", replace):
            mgr.enable()
        self.assertIn("dsound.dll", self.said("warning")[-1])
        self.assertIn("locked", self.said("warning")[-1])


class Disabling(Base):
    def setUp(self):
        super().setUp()
        self.mgr = self.manager()
        self.set_up(self.mgr)
        self.put(self.game_dir, {"OakGame/Binaries/Win64/my_own_dll.dll": b"user's"})

    def test_removes_the_real_files_and_only_those_leaving_the_mod_alone(self):
        self.choose("Disable")
        self.mgr.disable()
        self.assertEqual(self.present(self.game_dir, V1), [])
        self.assertEqual(self.in_mod(), V1)                                    # the mod keeps everything
        self.assertEqual(sorted(p.name for p in (self.game_dir / "OakGame/Binaries/Win64").iterdir()),
                         ["Borderlands4.exe", "my_own_dll.dll"])               # the game's and the user's files stay
        self.assertEqual((self.mgr.state.enabled, self.mgr.state.configured), (False, True))
        self.assertIn("runs stock", self.asked()[-1][2])
        self.assertIn("files you changed are kept", self.asked()[-1][2])
        self.assertIn("The game now runs without it", self.said("information")[-1])
        self.assertIn(f"SDK disabled: {len(real_part(V1))} removed from the game folder", self.log())
        self.assertTrue(self.mgr.zip_path.exists())
        self.assertTrue(self.mgr.health.complete)                              # off, and nothing left behind

    def test_cancelling_removes_nothing(self):
        self.choose("Cancel")
        self.mgr.disable()
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))
        self.assertTrue(self.mgr.state.enabled)

    def test_a_changed_file_is_kept_and_listed(self):
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"patched"})
        self.choose("Disable")
        self.mgr.disable()
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").read_bytes(), b"patched")
        self.assertIn("dsound.dll", self.said("warning")[-1])
        self.assertIn("were kept", self.said("warning")[-1])

    def test_the_game_running_blocks_it(self):
        self.running = True
        self.choose("Disable")
        self.mgr.disable()
        self.assertIn("is running", self.said("warning")[-1])
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))
        self.assertTrue(self.mgr.state.enabled)

    def test_a_locked_file_is_reported(self):
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == "dsound.dll":
                raise PermissionError("in use")
            return real_unlink(path, *args, **kwargs)

        self.choose("Disable")
        with mock.patch.object(Path, "unlink", unlink):
            self.mgr.disable()
        self.assertIn("could not be removed", self.said("warning")[-1])
        self.assertIn("in use", self.said("warning")[-1])

    def test_without_a_record_of_the_files_there_is_nothing_to_do(self):
        self.mgr.zip_path.unlink()
        self.mgr.disable()
        self.assertIn("nothing to remove", self.said("information")[-1])
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))

    def test_disable_then_enable_restores_everything_from_the_cached_copy(self):
        self.choose("Disable")
        self.mgr.disable()
        self.mgr.enable()
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))
        self.assertEqual(self.hub.downloads, [])
        self.assertEqual(self.mgr.state.enabled, True)

    def test_the_menu_label_follows_the_state(self):
        self.assertEqual([e[0] for e in self.mgr.menu_entries()],
                         ["Borderlands SDK: Disable", "Borderlands SDK: Check for updates"])
        self.mgr.state.update(enabled=False)
        self.assertEqual(self.mgr.menu_entries()[0][0], "Borderlands SDK: Enable")
        fresh = self.manager()
        fresh.state.update(enabled=None)
        self.assertEqual(fresh.menu_entries()[0][0], "Borderlands SDK: Set up...")


class AutoHeal(Base):
    """Both places are checked at startup, before MO2 starts any program, and after the game exits."""

    def setUp(self):
        super().setUp()
        self.mgr = self.manager()
        self.set_up(self.mgr)
        self.update_check = False
        self.org.modList().names.append(SUPPORT)
        self.org.modList().paths[SUPPORT] = str(self.mod_dir)

    def break_things(self):
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.put(self.game_dir, {"OakGame/Binaries/Win64/Plugins/python.dll": b"corrupted"})
        (self.mod_dir / "sdk_mods/mods_base.sdkmod").unlink()
        self.put(self.mod_dir, {"sdk_mods/__main__.py": b"corrupted"})

    def test_startup_puts_everything_right_without_asking(self):
        self.break_things()
        self.mgr.startup()
        self.assertEqual((self.in_mod(), self.in_game(real_part(V1))), (V1, real_part(V1)))
        self.assertEqual(fakes.DIALOGS, [])
        self.assertIn("restored 2 file(s) in 'SDK Mod Support' at startup", self.log())
        self.assertIn("restored 2 SDK file(s) in the game folder at startup", self.log())
        self.assertEqual(self.mgr.problems(), [])

    def test_before_a_launch_the_real_files_are_right_at_once_and_the_mod_gets_a_refresh(self):
        self.break_things()
        self.mgr.before_launch(r"C:\Apps\Steam\Borderlands 4\OakGame\Binaries\Win64\Borderlands4.exe")
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))
        self.assertIn("before MO2 started Borderlands4.exe", self.log())
        self.assertIn("MO2 sees files restored in the SDK mod from its next refresh", self.log())
        self.assertEqual(len(self.org.data_changed), 1)                          # the refresh that makes MO2 see them

    def test_it_runs_before_any_program_not_only_the_game(self):
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.mgr.before_launch(r"C:\Tools\SomeTool.exe")
        self.assertTrue((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())
        self.assertIn("before MO2 started SomeTool.exe", self.log())

    def test_after_the_game_exits_files_that_were_locked_are_put_right(self):
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.running = True
        self.mgr.before_launch("Borderlands4.exe")
        self.assertFalse((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())     # locked: left for later
        self.assertIn("the game is running, so its SDK files were left as they are", self.log())
        self.running = False
        self.mgr.after_run(r"C:\x\Borderlands4.exe")
        self.assertTrue((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())
        self.assertIn("after the game exited", self.log())

    def test_other_programs_ending_do_not_trigger_a_check(self):
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.mgr.after_run(r"C:\Tools\SomeTool.exe")
        self.assertFalse((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())

    def test_changed_files_are_overwritten_strictly(self):
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"patched by hand"})
        self.mgr.before_launch("x.exe")
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").read_bytes(), b"loader-1")

    def test_when_the_sdk_is_off_stray_real_files_are_removed_and_the_mod_stays(self):
        self.mgr.state.update(enabled=False)
        self.mgr.startup()
        self.assertEqual(self.present(self.game_dir, V1), [])
        self.assertEqual(self.in_mod(), V1)
        self.assertIn("removed 3 SDK file(s) from the game folder at startup: the SDK is switched off", self.log())

    def test_when_the_sdk_is_off_a_changed_real_file_is_left_alone(self):
        self.mgr.state.update(enabled=False)
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"mine"})
        self.mgr.before_launch("x.exe")
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").read_bytes(), b"mine")

    def test_nothing_happens_when_the_sdk_was_never_set_up(self):
        fresh = self.manager()
        fresh.state.update(enabled=None)
        fresh.zip_path.unlink()
        fakes.LOG.clear()
        fresh.before_launch("x.exe")
        fresh.after_run("Borderlands4.exe")
        self.assertEqual(fakes.LOG, [])

    def test_a_healthy_install_is_checked_quietly(self):
        fakes.LOG.clear()
        self.mgr.before_launch("x.exe")
        self.assertEqual(fakes.LOG, [])
        self.assertEqual(self.org.data_changed, [])

    def test_a_second_check_does_not_hash_everything_again(self):
        self.mgr.before_launch("x.exe")
        counted = mock.Mock(sha256=mock.Mock(wraps=hashlib.sha256))
        with mock.patch.object(sdk, "hashlib", counted):
            self.mgr.before_launch("x.exe")
        self.assertEqual(counted.sha256.call_count, 0)                           # remembered per size and time

    def test_files_that_cannot_be_written_are_a_warning_not_a_blocked_launch(self):
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        real_replace = sdk.os.replace

        def replace(src, dst):
            if Path(dst).name == "dsound.dll":
                raise PermissionError("locked")
            return real_replace(src, dst)

        with mock.patch.object(sdk.os, "replace", replace):
            self.mgr.before_launch("x.exe")
        self.assertTrue([m for m in self.warnings() if "could not be put right" in m and "dsound.dll" in m])
        self.assertEqual(self.mgr.problems(), [PROBLEM_FILES])                   # and the notification says so

    def test_a_failure_in_the_check_never_raises(self):
        with mock.patch.object(SdkManager, "heal", side_effect=RuntimeError("boom")):
            self.mgr.before_launch("x.exe")
            self.mgr.after_run("Borderlands4.exe")
        self.assertTrue([m for m in self.warnings() if "could not check the SDK's files before" in m and "boom" in m])

    def test_a_profile_change_checks_too(self):
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.mgr.on_profile_changed()
        self.assertTrue((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())
        self.assertIn("after the profile changed", self.log())

    def test_the_sdk_mod_is_created_if_its_folder_is_gone(self):
        import shutil
        shutil.rmtree(self.mod_dir)
        self.mgr.before_launch("x.exe")
        self.assertEqual(self.in_mod(), V1)

    def test_extra_files_in_the_sdk_mod_are_left_alone(self):
        self.put(self.mod_dir, {"sdk_mods/__pycache__/x.pyc": b"cache"})
        self.mgr.before_launch("x.exe")
        self.assertTrue((self.mod_dir / "sdk_mods/__pycache__/x.pyc").exists())

    def test_the_refresh_is_asked_for_once_per_burst(self):
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        (self.mod_dir / "sdk_mods/mods_base.sdkmod").unlink()
        self.mgr.before_launch("a.exe")
        (self.mod_dir / "sdk_mods/__main__.py").unlink()
        self.mgr.before_launch("b.exe")
        self.assertEqual(self.org.data_changed, [])                              # not from inside the hook
        fakes.QTimer.run_queued()
        self.assertEqual(len(self.org.data_changed), 1)


class Updating(Base):
    def setUp(self):
        super().setUp()
        self.mgr = self.manager()
        self.set_up(self.mgr)
        self.hub.latest = "v0.4"

    def test_no_newer_release_says_so(self):
        self.hub.latest = "v0.3"
        self.mgr.check_updates(manual=True)
        self.assertIn("up to date (v0.3)", self.said("information")[-1])
        self.assertEqual(self.asked(), [])

    def test_a_manual_check_offers_without_the_skip_choice(self):
        self.choose("Not now")
        self.mgr.check_updates(manual=True)
        self.assertEqual(self.asked()[-1][3], ["Update", "Not now"])
        self.assertIn("v0.4", self.asked()[-1][2])

    def test_updating_downloads_first_then_removes_the_old_files_then_installs_the_new(self):
        seen = {}

        def while_downloading():
            seen["old files still there"] = (self.present(self.game_dir, V1) == sorted(real_part(V1))
                                             and self.present(self.mod_dir, V1) == sorted(V1))

        self.hub.while_downloading = while_downloading
        self.choose("Update")
        self.mgr.check_updates(manual=True)
        self.assertTrue(seen["old files still there"])                         # nothing touched before the download
        self.assertEqual(self.in_mod(V2), V2)
        self.assertEqual(self.in_game(real_part(V2)), real_part(V2))
        self.assertFalse((self.mod_dir / "OakGame/Binaries/Win64/Plugins/old_only.dll").exists())     # old-only file gone
        self.assertFalse((self.game_dir / "OakGame/Binaries/Win64/Plugins/old_only.dll").exists())
        self.assertEqual(self.mgr.state.tag, "v0.4")
        self.assertEqual(sdk.read_manifest(self.mgr.zip_path)["sdk_mods/mods_base.sdkmod"].sha256, sha(b"base-2"))
        self.assertFalse(self.mgr.zip_path.with_name("oak2-sdk.zip.new").exists())
        self.assertIn(f"update: removed {len(real_part(V1))} old SDK file(s) from the game folder", self.log())
        self.assertIn(f"update: removed {len(V1)} old file(s) from 'SDK Mod Support'", self.log())
        self.assertIn("SDK updated to v0.4", self.log())
        self.assertEqual(self.mgr.problems(), [])

    def test_a_changed_old_real_file_is_kept_then_asked_about(self):
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"patched by the user"})
        self.choose("Update", "Leave them as they are")
        self.mgr.check_updates(manual=True)
        self.assertEqual((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").read_bytes(), b"patched by the user")
        self.assertIn("dsound.dll", self.asked()[-1][2])
        self.assertEqual(self.mgr.state.tag, "v0.4")

    def test_replacing_the_changed_one_during_the_update(self):
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"patched by the user"})
        self.choose("Update", "Replace them")
        self.mgr.check_updates(manual=True)
        self.assertEqual(self.in_game(real_part(V2)), real_part(V2))

    def test_a_failed_download_touches_nothing(self):
        self.hub.download_error = netio.NetworkError("offline")
        self.choose("Update")
        self.mgr.check_updates(manual=True)
        self.assertEqual((self.in_mod(), self.in_game(real_part(V1))), (V1, real_part(V1)))
        self.assertEqual(self.mgr.state.tag, "v0.3")
        self.assertEqual(sdk.read_manifest(self.mgr.zip_path)["sdk_mods/mods_base.sdkmod"].sha256, sha(b"base-1"))
        self.assertIn("offline", self.said("critical")[-1])

    def test_a_cancelled_download_touches_nothing(self):
        self.hub.download_error = netio.Cancelled()
        self.choose("Update")
        self.mgr.check_updates(manual=True)
        self.assertEqual((self.in_mod(), self.said("critical")), (V1, []))

    def test_a_bad_new_archive_is_refused_before_anything_is_removed(self):
        self.hub.zips["v0.4"] = zip_bytes({"../escape.dll": b"x"})
        self.choose("Update")
        self.mgr.check_updates(manual=True)
        self.assertEqual((self.in_mod(), self.in_game(real_part(V1))), (V1, real_part(V1)))
        self.assertIn("refused", self.said("critical")[-1])
        self.assertEqual(self.mgr.state.tag, "v0.3")

    def test_a_checksum_mismatch_touches_nothing(self):
        self.hub.download_error = netio.ChecksumError("does not match")
        self.choose("Update")
        self.mgr.check_updates(manual=True)
        self.assertEqual(self.in_mod(), V1)

    def test_a_locked_old_file_stops_the_update_with_the_old_copy_and_the_mod_intact(self):
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == "python.dll" and self.game_dir in path.parents:
                raise PermissionError("in use")
            return real_unlink(path, *args, **kwargs)

        self.choose("Update")
        with mock.patch.object(Path, "unlink", unlink):
            self.mgr.check_updates(manual=True)
        self.assertIn("update was stopped", self.said("critical")[-1])
        self.assertEqual(self.mgr.state.tag, "v0.3")
        self.assertEqual(self.in_mod(), V1)                                    # the mod was not touched
        self.assertEqual(sdk.read_manifest(self.mgr.zip_path)["sdk_mods/mods_base.sdkmod"].sha256, sha(b"base-1"))
        self.assertFalse(self.mgr.zip_path.with_name("oak2-sdk.zip.new").exists())
        self.assertEqual(self.mgr.problems(), [PROBLEM_FILES, PROBLEM_UPDATE])

    def test_the_game_running_blocks_an_update_of_an_enabled_sdk(self):
        self.running = True
        self.choose("Update")
        self.mgr.check_updates(manual=True)
        self.assertEqual((self.hub.downloads, self.in_mod()), ([], V1))

    def test_a_disabled_sdk_gets_its_cache_and_mod_updated_but_no_real_files(self):
        self.mgr.state.update(enabled=False)
        self.put(self.game_dir, {})
        for rel in real_part(V1):
            (self.game_dir / rel).unlink()
        self.choose("Update")
        self.mgr.check_updates(manual=True)
        self.assertEqual(self.mgr.state.tag, "v0.4")
        self.assertEqual(self.in_mod(V2), V2)
        self.assertEqual(self.present(self.game_dir, V2), [])                  # still stock
        self.assertIn("switched off", self.said("information")[-1])

    def test_a_manual_check_when_not_set_up_says_how_to_set_up(self):
        fresh = self.manager()
        fresh.state.update(enabled=None, tag="")
        fresh.check_updates(manual=True)
        self.assertIn("not set up yet", self.said("information")[-1])

    def test_a_failed_manual_check_is_reported_and_a_silent_one_is_only_logged(self):
        self.hub.fetch_error = netio.NetworkError("offline")
        self.mgr.check_updates(manual=True)
        self.assertIn("Could not check", self.said("warning")[-1])
        fakes.reset_ui()
        self.mgr.check_updates(manual=False)
        self.assertEqual(self.said("warning"), [])
        self.assertIn("could not check for an SDK update: offline", self.log())

    def test_an_update_that_fails_unexpectedly_is_reported(self):
        self.choose("Update")
        with mock.patch.object(sdk, "purge", side_effect=RuntimeError("boom")):
            self.mgr.check_updates(manual=True)
        self.assertIn("boom", self.said("critical")[-1])


class AtStartup(Base):
    def test_an_update_is_offered_with_skip_and_remembered_when_skipped(self):
        mgr = self.manager()
        self.set_up(mgr)
        self.hub.latest = "v0.4"
        self.choose("Skip this version")
        mgr.startup()
        self.assertEqual(self.asked()[-1][3], ["Update", "Skip this version", "Not now"])
        self.assertEqual(mgr.state.skipped_tag, "v0.4")
        self.assertEqual(mgr.problems(), [])                                    # no notification for a skipped version
        fakes.reset_ui()
        again = self.manager()
        again.startup()
        self.assertEqual(self.asked(), [])                                      # not asked again for v0.4
        self.hub.zips["v0.5"] = zip_bytes(V2)
        self.hub.latest = "v0.5"
        again.startup()
        self.assertEqual(len(self.asked()), 1)                                  # but a newer one is offered

    def test_not_now_leaves_a_notification(self):
        mgr = self.manager()
        self.set_up(mgr)
        self.hub.latest = "v0.4"
        self.choose("Not now")
        mgr.startup()
        self.assertEqual(mgr.problems(), [PROBLEM_UPDATE])
        self.assertIn("v0.4", mgr.short_description(PROBLEM_UPDATE))
        self.assertIn("you have v0.3", mgr.full_description(PROBLEM_UPDATE))

    def test_accepting_updates_straight_away(self):
        mgr = self.manager()
        self.set_up(mgr)
        self.hub.latest = "v0.4"
        self.choose("Update")
        mgr.startup()
        self.assertEqual((self.in_mod(V2), self.in_game(real_part(V2))), (V2, real_part(V2)))

    def test_up_to_date_is_silent(self):
        mgr = self.manager()
        self.set_up(mgr)
        mgr.startup()
        self.assertEqual((fakes.DIALOGS, len(self.hub.fetches)), ([], 1))

    def test_the_setting_can_switch_the_check_off(self):
        mgr = self.manager()
        self.set_up(mgr)
        self.update_check = False
        mgr.startup()
        self.assertEqual((self.hub.fetches, self.async_calls), ([], []))

    def test_a_switched_off_sdk_is_not_checked_for_updates(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        mgr.startup()
        self.assertEqual(self.hub.fetches, [])

    def test_an_offline_check_is_only_logged(self):
        mgr = self.manager()
        self.set_up(mgr)
        self.hub.fetch_error = netio.NetworkError("offline")
        mgr.startup()
        self.assertEqual(fakes.DIALOGS, [])
        self.assertIn("could not check for an SDK update: offline", self.log())

    def test_set_up_is_offered_when_sdk_mods_need_it(self):
        self.add_sdk_mod("Cool Mod")
        mgr = self.manager()
        self.choose("Not now")
        mgr.startup()
        question = self.asked()[-1]
        self.assertEqual(question[3], ["Set up the SDK", "Not now", "Don't ask again"])
        self.assertIn("Cool Mod", question[2])
        self.assertIn("'SDK Mod Support'", question[2])
        self.assertFalse(mgr.state.setup_declined)
        fakes.reset_ui()
        self.manager().startup()                                                # "Not now": asked again next time
        self.assertEqual(len(self.asked()), 1)

    def test_accepting_the_offer_sets_up(self):
        self.add_sdk_mod()
        mgr = self.manager()
        self.choose("Set up the SDK", "Download")
        mgr.startup()
        self.assertEqual((self.in_mod(), self.in_game(real_part(V1))), (V1, real_part(V1)))

    def test_dont_ask_again_is_remembered(self):
        self.add_sdk_mod()
        mgr = self.manager()
        self.choose("Don't ask again")
        mgr.startup()
        self.assertTrue(mgr.state.setup_declined)
        fakes.reset_ui()
        self.manager().startup()
        self.assertEqual(self.asked(), [])

    def test_the_offer_is_not_made_without_sdk_mods_or_for_a_disabled_mod(self):
        self.manager().startup()
        self.add_sdk_mod("Off", active=False)
        self.manager().startup()
        self.assertEqual(self.asked(), [])

    def test_the_real_thread_hands_its_result_back(self):
        mgr = self.manager(run_async=None)
        done, got = threading.Event(), []
        mgr._run_async(lambda: 42, lambda result: (got.append(result), done.set()))
        self.assertTrue(done.wait(5))
        self.assertEqual(got, [42])
        done.clear()
        mgr._run_async(lambda: 1 / 0, lambda result: (got.append(result), done.set()))
        self.assertTrue(done.wait(5))
        self.assertIsInstance(got[-1], ZeroDivisionError)


class UsvfsHandling(Base):
    """The usvfs manager is separate; the SDK manager starts it, shows its problem and its menu entries."""

    def setUp(self):
        super().setUp()
        self.stub = StubUsvfs()
        self.mgr = self.manager(usvfs_manager=self.stub)

    def test_it_is_started_first_and_a_failure_in_it_does_not_stop_the_rest(self):
        self.set_up(self.mgr)
        self.update_check = False
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.stub.startup_error = RuntimeError("boom")
        self.mgr.startup()
        self.assertEqual(self.stub.started, 1)
        self.assertTrue([m for m in self.warnings() if "could not check usvfs at startup" in m])
        self.assertTrue((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())      # the SDK check still ran

    def test_an_unconfigured_sdk_says_so_in_the_log_instead_of_staying_silent(self):
        self.add_sdk_mod("Cool Mod")
        self.mgr.state.update(setup_declined=True)
        self.mgr.startup()
        self.assertEqual(fakes.DIALOGS, [])
        text = self.log()
        self.assertIn("SDK support is on but the SDK is not set up", text)
        self.assertIn("set-up offer was declined", text)
        self.assertIn("Tools > Borderlands SDK: Set up...", text)

    def test_nothing_else_is_asked_once_a_restart_of_mo2_has_been_requested(self):
        self.set_up(self.mgr)
        self.update_check = True
        self.mgr.state.update(enabled=True, tag="v0.3")
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.stub.restarting = True
        self.mgr.startup()
        self.assertEqual(self.stub.started, 1)
        self.assertFalse((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())     # left for the next session
        self.assertEqual(self.hub.fetches, [])
        self.assertEqual(fakes.DIALOGS, [])

    def test_its_problem_shows_once_the_sdk_or_sdk_mods_are_in_play(self):
        self.stub.problem = ("usvfs 0.5.6.1 is too old", "MO2's usvfs is 0.5.6.1.")
        self.assertEqual(self.mgr.problems(), [])                               # no SDK, no SDK mods: not our business
        self.add_sdk_mod("Cool Mod")
        self.assertIn(PROBLEM_USVFS, self.mgr.problems())
        self.assertEqual(self.mgr.short_description(PROBLEM_USVFS), "usvfs 0.5.6.1 is too old")
        self.assertEqual(self.mgr.full_description(PROBLEM_USVFS), "MO2's usvfs is 0.5.6.1.")

    def test_it_shows_for_a_configured_sdk_even_without_sdk_mods(self):
        self.stub.problem = ("restart", "x")
        self.set_up(self.mgr)
        self.assertIn(PROBLEM_USVFS, self.mgr.problems())

    def test_it_goes_away_when_usvfs_is_fine(self):
        self.add_sdk_mod("Cool Mod")
        self.stub.problem = None
        self.assertNotIn(PROBLEM_USVFS, self.mgr.problems())
        self.assertEqual(self.mgr.short_description(PROBLEM_USVFS), "usvfs")

    def test_its_fix_goes_to_the_usvfs_manager(self):
        self.mgr.fix(PROBLEM_USVFS)
        self.assertEqual(self.stub.fixed, 1)

    def test_its_menu_entries_follow_the_sdk_ones(self):
        self.stub.entries = [("Borderlands usvfs: Upgrade to 0.5.7.2", "tip", lambda: None)]
        labels = [e[0] for e in self.mgr.menu_entries()]
        self.assertEqual(labels, ["Borderlands SDK: Set up...", "Borderlands SDK: Check for updates",
                                  "Borderlands usvfs: Upgrade to 0.5.7.2"])

    def test_without_a_usvfs_manager_nothing_changes(self):
        mgr = self.manager()
        self.add_sdk_mod("Cool Mod")
        self.assertEqual(mgr.problems(), [PROBLEM_NOT_ACTIVE])
        self.assertEqual(len(mgr.menu_entries()), 2)


class Problems(Base):
    def test_sdk_mods_without_the_sdk(self):
        self.add_sdk_mod("Cool Mod")
        mgr = self.manager()
        self.assertEqual(mgr.problems(), [PROBLEM_NOT_ACTIVE])
        self.assertIn("not set up", mgr.short_description(PROBLEM_NOT_ACTIVE))
        self.assertIn("Cool Mod", mgr.full_description(PROBLEM_NOT_ACTIVE))
        mgr.state.update(enabled=False)
        self.assertIn("switched off", mgr.short_description(PROBLEM_NOT_ACTIVE))
        self.assertEqual(mgr.problems(), [PROBLEM_NOT_ACTIVE])

    def test_nothing_to_warn_about_without_sdk_mods(self):
        self.add_sdk_mod("Plain", files=("OakGame/Content/Paks/a.pak",))
        self.add_sdk_mod("Off", active=False)
        self.assertEqual(self.manager().problems(), [])

    def test_an_enabled_complete_sdk_has_no_problem(self):
        self.add_sdk_mod()
        mgr = self.manager()
        self.set_up(mgr)
        self.assertEqual(mgr.problems(), [])

    def test_the_sdks_own_mod_and_files_do_not_count_as_sdk_mods(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        self.mods.append((SUPPORT, self.mod_dir))                               # the plugin's own mod, full of SDK files
        self.add_sdk_mod("SDK as a mod", files=("sdk_mods/mods_base.sdkmod", "sdk_mods/keybinds/__init__.py"))
        self.assertEqual(mgr.sdk_mods(), [])
        self.assertEqual(mgr.problems(), [])

    def test_the_sdk_mod_is_excluded_by_name_even_before_the_archive_is_known(self):
        mgr = self.manager()
        self.put(self.mod_dir, {"sdk_mods/unknown_name.sdkmod": b"x"})
        self.mods.append((SUPPORT, self.mod_dir))
        self.assertEqual(mgr.sdk_mods(), [])

    def test_fix_actions(self):
        mgr = self.manager()
        with mock.patch.object(mgr, "enable") as enable, mock.patch.object(mgr, "update") as update:
            mgr.fix(PROBLEM_NOT_ACTIVE)
            mgr.fix(PROBLEM_FILES)
            mgr.release = sdk.Release("v9", "oak2-sdk.zip", 1, "", "https://x", "https://x")
            mgr.fix(PROBLEM_UPDATE)
        self.assertEqual([c.kwargs for c in enable.call_args_list], [{"repair": False}, {"repair": True}])
        update.assert_called_once_with(mgr.release)

    def test_a_changed_file_is_listed_with_where_it_is(self):
        mgr = self.manager()
        self.set_up(mgr)
        self.put(self.game_dir, {"OakGame/Binaries/Win64/dsound.dll": b"patched"})
        self.put(self.mod_dir, {"sdk_mods/__main__.py": b"edited"})
        mgr.refresh()
        text = mgr.full_description(PROBLEM_FILES)
        self.assertIn("dsound.dll (changed in the game folder)", text)
        self.assertIn("sdk_mods/__main__.py (changed in 'SDK Mod Support')", text)
        self.assertIn("v0.3", text)

    def test_files_left_in_the_game_folder_after_switching_off_are_a_problem(self):
        mgr = self.manager()
        self.set_up(mgr, enabled=False)
        self.put(self.game_dir, real_part(V1))                                  # strays
        mgr.refresh()
        self.assertEqual(mgr.problems(), [PROBLEM_FILES])
        self.assertIn("(still in the game folder)", mgr.full_description(PROBLEM_FILES))

    def test_long_lists_are_trimmed(self):
        for i in range(12):
            self.add_sdk_mod(f"Mod {i:02d}")
        text = self.manager().full_description(PROBLEM_NOT_ACTIVE)
        self.assertIn("and 4 more", text)


class PluginWiring(Base):
    def setUp(self):
        super().setUp()
        self.org.settings[("Borderlands Tools", "sdk_support")] = True          # off by default: see SdkSwitch
        self.plugin = BorderlandsTools()
        self.plugin._make_usvfs = lambda: None                                  # the real one reads MO2's own folder
        self.diag = SdkDiagnose(self.plugin)
        self.plugin.diagnose = self.diag
        self.plugin.init(self.org)
        self.diag.init(self.org)
        self.plugin.setParentWidget(None)
        self.mgr = self.plugin._sdk_manager()
        self.mgr._fetch, self.mgr._download = self.hub.fetch, self.hub.download
        self.mgr._game_running = lambda binary: self.running
        self.mgr._run_async = lambda work, callback: callback(work())

    def test_the_notifications_are_a_plugin_of_their_own_with_a_fix_for_every_problem(self):
        self.assertIsInstance(self.diag, mobase.IPluginDiagnose)
        self.assertNotIsInstance(self.plugin, mobase.IPluginDiagnose)           # the tools plugin stays a plain tool
        self.assertTrue(all(self.diag.hasGuidedFix(k) for k in (0, 1, 2, 3)))
        self.assertEqual(self.diag.name(), "Borderlands SDK Notifications")
        (kind, names), = self.diag.requirements()
        self.assertEqual(names, {"Borderlands 4"})                             # only games that have an SDK
        self.assertEqual(self.diag.settings(), [])

    def test_active_problems_come_from_the_manager(self):
        self.assertEqual(self.diag.activeProblems(), [])
        self.mgr._active_mods = lambda: [("Cool Mod", self.add_sdk_mod("Cool Mod", active=False))]
        self.assertEqual(self.diag.activeProblems(), [PROBLEM_NOT_ACTIVE])
        self.assertIn("not set up", self.diag.shortDescription(PROBLEM_NOT_ACTIVE))
        self.assertIn("Cool Mod", self.diag.fullDescription(PROBLEM_NOT_ACTIVE))

    def test_the_guided_fix_sets_the_sdk_up(self):
        self.choose("Download")
        self.diag.startGuidedFix(PROBLEM_NOT_ACTIVE)
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))

    def test_a_failure_computing_problems_never_breaks_mo2(self):
        with mock.patch.object(SdkManager, "problems", side_effect=RuntimeError("boom")):
            self.assertEqual(self.diag.activeProblems(), [])
        self.assertTrue([m for lvl, m in fakes.LOG if "SDK notifications" in m])

    def test_games_without_an_sdk_have_no_problems_or_manager(self):
        org = fakes.FakeOrganizer()
        game = BorderlandsGame(BORDERLANDS_3)
        game.init(org)
        org.managed = game
        plugin = BorderlandsTools()
        diag = SdkDiagnose(plugin)
        plugin.diagnose = diag
        plugin.init(org)
        self.assertEqual((diag.activeProblems(), plugin._sdk_manager(), diag.shortDescription(0), diag.fullDescription(0)),
                         ([], None, "", ""))
        diag.startGuidedFix(0)                                                  # nothing to do, no error

    def test_installing_an_sdk_mod_without_the_sdk_warns_in_the_log_and_refreshes_the_notification(self):
        folder = self.add_sdk_mod("New Mod", active=False)
        fakes.LOG.clear()
        before = self.diag.invalidations
        self.org.installed_callbacks[-1](fakes.FakeMod(folder, name="New Mod"))
        warnings = self.warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("'New Mod' is an SDK mod, but the SDK is not set up", warnings[0])
        self.assertGreater(self.diag.invalidations, before)

    def test_the_warning_says_switched_off_when_it_is(self):
        self.mgr.state.update(enabled=False)
        folder = self.add_sdk_mod("New Mod", active=False)
        self.org.installed_callbacks[-1](fakes.FakeMod(folder, name="New Mod"))
        self.assertIn("the SDK is switched off", self.log())

    def test_no_warning_when_the_sdk_is_on_or_the_mod_is_not_an_sdk_mod(self):
        self.mgr.state.update(enabled=True)
        folder = self.add_sdk_mod("Fine", active=False)
        self.org.installed_callbacks[-1](fakes.FakeMod(folder, name="Fine"))
        self.mgr.state.update(enabled=False)
        plain = self.add_sdk_mod("Plain", active=False, files=("OakGame/x.pak",))
        self.org.installed_callbacks[-1](fakes.FakeMod(plain, name="Plain"))
        self.assertEqual(self.warnings(), [])

    def test_the_sdk_mod_itself_never_triggers_the_warning(self):
        folder = self.add_sdk_mod(SUPPORT, active=False)
        self.org.installed_callbacks[-1](fakes.FakeMod(folder, name=SUPPORT))
        self.assertEqual(self.warnings(), [])

    def test_the_warning_waits_until_the_game_plugin_has_placed_the_files(self):
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        folder = self.add_sdk_mod("New Mod", active=False)
        self.org.installed_callbacks[-1](fakes.FakeMod(folder, name="New Mod"))
        self.assertEqual(self.warnings(), [])                                   # not from inside the callback
        fakes.QTimer.run_queued()
        self.assertEqual(len(self.warnings()), 1)

    def test_mod_changes_refresh_the_notification(self):
        before = self.diag.invalidations
        self.org.modList().state_callbacks[-1]({"x": 0})
        self.org.modList().removed_callbacks[-1]("x")
        self.assertEqual(self.diag.invalidations, before + 2)

    def test_an_invalidation_failure_is_swallowed(self):
        self.diag._invalidate = mock.Mock(side_effect=RuntimeError("no mo2"))
        self.diag.invalidate()                                                  # must not raise
        self.plugin.diagnose = None
        self.plugin._notify()                                                   # nor without the plugin

    def test_startup_checks_the_sdk(self):
        self.mgr.state.update(enabled=True, tag="v0.3")
        self.org.ui_callbacks[-1](object())
        self.assertEqual(len(self.hub.fetches), 1)

    def test_startup_waits_for_mo2s_event_loop_and_window(self):
        """MO2 calls the callback inside its main window's constructor; a prompt or restart there strands MO2."""
        self.mgr.state.update(enabled=True, tag="v0.3")
        window = mock.Mock()
        window.isVisible.return_value = False                                   # constructing: not shown yet
        fakes.QThread.level[0] = 0                                              # ...and exec() has not started
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        self.addCleanup(fakes.QThread.level.__setitem__, 0, 1)
        self.org.ui_callbacks[-1](window)
        for _ in range(3):
            fakes.QTimer.queue.pop(0)()                                         # the timer fires, nothing is ready
        self.assertEqual(self.hub.fetches, [])
        self.assertEqual(fakes.DIALOGS, [])
        window.isVisible.return_value = True
        fakes.QTimer.queue.pop(0)()                                             # visible but still before exec()
        self.assertEqual(self.hub.fetches, [])
        fakes.QThread.level[0] = 1
        fakes.QTimer.queue.pop(0)()                                             # the real loop: now it runs
        self.assertEqual(len(self.hub.fetches), 1)

    def test_a_startup_failure_never_breaks_mo2(self):
        with mock.patch.object(SdkManager, "startup", side_effect=RuntimeError("boom")):
            self.org.ui_callbacks[-1](object())
        self.assertTrue([m for lvl, m in fakes.LOG if "at startup" in m])

    def test_the_setting_is_read_for_the_update_check(self):
        self.mgr.state.update(enabled=True, tag="v0.3")
        self.org.settings[("Borderlands Tools", "sdk_update_check")] = False
        self.mgr.startup()
        self.assertEqual(self.hub.fetches, [])
        self.org.settings[("Borderlands Tools", "sdk_update_check")] = "true"
        self.mgr.startup()
        self.assertEqual(len(self.hub.fetches), 1)

    # -- the launch hooks
    def test_the_hooks_are_registered_and_never_block_a_launch(self):
        self.assertTrue(self.org.about_to_run_callbacks[-1]("x.exe"))
        self.assertTrue(self.org.about_to_run_callbacks[-1]("x.exe", object(), "args"))
        with mock.patch.object(SdkManager, "before_launch", side_effect=RuntimeError("boom")):
            self.assertTrue(self.org.about_to_run_callbacks[-1]("x.exe"))
        self.assertTrue([m for m in self.warnings() if "before a launch" in m])

    def test_the_hook_heals_the_real_files_before_a_launch(self):
        self.set_up(self.mgr)
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.org.about_to_run_callbacks[-1]("Borderlands4.exe")
        self.assertTrue((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())

    def test_the_hook_is_a_no_op_for_games_without_an_sdk(self):
        org = fakes.FakeOrganizer()
        game = BorderlandsGame(BORDERLANDS_3)
        game.init(org)
        org.managed = game
        BorderlandsTools().init(org)
        self.assertTrue(org.about_to_run_callbacks[-1]("x.exe"))

    def test_finished_run_and_profile_change_hooks(self):
        self.set_up(self.mgr)
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.org.finished_callbacks[-1]("Borderlands4.exe", 0)
        self.assertTrue((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.org.profile_callbacks[-1]("old", "new")
        self.assertTrue((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())
        with mock.patch.object(SdkManager, "after_run", side_effect=RuntimeError("boom")):
            self.org.finished_callbacks[-1]("x.exe", 0)
        with mock.patch.object(SdkManager, "on_profile_changed", side_effect=RuntimeError("boom")):
            self.org.profile_callbacks[-1]("a", "b")
        self.assertEqual(len([m for m in self.warnings() if "boom" in m]), 2)

    def test_the_default_usvfs_manager_is_built_with_the_real_class(self):
        from borderlands.usvfsmanager import UsvfsManager

        plugin = BorderlandsTools()
        plugin.init(self.org)
        mgr = plugin._sdk_manager()
        self.assertIsInstance(mgr.usvfs, UsvfsManager)


class SdkSwitch(Base):
    """The SDK functions are off by default (plugin setting ``sdk_support``); usvfs handling stays on."""

    def setUp(self):
        super().setUp()
        self.stub = StubUsvfs()
        self.stub.entries = [("Borderlands usvfs: Upgrade", "tip", lambda: None)]
        self.off = self.manager(usvfs_manager=self.stub, sdk_active=lambda: False)
        self.add_sdk_mod("Cool Mod")

    def test_it_is_on_unless_the_setting_is_turned_off(self):
        plugin = BorderlandsTools()
        plugin.init(self.org)
        self.assertTrue(plugin._sdk_manager().active)                           # never written: the default
        self.org.settings[("Borderlands Tools", "sdk_support")] = False
        self.assertFalse(plugin._sdk_manager().active)
        self.org.settings[("Borderlands Tools", "sdk_support")] = "false"
        self.assertFalse(plugin._sdk_manager().active)
        self.org.settings[("Borderlands Tools", "sdk_support")] = "true"
        self.assertTrue(plugin._sdk_manager().active)
        self.assertEqual({s.key: s.default for s in plugin.settings()}["sdk_support"], True)

    def test_startup_only_runs_usvfs_and_asks_nothing_about_the_sdk(self):
        self.off.startup()
        self.assertEqual(self.stub.started, 1)
        self.assertEqual(fakes.DIALOGS, [])
        self.assertEqual(self.hub.fetches, [])
        self.assertFalse(self.off.state.configured)

    def test_no_notifications_launch_hooks_or_install_warnings(self):
        self.assertEqual(self.off.problems(), [])
        self.set_up(self.off)
        (self.game_dir / "OakGame/Binaries/Win64/dsound.dll").unlink()
        self.off.before_launch("Borderlands4.exe")
        self.off.after_run("Borderlands4.exe")
        self.off.on_profile_changed()
        self.assertFalse((self.game_dir / "OakGame/Binaries/Win64/dsound.dll").exists())
        self.off.on_mod_installed("Cool Mod", self.mods_dir / "Cool Mod")
        self.assertEqual(self.warnings(), [])

    def test_only_the_usvfs_menu_entries_remain(self):
        self.assertEqual([e[0] for e in self.off.menu_entries()], ["Borderlands usvfs: Upgrade"])
        self.assertEqual(self.manager(sdk_active=lambda: False).menu_entries(), [])
        on = self.manager(usvfs_manager=self.stub)
        self.assertGreater(len(on.menu_entries()), 1)

    def test_a_failing_switch_counts_as_off(self):
        def boom():
            raise RuntimeError("no settings")

        self.assertFalse(self.manager(sdk_active=boom).active)


class MenuEntries(Base):
    def setUp(self):
        super().setUp()
        self.org.settings[("Borderlands Tools", "sdk_support")] = True
        self.plugin = BorderlandsTools()
        self.plugin._make_usvfs = lambda: None
        self.plugin.init(self.org)
        self.plugin.setParentWidget(None)
        self.menu = fakes.QMenu()
        self.menu.aboutToShow.connect(lambda: (self.menu.clear(), self.menu.addAction(fakes.QAction("Borderlands: Choose a tool..."))))
        action = fakes.QAction("Tools")
        action._menu = self.menu
        window = mock.Mock()
        window.findChild.side_effect = lambda cls, name: action if name == "actionTool" else None
        self.mgr = self.plugin._sdk_manager()
        self.mgr._fetch, self.mgr._download = self.hub.fetch, self.hub.download
        self.mgr._game_running = lambda binary: self.running
        self.org.ui_callbacks[-1](window)

    def texts(self):
        self.menu.show()
        return [a.text() for a in self.menu.actions() if not a.separator]

    def entry(self, text):
        self.menu.show()
        return next(a for a in self.menu.actions() if a.text() == text)

    def test_the_sdk_entries_follow_the_state(self):
        self.assertEqual(self.texts()[1:], ["Borderlands SDK: Set up...", "Borderlands SDK: Check for updates"])
        self.mgr.state.update(enabled=True)
        self.assertEqual(self.texts()[1], "Borderlands SDK: Disable")
        self.mgr.state.update(enabled=False)
        self.assertEqual(self.texts()[1], "Borderlands SDK: Enable")

    def test_they_come_after_the_tool_entries(self):
        folder = self.mods_dir / "PhaseLoot"
        (folder / "Tools/P").mkdir(parents=True)
        (folder / "Tools/P/p.exe").write_text("x")
        self.mods.append(("PhaseLoot", folder))
        self.mgr._active_mods = lambda: list(self.mods)
        self.plugin._active_mods = lambda: list(self.mods)
        self.assertEqual(self.texts(), ["Borderlands: Choose a tool...", "Borderlands: p", "Borderlands SDK: Set up...",
                                        "Borderlands SDK: Check for updates"])

    def test_a_divider_sits_between_the_mods_programs_and_the_support_entries(self):
        folder = self.mods_dir / "PhaseLoot"
        (folder / "Tools/P").mkdir(parents=True)
        (folder / "Tools/P/p.exe").write_text("x")
        self.mods.append(("PhaseLoot", folder))
        self.mgr._active_mods = lambda: list(self.mods)
        self.plugin._active_mods = lambda: list(self.mods)
        self.menu.show()
        layout = ["|" if a.separator else a.text() for a in self.menu.actions()]
        self.assertEqual(layout, ["Borderlands: Choose a tool...", "|", "Borderlands: p", "|",
                                  "Borderlands SDK: Set up...", "Borderlands SDK: Check for updates"])

    def test_without_programs_there_is_only_the_one_divider(self):
        self.menu.show()
        layout = ["|" if a.separator else a.text() for a in self.menu.actions()]
        self.assertEqual(layout, ["Borderlands: Choose a tool...", "|", "Borderlands SDK: Set up...",
                                  "Borderlands SDK: Check for updates"])

    def test_set_up_runs_the_setup(self):
        self.choose("Download")
        self.entry("Borderlands SDK: Set up...").triggered.emit(False)
        self.assertEqual(self.in_game(real_part(V1)), real_part(V1))

    def test_disable_runs_the_disable(self):
        self.set_up(self.mgr)
        self.choose("Disable")
        self.entry("Borderlands SDK: Disable").triggered.emit(False)
        self.assertEqual(self.present(self.game_dir, V1), [])

    def test_check_for_updates_runs_a_manual_check(self):
        self.set_up(self.mgr)
        self.entry("Borderlands SDK: Check for updates").triggered.emit(False)
        self.assertIn("up to date", self.said("information")[-1])

    def test_usvfs_entries_appear_when_the_manager_has_them(self):
        stub = StubUsvfs()
        ran = []
        stub.entries = [("Borderlands usvfs: Upgrade to 0.5.7.2", "tip", lambda: ran.append(1))]
        self.mgr.usvfs = stub
        self.assertEqual(self.texts()[-1], "Borderlands usvfs: Upgrade to 0.5.7.2")
        self.entry("Borderlands usvfs: Upgrade to 0.5.7.2").triggered.emit(False)
        self.assertEqual(ran, [1])

    def test_games_without_an_sdk_get_no_sdk_entries(self):
        org = fakes.FakeOrganizer()
        game = BorderlandsGame(BORDERLANDS_3)
        game.init(org)
        org.managed = game
        plugin = BorderlandsTools()
        plugin.init(org)
        menu = fakes.QMenu()
        action = fakes.QAction("Tools")
        action._menu = menu
        window = mock.Mock()
        window.findChild.side_effect = lambda cls, name: action
        org.ui_callbacks[-1](window)
        menu.show()
        self.assertEqual([a.text() for a in menu.actions()], [])


class ModFlag(Base):
    def test_only_borderlands_4_registers_the_flag(self):
        self.assertTrue(any(isinstance(args[1], sdkcontent.SdkModContent) for args in self.org._features.registered))
        org = fakes.FakeOrganizer()
        game = BorderlandsGame(BORDERLANDS_3)
        game.init(org)
        (three,) = [args[1] for args in org._features.registered if isinstance(args[1], sdkcontent.SdkModContent)]
        self.assertEqual([c.name for c in three.getAllContents()], ["Pak mod"])      # no SDK, so no SDK flag
        old = fakes.FakeOrganizer()
        BorderlandsGame(BORDERLANDS_2).init(old)                                     # Borderlands 2: no SDK, no paks
        self.assertFalse(any(isinstance(args[1], sdkcontent.SdkModContent) for args in old._features.registered))

    def test_the_icon_is_mo2s_settings_button(self):
        (content,) = sdkcontent.SdkModContent().getAllContents()
        self.assertEqual((content.id, content.name, content.icon), (1, "SDK mod", ":/MO/gui/settings"))

    def test_pak_mods_get_the_package_icon_and_mods_with_both_get_both(self):
        feature = sdkcontent.SdkModContent(pak_suffixes=(".pak", ".utoc", ".ucas"))
        sdk_icon, pak_icon = feature.getAllContents()
        self.assertEqual((pak_icon.id, pak_icon.name, pak_icon.icon), (2, "Pak mod", ":/MO/gui/package"))
        pak = FakeTree(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("Cool_P.PAK")])])]))
        sdk_only = FakeTree(E("sdk_mods", True, [E("thing.sdkmod")]))
        both = FakeTree(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("a.utoc")])])]),
                        E("sdk_mods", True, [E("thing.sdkmod")]))
        neither = FakeTree(E("OakGame", True, [E("Binaries", True, [E("x.dll")])]))
        self.assertEqual([feature.getContentsFor(t) for t in (pak, sdk_only, both, neither)],
                         [[2], [1], [1, 2], []])

    def test_a_pak_in_a_nested_folder_counts_and_a_folder_named_like_one_does_not(self):
        feature = sdkcontent.SdkModContent(pak_suffixes=(".pak",))
        nested = FakeTree(E("a", True, [E("b", True, [E("deep.pak")])]))
        folder = FakeTree(E("looks.pak", True, [E("readme.txt")]))
        self.assertEqual((feature.getContentsFor(nested), feature.getContentsFor(folder)), ([2], []))

    def test_mods_holding_sdk_mods_are_flagged(self):
        feature = sdkcontent.SdkModContent()
        yes = FakeTree(E("sdk_mods", True, [E("thing.sdkmod")]))
        no = FakeTree(E("OakGame", True, [E("x.pak")]))
        self.assertEqual((feature.getContentsFor(yes), feature.getContentsFor(no)), ([1], []))

    def test_the_sdks_own_files_are_not_flagged_once_the_archive_is_known(self):
        mgr = self.manager()
        self.set_up(mgr)                                                         # loads the manifest, so the base names
        feature = sdkcontent.SdkModContent()
        own = FakeTree(E("sdk_mods", True, [E("mods_base.sdkmod"), E("keybinds", True, [E("__init__.py")])]))
        mine = FakeTree(E("sdk_mods", True, [E("mods_base.sdkmod"), E("mine.sdkmod")]))
        self.assertEqual((feature.getContentsFor(own), feature.getContentsFor(mine)), ([], [1]))

    def column_window(self, hidden=True):
        window = mock.Mock()
        header = window.findChild.return_value.header.return_value
        header.isSectionHidden.return_value = hidden
        return window, header

    def test_the_content_column_is_shown_once_so_the_flag_is_visible(self):
        marker = self.root / "ui_state.json"
        window, header = self.column_window()
        self.assertTrue(sdkcontent.show_column_once(window, marker))
        header.setSectionHidden.assert_called_once_with(3, False)               # ModList::COL_CONTENT
        self.assertIn("Content column", self.log())
        header.setSectionHidden.reset_mock()
        self.assertFalse(sdkcontent.show_column_once(window, marker))           # the user may hide it again
        header.setSectionHidden.assert_not_called()

    def titled_window(self, titles, hidden=True):
        window, header = self.column_window(hidden)
        header.count.return_value = len(titles)
        window.findChild.return_value.model.return_value.headerData.side_effect = lambda section, orientation: titles[section]
        return window, header

    def test_the_column_is_found_by_its_header_text_wherever_it_sits(self):
        window, header = self.titled_window(["Mod Name", "Content", "Conflicts", "Flags", "Category"])
        sdkcontent.show_column_once(window, self.root / "ui_state.json")
        header.setSectionHidden.assert_called_once_with(1, False)               # reordered: not position 3

    def test_an_unrecognised_header_falls_back_to_the_usual_position(self):
        for titles in (["Mod Name", "Inhalt", "Konflikte", "Flags"], ["a", "b"]):
            window, header = self.titled_window(titles)
            self.assertEqual(sdkcontent.content_section(window.findChild.return_value, header), 3)

    def test_a_model_that_cannot_be_asked_falls_back_too(self):
        window, header = self.column_window()
        window.findChild.return_value.model.side_effect = RuntimeError("gone")
        self.assertEqual(sdkcontent.content_section(window.findChild.return_value, header), 3)

    def test_a_column_that_is_already_shown_is_left_alone_and_remembered(self):
        marker = self.root / "ui_state.json"
        window, header = self.column_window(hidden=False)
        sdkcontent.show_column_once(window, marker)
        header.setSectionHidden.assert_not_called()
        self.assertTrue(marker.exists())

    def test_no_mod_list_view_or_a_failure_changes_nothing_and_never_raises(self):
        marker = self.root / "ui_state.json"
        self.assertFalse(sdkcontent.show_column_once(object(), marker))
        window, header = self.column_window()
        header.setSectionHidden.side_effect = RuntimeError("deleted widget")
        self.assertFalse(sdkcontent.show_column_once(window, marker))
        self.assertFalse(marker.exists())                                       # tried again next start
        self.assertIn("could not show the Content column", self.log())

    def test_startup_shows_the_column_only_when_the_sdk_functions_are_on(self):
        self.org.settings[("Borderlands Tools", "sdk_support")] = True
        plugin = BorderlandsTools()
        plugin._make_usvfs = lambda: None
        plugin.init(self.org)
        window, header = self.column_window()
        self.org.ui_callbacks[-1](window)
        header.setSectionHidden.assert_called_once_with(3, False)
        off = fakes.FakeOrganizer()
        off.settings[("Borderlands Tools", "sdk_support")] = False
        off.base_path = str(self.root / "fresh instance")                      # no marker yet: only the switch decides
        game = BorderlandsGame(BORDERLANDS_4)
        game.init(off)
        off.managed = game
        other = BorderlandsTools()
        other._make_usvfs = lambda: None
        other.init(off)
        window2, header2 = self.column_window()
        off.ui_callbacks[-1](window2)
        header2.setSectionHidden.assert_not_called()

    def test_the_base_names_are_cleared_when_the_cached_copy_goes(self):
        mgr = self.manager()
        self.set_up(mgr)
        self.assertIn("mods_base.sdkmod", sdkcontent.SdkModContent.base)
        mgr.zip_path.unlink()
        mgr.manifest()
        self.assertEqual(sdkcontent.SdkModContent.base, frozenset())


if __name__ == "__main__":
    unittest.main()
