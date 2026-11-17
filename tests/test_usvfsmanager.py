"""The usvfs manager: startup check, upgrade, revert, restart offer, notification text, menu entries."""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests import fakes

fakes.install()

from borderlands import netio, usvfs  # noqa: E402
from borderlands.usvfsmanager import UsvfsManager  # noqa: E402

NAMES = usvfs.FILES


def contents(tag):
    return {name: f"{tag}:{name}".encode() for name in NAMES}


class Base(unittest.TestCase):
    def setUp(self):
        fakes.LOG.clear()
        fakes.reset_ui()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "MO2"
        self.root.mkdir()
        for name, data in contents("old").items():
            (self.root / name).write_bytes(data)
        self.org = fakes.FakeOrganizer()
        self.org.base_path = str(base / "instances" / "Borderlands 4")
        self.status = usvfs.Status("0.5.6.1", "0.5.6.1")
        self.restarts, self.changes, self.fetches = [], [], []
        self.fetch_error = None
        self.reader_version = "0.5.6.1"
        self.window = mock.Mock()

    def manager(self, **over):
        def fetch(release, workdir, download, progress):
            self.fetches.append(release.version)
            if self.fetch_error:
                raise self.fetch_error
            stage = Path(workdir) / "new"
            stage.mkdir(parents=True, exist_ok=True)
            for name, data in contents("new").items():
                (stage / name).write_bytes(data)
            progress(1, 2)
            return {name: stage / name for name in NAMES}

        kwargs = dict(root=self.root, on_change=lambda: self.changes.append(1), status_fn=lambda: self.status,
                      fetch_fn=fetch, restart_fn=lambda: self.restarts.append(1), reader=lambda p: self.reader_version)
        kwargs.update(over)
        return UsvfsManager(self.org, lambda: self.window, **kwargs)

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

    def read(self, name):
        return (self.root / name).read_bytes()

    def files(self):
        return {n: self.read(n) for n in NAMES}


