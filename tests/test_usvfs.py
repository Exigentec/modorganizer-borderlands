"""usvfs version handling: versions, backups, safe replacement, pinned download, restart command."""

import hashlib
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from borderlands import netio, usvfs

NAMES = usvfs.FILES


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contents(tag: str) -> dict[str, bytes]:
    return {name: f"{tag}:{name}".encode() for name in NAMES}


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "MO2"
        self.root.mkdir()

    def install(self, tag="old", root=None):
        for name, data in contents(tag).items():
            ((root or self.root) / name).write_bytes(data)

    def read(self, name):
        return (self.root / name).read_bytes()

    def tree(self, root=None):
        return sorted(p.name for p in (root or self.root).iterdir())


class Versions(unittest.TestCase):
    def test_the_fixed_versions(self):
        for v in ("0.5.7.1", "0.5.7.2", "0.5.8", "0.6", "1.0.0.0", "v0.5.7.1"):
            self.assertTrue(usvfs.is_fixed(v), v)

    def test_the_broken_ones(self):
        for v in ("0.5.6.1", "0.5.6.2", "0.5.7", "0.5.7.0", "0.4.8", None, "", "unknown"):
            self.assertFalse(usvfs.is_fixed(v), v)

    def test_version_keys_compare_numerically(self):
        self.assertGreater(usvfs.version_key("0.5.10.0"), usvfs.version_key("0.5.9.9"))
        self.assertEqual(usvfs.version_key("0.5.6.1 (abc123)"), (0, 5, 6, 1))
        self.assertIsNone(usvfs.version_key("none"))

    def test_the_pinned_release_is_sane_and_fixed(self):
        t = usvfs.TARGET
        self.assertTrue(usvfs.is_fixed(t.version))
        self.assertTrue(t.url.startswith("https://github.com/ModOrganizer2/usvfs/releases/download/"))
        self.assertTrue(t.url.endswith(f"usvfs_v{t.version}.7z"))
        self.assertEqual(set(t.files), set(NAMES))
        for digest in (t.archive_sha256, *t.files.values()):
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_installed_version_reads_the_x64_dll(self):
        seen = []
        reader = lambda path: seen.append(Path(path).name) or "0.5.6.1"
        self.assertEqual(usvfs.installed_version(Path("somewhere"), reader), "0.5.6.1")
        self.assertEqual(seen, ["usvfs_x64.dll"])

    @unittest.skipUnless(sys.platform == "win32", "reads a Windows version resource")
    def test_a_real_file_version_and_missing_ones(self):
        notepad = Path(r"C:\Windows\System32\notepad.exe")
        if not notepad.exists():
            self.skipTest("no notepad")
        self.assertRegex(usvfs.file_version(notepad), r"^\d+\.\d+\.\d+\.\d+$")
        self.assertIsNone(usvfs.file_version(Path(r"C:\does\not\exist.dll")))
        self.assertIsNone(usvfs.file_version(Path(__file__)))                  # a file with no version resource

    @unittest.skipUnless(sys.platform == "win32", "asks Windows which modules are loaded")
    def test_usvfs_is_not_loaded_in_a_test_process(self):
        self.assertIsNone(usvfs.loaded_version())


class Statuses(unittest.TestCase):
    def test_combinations(self):
        S = usvfs.Status
        old = S("0.5.6.1", "0.5.6.1")
        self.assertEqual((old.needs_upgrade, old.pending_restart, old.running_fixed), (True, False, False))
        pending = S("0.5.7.2", "0.5.6.1")
        self.assertEqual((pending.needs_upgrade, pending.pending_restart, pending.running_fixed), (False, True, False))
        good = S("0.5.7.2", "0.5.7.2")
        self.assertEqual((good.needs_upgrade, good.pending_restart, good.running_fixed), (False, False, True))
        unknown_loaded = S("0.5.7.2", None)
        self.assertEqual((unknown_loaded.needs_upgrade, unknown_loaded.pending_restart, unknown_loaded.running_fixed),
                         (False, False, False))
        missing = S(None, None)
        self.assertTrue(missing.needs_upgrade)

    def test_status_combines_disk_and_loaded(self):
        s = usvfs.status(Path("x"), reader=lambda p: "0.5.6.1", loaded=lambda: "0.5.7.2")
        self.assertEqual((s.disk, s.loaded), ("0.5.6.1", "0.5.7.2"))