class Startup(Base):
    def test_an_old_usvfs_is_warned_about_and_the_upgrade_offered(self):
        self.choose("Not now")
        self.manager().startup()
        (warning,) = self.warnings()
        self.assertIn("usvfs 0.5.6.1 is older than 0.5.7.1", warning)
        self.assertIn("0.5.7.2", warning)
        (question,) = self.asked()
        self.assertEqual(question[3], ["Upgrade usvfs", "Not now", "Don't ask again"])
        text = question[2]
        self.assertIn("version 0.5.6.1", text)
        self.assertIn("0.5.7.1 or newer", text)
        self.assertIn("usvfs 0.5.7.2", text)
        self.assertIn("github.com/ModOrganizer2/usvfs", text)
        self.assertIn(str(self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip"), text)   # where the backup will go
        self.assertIn("unlikely", text)
        self.assertIn("other games", text)                                      # the warning about shared library
        self.assertIn("restarted", text)
        self.assertEqual(self.files(), contents("old"))                         # "Not now" changes nothing
        self.assertEqual(self.fetches, [])

    def test_accepting_backs_up_first_replaces_the_files_and_offers_a_restart(self):
        self.choose("Upgrade usvfs", "Restart now")
        self.manager().startup()
        self.assertEqual(self.files(), contents("new"))
        with zipfile.ZipFile(self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip") as z:
            self.assertEqual({n: z.read(n) for n in z.namelist()}, contents("old"))
        self.assertEqual(self.fetches, ["0.5.7.2"])
        warnings = self.warnings()
        self.assertEqual(len(warnings), 2)                                      # at startup, then at the upgrade
        self.assertIn("upgrading it to 0.5.7.2", warnings[1])
        self.assertIn(str(self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip"), warnings[1])   # the backup path is in the log
        self.assertIn("usvfs 0.5.7.2 replaced 0.5.6.1", self.log())
        self.assertEqual(self.restarts, [1])
        self.assertIn("Restart Mod Organizer 2 now", self.asked()[-1][2])
        self.assertEqual(self.asked()[-1][3], ["Restart now", "Later"])
        self.assertEqual(self.changes[-1:], [1])
        self.assertTrue(all(p._closed for p in fakes.PROGRESS))

    def test_the_backup_exists_before_the_download_starts(self):
        seen = {}

        def fetch(release, workdir, download, progress):
            seen["backup"] = (self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip").exists()
            raise netio.NetworkError("offline")

        self.choose("Upgrade usvfs")
        self.manager(fetch_fn=fetch).startup()
        self.assertTrue(seen["backup"])

    def test_later_means_no_restart(self):
        self.choose("Upgrade usvfs", "Later")
        self.manager().startup()
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.files(), contents("new"))                         # but it is installed

    def test_dont_ask_again_is_remembered_but_the_warning_stays(self):
        self.choose("Don't ask again")
        mgr = self.manager()
        mgr.startup()
        self.assertTrue(mgr.state.declined)
        self.assertEqual(self.changes[-1:], [1])
        fakes.reset_ui()
        fakes.LOG.clear()
        self.manager().startup()                                                # a new session reads the saved choice
        self.assertEqual(self.asked(), [])
        self.assertEqual(len(self.warnings()), 1)

    def test_a_backup_saved_loose_by_the_first_release_is_moved_into_the_folder_at_startup(self):
        usvfs.backup(self.root, "0.5.6.1")
        (self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip").replace(self.root / "usvfs_0.5.6.1.zip")
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        self.manager().startup()
        self.assertFalse((self.root / "usvfs_0.5.6.1.zip").exists())
        self.assertTrue((self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip").exists())
        self.assertIn(f"moved 1 usvfs backup(s) into {self.root / 'usvfs_versions'}", self.log())

    def test_a_working_usvfs_is_logged_as_info(self):
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        self.manager().startup()
        self.assertEqual(self.warnings(), [])
        self.assertIn("MO2 is using usvfs 0.5.7.2", self.log())
        self.assertEqual(fakes.DIALOGS, [])

    def test_the_disk_version_is_reported_when_the_loaded_one_is_unknown(self):
        self.status = usvfs.Status("0.5.7.2", None)
        self.manager().startup()
        self.assertIn("MO2 is using usvfs 0.5.7.2", self.log())

    def test_new_files_on_disk_with_the_old_library_still_running_ask_for_a_restart(self):
        self.status = usvfs.Status("0.5.7.2", "0.5.6.1")
        self.manager().startup()
        (warning,) = self.warnings()
        self.assertIn("still running 0.5.6.1", warning)
        self.assertIn("Restart MO2", warning)
        self.assertEqual(fakes.DIALOGS, [])

    def test_an_unreadable_version_is_reported(self):
        self.status = usvfs.Status(None, None)
        self.manager().startup()
        self.assertIn("could not read the version", self.warnings()[0])

    def test_leftovers_of_an_earlier_upgrade_are_removed(self):
        (self.root / "usvfs_x64.dll.replaced").write_bytes(b"old, was loaded")
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        self.manager().startup()
        self.assertFalse((self.root / "usvfs_x64.dll.replaced").exists())
        self.assertIn("removed 1 old usvfs file(s)", self.log())

    def test_a_failing_offer_never_breaks_startup(self):
        with mock.patch("borderlands.usvfsmanager.ui.ask", side_effect=RuntimeError("no gui")):
            self.manager().startup()
        self.assertTrue([m for m in self.warnings() if "usvfs upgrade offer failed" in m])


class Upgrading(Base):
    def test_a_download_failure_changes_nothing_and_says_so(self):
        for error in (netio.NetworkError("offline"), netio.ChecksumError("does not match"), ValueError("odd archive"),
                      RuntimeError("no tar")):
            fakes.reset_ui()
            self.fetch_error = error
            self.manager().upgrade()
            self.assertEqual(self.files(), contents("old"), error)
            self.assertIn("nothing was changed", self.said("critical")[-1])
            self.assertIn(str(error), self.said("critical")[-1])
            self.assertEqual(self.restarts, [])

    def test_cancelling_the_download_is_silent(self):
        self.fetch_error = netio.Cancelled()
        self.manager().upgrade()
        self.assertEqual((self.said("critical"), self.files()), ([], contents("old")))

    def test_a_replacement_failure_is_reported_and_the_old_files_are_back(self):
        with mock.patch.object(usvfs, "replace_files", side_effect=OSError("locked")):
            self.manager().upgrade()
        self.assertIn("put back", self.said("critical")[-1])
        self.assertEqual(self.files(), contents("old"))
        self.assertEqual(self.restarts, [])

    def test_a_missing_file_stops_the_backup_and_so_the_upgrade(self):
        (self.root / "usvfs_x86.dll").unlink()
        self.manager().upgrade()
        self.assertIn("The usvfs upgrade failed", self.said("critical")[-1])
        self.assertEqual(self.fetches, [])

    def test_the_temporary_download_folder_is_cleaned_up(self):
        made = []
        real = usvfs.new_workdir

        def tracking():
            made.append(real())
            return made[-1]

        self.choose("Later")
        with mock.patch.object(usvfs, "new_workdir", tracking):
            self.manager().upgrade()
        self.assertTrue(made and not made[0].exists())

    def test_the_download_folder_is_removed_even_when_the_download_fails(self):
        """The failure the first real run hit: an unreadable archive left a 26 MB folder in %TEMP%."""
        made = []
        real = usvfs.new_workdir

        def tracking():
            made.append(real())
            return made[-1]

        for error in (netio.NetworkError("offline"), ValueError("could not be read"), netio.Cancelled()):
            made.clear()
            self.fetch_error = error
            with mock.patch.object(usvfs, "new_workdir", tracking):
                self.manager().upgrade()
            self.assertTrue(made and not made[0].exists(), error)

    def test_the_download_folder_is_removed_when_the_replacement_fails(self):
        made = []
        real = usvfs.new_workdir

        def tracking():
            made.append(real())
            return made[-1]

        with mock.patch.object(usvfs, "new_workdir", tracking), \
                mock.patch.object(usvfs, "replace_files", side_effect=OSError("locked")):
            self.manager().upgrade()
        self.assertTrue(made and not made[0].exists())

    def test_a_manual_offer_has_no_dont_ask_again(self):
        self.choose("Cancel")
        self.manager().offer_upgrade(startup=False)
        self.assertEqual(self.asked()[-1][3], ["Upgrade usvfs", "Cancel"])
        self.assertEqual(self.files(), contents("old"))

    def test_a_second_upgrade_keeps_a_differently_named_backup(self):
        self.choose("Later", "Later")
        self.manager().upgrade()
        for name, data in contents("old again").items():                        # pretend the version label is the same
            (self.root / name).write_bytes(data)
        self.manager().upgrade()
        self.assertEqual(sorted(p.name for p in (self.root / "usvfs_versions").glob("usvfs_0.5.6.1*.zip")),
                         ["usvfs_0.5.6.1.zip", "usvfs_0.5.6.1_2.zip"])


class Reverting(Base):
    def setUp(self):
        super().setUp()
        self.choose("Later")
        self.manager().upgrade()                                               # now "new" is installed, "old" is backed up
        self.restarts.clear()
        fakes.reset_ui()
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        self.reader_version = "0.5.6.1"                                        # what the backup's x64 dll reports

    def test_revert_restores_the_backup_and_saves_the_current_files_first(self):
        mgr = self.manager()
        (backup,) = usvfs.list_backups(self.root)
        self.choose("Switch", "Later")
        mgr.restore(backup)
        self.assertEqual(self.files(), contents("old"))
        self.assertIn("Revert usvfs from 0.5.7.2 to 0.5.6.1", self.asked()[0][2])
        saved = self.root / "usvfs_versions" / "usvfs_0.5.7.2.zip"
        self.assertTrue(saved.exists())                                        # so it can be switched back
        self.assertIn("usvfs was 0.5.7.2; it is now 0.5.6.1", self.warnings()[-1])
        self.assertIn(str(saved), self.warnings()[-1])

    def test_cancelling_changes_nothing(self):
        mgr = self.manager()
        (backup,) = usvfs.list_backups(self.root)
        self.choose("Cancel")
        mgr.restore(backup)
        self.assertEqual(self.files(), contents("new"))
        self.assertFalse((self.root / "usvfs_versions" / "usvfs_0.5.7.2.zip").exists())

    def test_a_backup_that_is_not_what_its_name_says_is_refused(self):
        mgr = self.manager()
        (backup,) = usvfs.list_backups(self.root)
        self.reader_version = "0.4.0.0"
        self.choose("Switch")
        mgr.restore(backup)
        self.assertIn("nothing was changed", self.said("critical")[-1])
        self.assertEqual(self.files(), contents("new"))

    def test_a_restart_can_be_chosen_after_switching(self):
        mgr = self.manager()
        (backup,) = usvfs.list_backups(self.root)
        self.choose("Switch", "Restart now")
        mgr.restore(backup)
        self.assertEqual(self.restarts, [1])

    def test_going_forward_to_a_newer_backup_is_called_an_upgrade(self):
        mgr = self.manager()
        (backup,) = usvfs.list_backups(self.root)
        self.choose("Switch", "Later")
        mgr.restore(backup)                                                     # now old is installed again
        self.status = usvfs.Status("0.5.6.1", "0.5.6.1")
        newer = [b for b in usvfs.list_backups(self.root) if b.version == "0.5.7.2"]
        fakes.reset_ui()
        self.choose("Cancel")
        mgr.restore(newer[0])
        self.assertIn("Upgrade usvfs from 0.5.6.1 to 0.5.7.2", self.asked()[0][2])


class MenuEntries(Base):
    def labels(self):
        return [e[0] for e in self.manager().menu_entries()]

    def test_an_old_usvfs_with_no_backups_offers_the_download(self):
        self.assertEqual(self.labels(), ["Borderlands usvfs: Upgrade to 0.5.7.2"])

    def test_the_download_entry_runs_the_manual_offer(self):
        (entry,) = self.manager().menu_entries()
        self.choose("Cancel")
        entry[2]()
        self.assertEqual(self.asked()[-1][3], ["Upgrade usvfs", "Cancel"])

    def test_a_new_usvfs_with_nothing_saved_has_no_entries(self):
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        self.assertEqual(self.labels(), [])

    def test_a_new_usvfs_with_an_older_backup_offers_the_revert(self):
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        usvfs.backup(self.root, "0.5.6.1")
        self.assertEqual(self.labels(), ["Borderlands usvfs: Revert to 0.5.6.1 (from backup)"])

    def test_an_old_usvfs_with_a_backup_of_the_new_one_uses_the_backup_instead_of_downloading(self):
        for name, data in contents("new").items():
            (self.root / name).write_bytes(data)
        usvfs.backup(self.root, "0.5.7.2")
        for name, data in contents("old").items():
            (self.root / name).write_bytes(data)
        self.assertEqual(self.labels(), ["Borderlands usvfs: Upgrade to 0.5.7.2 (from backup)"])

    def test_the_backup_of_the_current_version_is_not_listed(self):
        usvfs.backup(self.root, "0.5.6.1")
        self.assertEqual(self.labels(), ["Borderlands usvfs: Upgrade to 0.5.7.2"])

    def test_several_backups_are_listed_oldest_first(self):
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        usvfs.backup(self.root, "0.5.7.0")
        usvfs.backup(self.root, "0.5.6.1")
        self.assertEqual(self.labels(), ["Borderlands usvfs: Revert to 0.5.6.1 (from backup)",
                                         "Borderlands usvfs: Revert to 0.5.7.0 (from backup)"])

    def test_an_unreadable_version_offers_no_download(self):
        self.status = usvfs.Status(None, None)
        self.assertEqual(self.labels(), [])

    def test_a_backup_entry_switches_to_that_backup(self):
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        usvfs.backup(self.root, "0.5.6.1")
        entry = self.manager().menu_entries()[0]
        self.choose("Switch", "Later")
        entry[2]()
        self.assertEqual(self.files(), contents("old"))


class Notification(Base):
    def test_an_old_usvfs(self):
        short, long = self.manager().problem_text()
        self.assertIn("usvfs 0.5.6.1 is too old", short)
        self.assertIn("0.5.7.1 or newer", long)
        self.assertIn("0.5.7.2", long)

    def test_a_restart_is_pending(self):
        self.status = usvfs.Status("0.5.7.2", "0.5.6.1")
        short, long = self.manager().problem_text()
        self.assertIn("Restart MO2", short)
        self.assertIn("still running 0.5.6.1", long)

    def test_no_problem_when_it_works(self):
        self.status = usvfs.Status("0.5.7.2", "0.5.7.2")
        self.assertIsNone(self.manager().problem_text())

    def test_the_fix_upgrades_or_offers_the_restart(self):
        self.choose("Cancel")
        self.manager().fix()
        self.assertEqual(self.asked()[-1][3], ["Upgrade usvfs", "Cancel"])
        fakes.reset_ui()
        self.status = usvfs.Status("0.5.7.2", "0.5.6.1")
        self.choose("Restart now")
        self.manager().fix()
        self.assertEqual(self.restarts, [1])


class Restarting(Base):
    def default_manager(self, root=None):
        return UsvfsManager(self.org, lambda: self.window, root=root or self.root, status_fn=lambda: self.status)

    def test_the_default_restart_relaunches_the_same_instance_and_profile_and_closes_mo2(self):
        self.org.profile = lambda: mock.Mock(name="profile", **{"name.return_value": "Default"})
        spawned = []
        with mock.patch.object(usvfs, "spawn_restart", lambda cmd: spawned.append(cmd)), \
                mock.patch("borderlands.usvfsmanager.os.getpid", return_value=777):
            self.default_manager().restart()
        (command,) = spawned
        script = command[-1]
        self.assertIn("Wait-Process -Id 777", script)
        self.assertIn("""-ArgumentList '-i "Borderlands 4" -p Default'""", script)
        self.assertIn("ModOrganizer.exe", script)
        self.window.close.assert_called_once_with()

    def test_mo2_is_only_closed_from_the_running_event_loop(self):
        """Closing inside the start-up callback loses MO2's quit request (exec() clears it) and strands MO2."""
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        self.addCleanup(fakes.QThread.level.__setitem__, 0, 1)
        fakes.QThread.level[0] = 0                                              # before exec()
        mgr = self.default_manager()
        with mock.patch.object(usvfs, "spawn_restart", lambda cmd: None):
            mgr.restart()
        self.assertTrue(mgr.restarting)                                         # asked for, not yet carried out
        fakes.QTimer.queue.pop(0)()
        self.window.close.assert_not_called()
        fakes.QThread.level[0] = 2                                              # a dialog's nested loop
        fakes.QTimer.queue.pop(0)()
        self.window.close.assert_not_called()
        fakes.QThread.level[0] = 1
        fakes.QTimer.queue.pop(0)()
        self.window.close.assert_called_once_with()
        self.assertIn("restarting Mod Organizer 2", self.log())

    def test_a_portable_instance_is_started_without_an_instance_name(self):
        self.org.base_path = str(self.root)
        spawned = []
        with mock.patch.object(usvfs, "spawn_restart", lambda cmd: spawned.append(cmd)):
            self.default_manager().restart()
        self.assertNotIn("'-i'", spawned[0][-1])

    def test_without_a_window_the_application_is_asked_to_quit(self):
        self.window = None
        before = len(fakes.QUIT)
        with mock.patch.object(usvfs, "spawn_restart", lambda cmd: None):
            self.default_manager().restart()
        self.assertEqual(len(fakes.QUIT), before + 1)

    def test_a_failure_to_launch_the_helper_is_reported_and_mo2_stays_open(self):
        with mock.patch.object(usvfs, "spawn_restart", side_effect=OSError("no powershell")):
            self.default_manager().restart()
        self.assertIn("no powershell", self.said("critical")[-1])
        self.window.close.assert_not_called()


class Defaults(Base):
    def test_the_default_root_is_mo2s_own_folder(self):
        fakes.APP_DIR[0] = str(self.root)
        self.addCleanup(fakes.APP_DIR.__setitem__, 0, "C:/fake/MO2")
        mgr = UsvfsManager(self.org, lambda: None, status_fn=lambda: self.status)
        self.assertEqual(mgr.root, self.root)

    def test_the_default_status_reads_the_files_next_to_mo2(self):
        mgr = UsvfsManager(self.org, lambda: None, root=self.root, reader=lambda p: "0.5.6.1")
        self.assertEqual(mgr.status().disk, "0.5.6.1")

    def test_a_corrupt_state_file_means_not_declined(self):
        path = Path(self.org.base_path) / "plugin_state" / "borderlands" / "usvfs_state.json"
        path.parent.mkdir(parents=True)
        path.write_text("{nope")
        self.assertFalse(self.manager().state.declined)


if __name__ == "__main__":
    unittest.main()