class Backups(Tmp):
    def test_creates_a_zip_with_exactly_the_four_files(self):
        self.install("old")
        path = usvfs.backup(self.root, "0.5.6.1")
        self.assertEqual(path, self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip")   # in its own folder
        with zipfile.ZipFile(path) as z:
            self.assertEqual(sorted(z.namelist()), sorted(NAMES))
            self.assertEqual(z.read("usvfs_x64.dll"), contents("old")["usvfs_x64.dll"])
        self.assertEqual([n for n in self.tree() if n.endswith(".tmp")], [])

    def test_an_identical_backup_is_reused_and_a_different_one_gets_a_new_name(self):
        self.install("old")
        first = usvfs.backup(self.root, "0.5.6.1")
        self.assertEqual(usvfs.backup(self.root, "0.5.6.1"), first)
        self.install("patched")                                                  # same version label, different bytes
        second = usvfs.backup(self.root, "0.5.6.1")
        self.assertEqual(second.name, "usvfs_0.5.6.1_2.zip")
        self.assertEqual(usvfs.backup(self.root, "0.5.6.1"), second)
        self.assertEqual(first.name, "usvfs_0.5.6.1.zip")

    def test_a_missing_file_stops_the_backup(self):
        self.install("old")
        (self.root / "usvfs_x86.dll").unlink()
        with self.assertRaises(FileNotFoundError):
            usvfs.backup(self.root, "0.5.6.1")
        self.assertFalse((self.root / "usvfs_versions").exists())

    def test_listing_is_numeric_and_ignores_anything_else(self):
        for version in ("0.5.10.0", "0.5.9.1", "0.5.6.1"):
            self.install(version)
            usvfs.backup(self.root, version)
        folder = self.root / "usvfs_versions"
        (folder / "usvfs_notes.zip").write_text("x")
        (folder / "usvfs_0.1.zip").write_bytes(b"not a zip")
        with zipfile.ZipFile(folder / "usvfs_0.2.zip", "w") as z:                # a zip without the four files
            z.writestr("something.txt", "x")
        with zipfile.ZipFile(folder / "usvfs_0.3.zip", "w") as z:                # the four plus an extra
            for name in NAMES:
                z.writestr(name, "x")
            z.writestr("extra.txt", "x")
        with zipfile.ZipFile(folder / "usvfs_0.4.zip", "w") as z:                # the four, but in a folder
            for name in NAMES:
                z.writestr("folder/" + name, "x")
        (folder / "usvfs_0.6.zip.tmp").write_text("x")
        self.assertEqual([b.version for b in usvfs.list_backups(self.root)], ["0.5.6.1", "0.5.9.1", "0.5.10.0"])
        self.assertEqual(usvfs.list_backups(self.root / "nowhere"), [])

    def test_backups_are_only_listed_from_their_own_folder(self):
        self.install("old")
        usvfs.backup(self.root, "0.5.6.1")
        (self.root / "usvfs_0.9.zip").write_bytes((self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip").read_bytes())
        self.assertEqual([b.version for b in usvfs.list_backups(self.root)], ["0.5.6.1"])    # a loose copy is not listed

    def test_loose_backups_from_an_earlier_version_are_moved_into_the_folder(self):
        self.install("old")
        usvfs.backup(self.root, "0.5.6.1")
        loose = self.root / "usvfs_0.5.6.1.zip"
        (self.root / "usvfs_versions" / "usvfs_0.5.6.1.zip").replace(loose)       # as the first release left it
        (self.root / "usvfs_notes.zip").write_text("not a backup")
        (self.root / "usvfs_0.4.zip").write_bytes(b"junk")
        moved = usvfs.migrate_backups(self.root)
        self.assertEqual([p.name for p in moved], ["usvfs_0.5.6.1.zip"])
        self.assertFalse(loose.exists())
        self.assertEqual([b.version for b in usvfs.list_backups(self.root)], ["0.5.6.1"])
        self.assertTrue((self.root / "usvfs_notes.zip").exists() and (self.root / "usvfs_0.4.zip").exists())
        self.assertEqual(usvfs.migrate_backups(self.root), [])                    # nothing left to move

    def test_migrating_never_overwrites_an_existing_backup(self):
        self.install("old")
        kept = usvfs.backup(self.root, "0.5.6.1")
        self.install("other")
        (self.root / "usvfs_0.5.6.1.zip").write_bytes(b"")
        with zipfile.ZipFile(self.root / "usvfs_0.5.6.1.zip", "w") as z:
            for name in NAMES:
                z.writestr(name, "different")
        moved = usvfs.migrate_backups(self.root)
        self.assertEqual([p.name for p in moved], ["usvfs_0.5.6.1_2.zip"])
        self.assertEqual(len(usvfs.list_backups(self.root)), 2)
        self.assertTrue(kept.exists())

    def test_no_folder_is_made_when_there_is_nothing_to_migrate(self):
        self.install("old")
        self.assertEqual(usvfs.migrate_backups(self.root), [])
        self.assertFalse((self.root / "usvfs_versions").exists())
        self.assertEqual(usvfs.migrate_backups(self.root / "nowhere"), [])

    def test_the_second_backup_of_a_version_is_listed_under_that_version(self):
        self.install("a")
        usvfs.backup(self.root, "0.5.6.1")
        self.install("b")
        usvfs.backup(self.root, "0.5.6.1")
        self.assertEqual([(b.version, b.path.name) for b in usvfs.list_backups(self.root)],
                         [("0.5.6.1", "usvfs_0.5.6.1.zip"), ("0.5.6.1", "usvfs_0.5.6.1_2.zip")])

    def test_extracting_checks_the_version(self):
        self.install("old")
        (backup,) = [usvfs.Backup("0.5.6.1", usvfs.backup(self.root, "0.5.6.1"))]
        files = usvfs.extract_backup(backup, self.root / "stage", reader=lambda p: "0.5.6.1")
        self.assertEqual(sorted(files), sorted(NAMES))
        self.assertEqual(files["usvfs_x86.dll"].read_bytes(), contents("old")["usvfs_x86.dll"])
        with self.assertRaises(ValueError) as ctx:
            usvfs.extract_backup(backup, self.root / "stage2", reader=lambda p: "0.5.7.2")
        self.assertIn("not 0.5.6.1", str(ctx.exception))
        usvfs.extract_backup(backup, self.root / "stage3", reader=lambda p: None)  # no resource: cannot tell, accepted

    def test_extracting_a_broken_backup_is_refused(self):
        (self.root / "usvfs_0.5.6.1.zip").write_bytes(b"junk")
        with self.assertRaises(ValueError):
            usvfs.extract_backup(usvfs.Backup("0.5.6.1", self.root / "usvfs_0.5.6.1.zip"), self.root / "s")


class Replacing(Tmp):
    def setUp(self):
        super().setUp()
        self.install("old")
        self.new = Path(self._tmp.name) / "new"
        self.new.mkdir()
        self.install("new", self.new)
        self.files = {name: self.new / name for name in NAMES}

    def test_swaps_all_four_and_keeps_the_old_ones_aside(self):
        done = usvfs.replace_files(self.root, self.files)
        self.assertEqual(done, list(NAMES))
        for name in NAMES:
            self.assertEqual(self.read(name), contents("new")[name])
            self.assertEqual(self.read(name + ".replaced"), contents("old")[name])

    def test_a_second_swap_does_not_overwrite_the_first_ones_leftovers(self):
        usvfs.replace_files(self.root, self.files)
        self.install("newer", self.new)
        usvfs.replace_files(self.root, self.files)
        self.assertEqual(self.read("usvfs_x64.dll.replaced"), contents("old")["usvfs_x64.dll"])
        self.assertEqual(self.read("usvfs_x64.dll.replaced2"), contents("new")["usvfs_x64.dll"])

    def test_a_failure_puts_everything_back(self):
        real = shutil.copy2
        calls = []

        def flaky(src, dst, **kw):
            calls.append(Path(dst).name)
            if len(calls) == 3:
                raise OSError("disk full")
            return real(src, dst, **kw)

        with mock.patch.object(usvfs.shutil, "copy2", flaky), self.assertRaises(OSError):
            usvfs.replace_files(self.root, self.files)
        self.assertEqual({n: self.read(n) for n in NAMES}, contents("old"))      # the old files are back
        self.assertEqual(self.tree(), sorted(NAMES))                              # no leftovers, no half-placed files

    def test_a_missing_target_is_simply_filled(self):
        for name in NAMES:
            (self.root / name).unlink()
        usvfs.replace_files(self.root, self.files)
        self.assertEqual(self.tree(), sorted(NAMES))
        self.assertEqual(self.read("usvfs_x64.dll"), contents("new")["usvfs_x64.dll"])

    def test_leftovers_are_cleaned_up_later_and_a_locked_one_is_tolerated(self):
        usvfs.replace_files(self.root, self.files)
        (self.root / "notes.txt").write_text("keep")
        real_unlink = Path.unlink

        def unlink(path, *a, **kw):
            if path.name == "usvfs_x64.dll.replaced":
                raise PermissionError("still loaded")
            return real_unlink(path, *a, **kw)

        with mock.patch.object(Path, "unlink", unlink):
            self.assertEqual(usvfs.cleanup_replaced(self.root), 3)
        self.assertEqual(usvfs.cleanup_replaced(self.root), 1)                    # the next start gets it
        self.assertEqual(self.tree(), sorted(NAMES + ("notes.txt",)))

    @unittest.skipUnless(sys.platform == "win32", "loads a DLL")
    def test_a_loaded_dll_can_be_swapped_by_renaming_it(self):
        """The reason for the rename: Windows will not overwrite a loaded DLL, but will rename it."""
        import ctypes

        victim = Path(r"C:\Windows\System32\msimg32.dll")
        if not victim.exists():
            self.skipTest("no msimg32.dll to borrow")
        shutil.copy2(victim, self.root / "usvfs_x64.dll")
        handle = ctypes.WinDLL(str(self.root / "usvfs_x64.dll"))                 # now loaded, like MO2's usvfs
        try:
            with self.assertRaises(OSError):
                (self.root / "usvfs_x64.dll").write_bytes(b"overwrite")           # what a naive copy would do
            self.assertEqual(self.read("usvfs_x64.dll"), victim.read_bytes())
            usvfs.replace_files(self.root, {"usvfs_x64.dll": self.new / "usvfs_x64.dll"})
            self.assertEqual(self.read("usvfs_x64.dll"), contents("new")["usvfs_x64.dll"])
            self.assertEqual(self.read("usvfs_x64.dll.replaced"), victim.read_bytes())
            self.assertEqual(usvfs.cleanup_replaced(self.root), 0)                # cannot delete it while loaded
        finally:
            ctypes.windll.kernel32.FreeLibrary.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.FreeLibrary(handle._handle)
        self.assertEqual(usvfs.cleanup_replaced(self.root), 1)                    # after the "restart" it goes


class TarChoice(Tmp):
    """Git for Windows and Cygwin put a GNU tar, which cannot read 7z, on PATH ahead of Windows' own."""

    def system_root(self, with_tar=True):
        windows = Path(self._tmp.name) / "Windows"
        (windows / "System32").mkdir(parents=True)
        if with_tar:
            (windows / "System32" / "tar.exe").write_bytes(b"")
        return windows

    def test_windows_own_tar_wins_over_whatever_is_first_on_path(self):
        windows = self.system_root()
        run = mock.Mock()
        with mock.patch.dict(usvfs.os.environ, {"SystemRoot": str(windows)}), \
                mock.patch.object(usvfs.shutil, "which", return_value=r"C:\Apps\Git\usr\bin\tar.EXE"):
            self.assertEqual(usvfs._tar(run), str(windows / "System32" / "tar.exe"))
        run.assert_not_called()                                                   # no need to even ask the other one

    def test_a_path_tar_is_used_only_if_it_is_bsdtar(self):
        windows = self.system_root(with_tar=False)
        with mock.patch.dict(usvfs.os.environ, {"SystemRoot": str(windows)}), \
                mock.patch.object(usvfs.shutil, "which", return_value="/usr/bin/tar"):
            good = mock.Mock(return_value=mock.Mock(stdout="bsdtar 3.8.9 - libarchive 3.8.9"))
            self.assertEqual(usvfs._tar(good), "/usr/bin/tar")
            gnu = mock.Mock(return_value=mock.Mock(stdout="tar (GNU tar) 1.35"))
            with self.assertRaises(RuntimeError):
                usvfs._tar(gnu)
            broken = mock.Mock(side_effect=OSError("cannot run"))
            with self.assertRaises(RuntimeError):
                usvfs._tar(broken)

    def test_no_tar_at_all_is_reported_clearly(self):
        windows = self.system_root(with_tar=False)
        with mock.patch.dict(usvfs.os.environ, {"SystemRoot": str(windows)}), \
                mock.patch.object(usvfs.shutil, "which", return_value=None), \
                self.assertRaises(RuntimeError) as ctx:
            usvfs._tar(mock.Mock())
        self.assertIn("bsdtar", str(ctx.exception))

    @unittest.skipUnless(sys.platform == "win32", "uses this machine's tar")
    def test_on_this_machine_it_picks_a_tar_that_reads_7z(self):
        found = usvfs._tar()
        out = usvfs.subprocess.run([found, "--version"], capture_output=True, text=True).stdout
        self.assertIn("bsdtar", out.casefold())


class Fetching(Tmp):
    """The download is faked; tar is faked to unpack the files it is told to."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(usvfs, "_tar", return_value="tar"))

    FILES = {name: f"released:{name}".encode() for name in NAMES}

    def release(self, **over):
        values = dict(version="9.9.9.9", url="https://github.com/o/r/releases/download/v9/usvfs_v9.7z",
                      archive_sha256="a" * 64, folder="bin/", files={n: sha(d) for n, d in self.FILES.items()})
        values.update(over)
        return usvfs.Release(**values)

    def fake_run(self, listing=None, fail_extract=False, bad=None):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1] == "-tf":
                names = listing if listing is not None else [f"bin/{n}" for n in NAMES] + ["debug/bin/usvfs_x64.dll", "lib/x.lib"]
                return mock.Mock(returncode=0, stdout="\n".join(names), stderr="")
            if fail_extract:
                return mock.Mock(returncode=1, stdout="", stderr="boom")
            stage = Path(cmd[cmd.index("-C") + 1])
            for member in cmd[cmd.index("-C") + 2:]:
                path = stage / member
                path.parent.mkdir(parents=True, exist_ok=True)
                name = member.split("/")[-1]
                path.write_bytes((bad or {}).get(name, self.FILES[name]))
            return mock.Mock(returncode=0, stdout="", stderr="")

        run.calls = calls
        return run

    def download(self, seen=None):
        def download(url, dest, expected, progress=None, **kw):
            (seen if seen is not None else []).append((url, Path(dest).name, expected))
            Path(dest).write_bytes(b"7z archive")
            return expected

        return download

    def test_downloads_unpacks_only_the_four_files_and_checks_each(self):
        seen, run = [], self.fake_run()
        files = usvfs.fetch(self.release(), self.root / "work", self.download(seen), run=run)
        self.assertEqual(sorted(files), sorted(NAMES))
        self.assertEqual(files["usvfs_x64.dll"].read_bytes(), self.FILES["usvfs_x64.dll"])
        self.assertEqual(seen, [("https://github.com/o/r/releases/download/v9/usvfs_v9.7z", "usvfs_v9.7z", "a" * 64)])
        extract = run.calls[1]
        self.assertEqual(sorted(extract[extract.index("-C") + 2:]), sorted(f"bin/{n}" for n in NAMES))   # not debug/bin

    def test_a_binary_that_does_not_match_its_pinned_hash_is_refused(self):
        run = self.fake_run(bad={"usvfs_x86.dll": b"tampered"})
        with self.assertRaises(netio.ChecksumError) as ctx:
            usvfs.fetch(self.release(), self.root / "work", self.download(), run=run)
        self.assertIn("usvfs_x86.dll", str(ctx.exception))

    def test_an_archive_checksum_failure_stops_before_unpacking(self):
        def download(url, dest, expected, progress=None, **kw):
            raise netio.ChecksumError("the download does not match")

        run = self.fake_run()
        with self.assertRaises(netio.ChecksumError):
            usvfs.fetch(self.release(), self.root / "work", download, run=run)
        self.assertEqual(run.calls, [])

    def test_a_cancelled_or_failed_download_propagates(self):
        for error in (netio.Cancelled(), netio.NetworkError("offline")):
            def download(url, dest, expected, progress=None, _e=error, **kw):
                raise _e

            with self.assertRaises(type(error)):
                usvfs.fetch(self.release(), self.root / "work", download, run=self.fake_run())

    def test_an_archive_without_the_expected_files_is_refused(self):
        run = self.fake_run(listing=["bin/usvfs_x64.dll", "readme.md"])
        with self.assertRaises(ValueError) as ctx:
            usvfs.fetch(self.release(), self.root / "work", self.download(), run=run)
        self.assertIn("usvfs_x86.dll", str(ctx.exception))

    def test_a_failing_tar_is_reported(self):
        with self.assertRaises(ValueError) as ctx:
            usvfs.fetch(self.release(), self.root / "work", self.download(), run=self.fake_run(fail_extract=True))
        self.assertIn("boom", str(ctx.exception))

    def test_a_tar_that_cannot_read_the_archive_says_why(self):
        def run(cmd, **kwargs):
            return mock.Mock(returncode=2, stdout="", stderr="tar: This does not look like a tar archive\ntar: Exiting")

        with self.assertRaises(ValueError) as ctx:
            usvfs.fetch(self.release(), self.root / "work", self.download(), run=run)
        message = str(ctx.exception)
        self.assertIn("could not be read", message)
        self.assertIn("exit 2", message)
        self.assertIn("does not look like a tar archive", message)

    def test_a_stale_extraction_folder_is_not_trusted(self):
        stale = self.root / "work" / "extract" / "bin"
        stale.mkdir(parents=True)
        (stale / "usvfs_x64.dll").write_bytes(b"stale")
        files = usvfs.fetch(self.release(), self.root / "work", self.download(), run=self.fake_run())
        self.assertEqual(files["usvfs_x64.dll"].read_bytes(), self.FILES["usvfs_x64.dll"])


class Restarting(unittest.TestCase):
    def command(self, **kw):
        return usvfs.restart_command(Path(r"C:\Apps\MO2\ModOrganizer.exe"), 4321, **kw)[-1]

    def test_waits_for_the_old_process_and_only_then_starts(self):
        script = self.command(instance="Borderlands 4", profile="Default")
        self.assertIn("Wait-Process -Id 4321 -Timeout 90", script)
        self.assertIn("if (-not (Get-Process -Id 4321", script)
        self.assertIn(r"-FilePath 'C:\Apps\MO2\ModOrganizer.exe'", script)
        self.assertIn("""-ArgumentList '-i "Borderlands 4" -p Default'""", script)
        self.assertIn(r"-WorkingDirectory 'C:\Apps\MO2'", script)

    def test_without_an_instance_or_profile_there_are_no_arguments(self):
        self.assertNotIn("-ArgumentList", self.command())
        self.assertIn("-ArgumentList '-i Only'", self.command(instance="Only"))

    def test_quotes_in_names_cannot_break_out_of_the_script(self):
        script = self.command(instance="It's; Remove-Item x")
        self.assertIn("""'-i "It''s; Remove-Item x"'""", script)
        with self.assertRaises(ValueError):
            self.command(instance='a" -x "b')

    def test_a_name_with_a_space_reaches_mo2_as_one_argument(self):
        """Start-Process -ArgumentList 'a','b c' hands MO2 three arguments; one quoted string hands it two."""
        script = self.command(instance="Borderlands 4", profile="Default")
        self.assertNotIn("','", script)

    def test_the_pid_is_always_a_number(self):
        script = usvfs.restart_command(Path("x.exe"), 12)[-1]
        self.assertIn("-Id 12 ", script)
        with self.assertRaises(ValueError):
            usvfs.restart_command(Path("x.exe"), "12; evil")

    def test_it_is_launched_silent_and_never_detached(self):
        spawned = []
        usvfs.spawn_restart(["powershell", "-Command", "x"], spawn=lambda cmd, **kw: spawned.append((cmd, kw)))
        (cmd, kw), = spawned
        self.assertEqual(cmd[0], "powershell")
        self.assertTrue(kw["close_fds"])
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (usvfs.subprocess.DEVNULL,) * 3)
        self.assertFalse(kw["creationflags"] & 0x00000008)                      # DETACHED_PROCESS: PowerShell then never runs
        self.assertTrue(kw["creationflags"] & 0x08000000)                       # CREATE_NO_WINDOW

    def test_a_job_that_forbids_breaking_away_falls_back_to_a_plain_spawn(self):
        flags = []

        def spawn(cmd, **kw):
            flags.append(kw["creationflags"])
            if kw["creationflags"] & 0x01000000:
                raise PermissionError("access denied")

        usvfs.spawn_restart(["powershell"], spawn=spawn)
        self.assertEqual(len(flags), 2)
        self.assertFalse(flags[1] & 0x01000000)

    @unittest.skipUnless(sys.platform == "win32", "needs Windows PowerShell")
    def test_the_real_helper_starts_the_program_once_the_watched_process_ends(self):
        """End to end with the real spawn flags: with DETACHED_PROCESS this never started anything."""
        import subprocess
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            marker = tmp / "args.txt"
            show = tmp / "show.py"
            show.write_text("import sys, pathlib\npathlib.Path(sys.argv[1]).write_text(repr(sys.argv[2:]))\n",
                            encoding="utf-8")
            victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
            log = tmp / "restart.log"
            command = usvfs.restart_command(Path(sys.executable), victim.pid, "Borderlands 4", "Default", log)
            command[-1] = command[-1].replace("-ArgumentList '", f"-ArgumentList '\"{show}\" \"{marker}\" ", 1)
            usvfs.spawn_restart(command)
            deadline = time.monotonic() + 30
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.25)
            time.sleep(0.5)
            self.assertTrue(marker.exists(), "the helper never started the program")
            self.assertEqual(marker.read_text(), repr(["-i", "Borderlands 4", "-p", "Default"]))
            steps = log.read_text()
            self.assertLess(steps.index("MO2 has exited"), steps.index("started MO2"))

    @unittest.skipUnless(sys.platform == "win32", "needs Windows PowerShell")
    def test_the_helper_deletes_the_set_aside_files_once_mo2_has_exited_and_logs_it(self):
        import subprocess
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            folder = tmp / "MO2"
            folder.mkdir()
            exe = folder / "ModOrganizer.exe"
            exe.write_bytes(b"")                                                # never started: no such program
            for name in ("usvfs_x64.dll.replaced", "usvfs_x86.dll.replaced2", "usvfs_x64.dll", "other.replaced"):
                (folder / name).write_bytes(b"x")
            log = tmp / "restart.log"
            victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
            usvfs.spawn_restart(usvfs.restart_command(exe, victim.pid, None, None, log))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not (log.exists() and ("started MO2" in log.read_text()
                                                                         or "could not start" in log.read_text())):
                time.sleep(0.25)
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             ["ModOrganizer.exe", "other.replaced", "usvfs_x64.dll"])
            self.assertIn("removed usvfs_x64.dll.replaced", log.read_text())
            self.assertIn("could not start MO2", log.read_text())               # and a failure is written down


if __name__ == "__main__":
    unittest.main()
