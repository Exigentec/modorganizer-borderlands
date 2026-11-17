"""The SDK core: manifest, verify, extract, purge, state, releases, downloads."""

import hashlib
import io
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from borderlands import netio, sdk
from tests import fakes

fakes.install()

from tests.fakes import FakeEntry as E, FakeTree  # noqa: E402

REAL_LAYOUT = {
    "OakGame/Binaries/Win64/dsound.dll": b"loader",
    "OakGame/Binaries/Win64/Plugins/python314.dll": b"python" * 10,
    "OakGame/Binaries/Win64/Plugins/DLLs/ssl.pyd": b"ssl",
    "sdk_mods/__main__.py": b"print('main')",
    "sdk_mods/mods_base.sdkmod": b"base",
    "sdk_mods/console_mod_menu.sdkmod": b"menu",
    "sdk_mods/keybinds/__init__.py": b"kb",
    "sdk_mods/settings/.gitignore": b"*",
    "sdk_mods/.stubs/LICENSE": b"mit",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.game = self.root / "game"
        self.game.mkdir()

    def zip(self, files=None, name="sdk.zip"):
        return make_zip(self.root / name, REAL_LAYOUT if files is None else files)


class Manifest(Tmp):
    def test_learns_every_file_with_size_and_hash(self):
        manifest = sdk.read_manifest(self.zip())
        self.assertEqual(set(manifest), set(REAL_LAYOUT))
        entry = manifest["OakGame/Binaries/Win64/dsound.dll"]
        self.assertEqual((entry.size, entry.sha256), (6, sha(b"loader")))

    def test_nothing_about_the_layout_is_pinned(self):
        manifest = sdk.read_manifest(self.zip({"NewTop/a.bin": b"a", "OakGame/deep/er/b.bin": b"b"}))
        self.assertEqual(set(manifest), {"NewTop/a.bin", "OakGame/deep/er/b.bin"})

    def test_base_names_are_what_the_sdk_ships_under_sdk_mods(self):
        names = sdk.base_names(sdk.read_manifest(self.zip()))
        self.assertEqual(names, {"__main__.py", "mods_base.sdkmod", "console_mod_menu.sdkmod",
                                 "keybinds", "settings", ".stubs"})

    def test_unsafe_names_refuse_the_whole_archive(self):
        for bad in ("/abs/x.dll", "../x.dll", "a/../../x.dll", "C:/x.dll", "a/./b.dll", "a//b.dll", "dots./x.dll",
                    "space /x.dll", "x."):
            with self.subTest(bad):
                with self.assertRaises(sdk.UnsafeArchive):
                    sdk.read_manifest(self.zip({"ok/fine.bin": b"1", bad: b"2"}))

    def test_name_check_covers_what_a_zip_cannot_hold_on_windows(self):
        for bad in ("a\\b.dll", "", "a\x00b"):
            with self.subTest(repr(bad)):
                with self.assertRaises(sdk.UnsafeArchive):
                    sdk._check_name(bad)

    def test_names_that_differ_only_in_case_are_refused(self):
        with self.assertRaises(sdk.UnsafeArchive) as ctx:
            sdk.read_manifest(self.zip({"a/File.dll": b"1", "A/file.DLL": b"2"}))
        self.assertIn("case", str(ctx.exception))

    def test_the_games_own_executable_is_protected(self):
        files = {"OakGame/Binaries/Win64/Borderlands4.exe": b"evil", "sdk_mods/a.sdkmod": b"x"}
        with self.assertRaises(sdk.UnsafeArchive) as ctx:
            sdk.read_manifest(self.zip(files), protected=["oakgame\\binaries\\win64\\BORDERLANDS4.EXE"])
        self.assertIn("belongs to the game", str(ctx.exception))

    def test_too_many_files_or_too_much_data_is_refused(self):
        with mock.patch.object(sdk, "MAX_FILES", 2), self.assertRaises(sdk.UnsafeArchive):
            sdk.read_manifest(self.zip({"a": b"1", "b": b"2", "c": b"3"}))
        with mock.patch.object(sdk, "MAX_TOTAL_BYTES", 5), self.assertRaises(sdk.UnsafeArchive):
            sdk.read_manifest(self.zip({"a": b"123456"}))

    def test_empty_and_broken_archives_are_refused(self):
        with self.assertRaises(sdk.UnsafeArchive):
            sdk.read_manifest(self.zip({}))
        (self.root / "bad.zip").write_bytes(b"this is not a zip file")
        with self.assertRaises(sdk.UnsafeArchive):
            sdk.read_manifest(self.root / "bad.zip")
        with self.assertRaises(sdk.UnsafeArchive):
            sdk.read_manifest(self.root / "missing.zip")

    def test_folder_entries_are_ignored(self):
        path = self.root / "d.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("folder/", b"")
            z.writestr("folder/a.bin", b"a")
        self.assertEqual(set(sdk.read_manifest(path)), {"folder/a.bin"})


class Hashing(Tmp):
    def test_the_hash_is_cached_until_the_file_changes(self):
        hasher = sdk.Hasher()
        path = self.root / "f.bin"
        path.write_bytes(b"one")
        one, three = sha(b"one"), sha(b"three")
        counted = mock.Mock(sha256=mock.Mock(wraps=hashlib.sha256))
        with mock.patch.object(sdk, "hashlib", counted):    # only the code under test is counted
            self.assertEqual(hasher.sha256(path), one)
            self.assertEqual(hasher.sha256(path), one)
            self.assertEqual(counted.sha256.call_count, 1)
            path.write_bytes(b"three")                      # size changes
            self.assertEqual(hasher.sha256(path), three)
            self.assertEqual(counted.sha256.call_count, 2)

    def test_a_missing_file_has_no_hash(self):
        self.assertIsNone(sdk.Hasher().sha256(self.root / "nope"))


class Verify(Tmp):
    def setUp(self):
        super().setUp()
        self.manifest = sdk.read_manifest(self.zip())
        self.hasher = sdk.Hasher()

    def place(self, rel, data):
        path = sdk.path_of(self.game, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_everything_missing(self):
        report = sdk.verify(self.manifest, self.game, self.hasher)
        self.assertEqual((len(report.ok), len(report.missing), report.modified, report.complete),
                         (0, len(REAL_LAYOUT), [], False))

    def test_complete(self):
        for rel, data in REAL_LAYOUT.items():
            self.place(rel, data)
        report = sdk.verify(self.manifest, self.game, self.hasher)
        self.assertTrue(report.complete)
        self.assertEqual(len(report.ok), len(REAL_LAYOUT))

    def test_missing_and_modified_are_told_apart(self):
        for rel, data in REAL_LAYOUT.items():
            self.place(rel, data)
        self.place("OakGame/Binaries/Win64/dsound.dll", b"patched")
        sdk.path_of(self.game, "sdk_mods/mods_base.sdkmod").unlink()
        report = sdk.verify(self.manifest, self.game, self.hasher)
        self.assertEqual(report.modified, ["OakGame/Binaries/Win64/dsound.dll"])
        self.assertEqual(report.missing, ["sdk_mods/mods_base.sdkmod"])
        self.assertFalse(report.complete)

    def test_only_the_real_game_folder_counts(self):
        mod = self.root / "mods" / "SDK"
        for rel, data in REAL_LAYOUT.items():           # a complete SDK, but inside an MO2 mod folder
            path = sdk.path_of(mod, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.assertEqual(len(sdk.verify(self.manifest, self.game, self.hasher).missing), len(REAL_LAYOUT))

    def test_a_folder_in_place_of_a_file_is_missing(self):
        sdk.path_of(self.game, "sdk_mods/__main__.py").mkdir(parents=True)
        self.assertIn("sdk_mods/__main__.py", sdk.verify(self.manifest, self.game, self.hasher).missing)


class Extract(Tmp):
    def setUp(self):
        super().setUp()
        self.zip_path = self.zip()
        self.manifest = sdk.read_manifest(self.zip_path)

    def test_writes_the_named_files_and_their_folders(self):
        result = sdk.extract(self.zip_path, self.manifest, self.game,
                             ["OakGame/Binaries/Win64/Plugins/DLLs/ssl.pyd", "sdk_mods/mods_base.sdkmod"])
        self.assertEqual((len(result.written), result.failed), (2, []))
        self.assertEqual((self.game / "sdk_mods/mods_base.sdkmod").read_bytes(), b"base")
        self.assertFalse((self.game / "sdk_mods/keybinds").exists())          # only what was asked for
        self.assertEqual([p.name for p in self.game.rglob("*") if p.name.endswith(sdk.PART_SUFFIX)], [])

    def test_replaces_a_different_existing_file(self):
        (self.game / "sdk_mods").mkdir()
        (self.game / "sdk_mods/mods_base.sdkmod").write_bytes(b"old")
        sdk.extract(self.zip_path, self.manifest, self.game, ["sdk_mods/mods_base.sdkmod"])
        self.assertEqual((self.game / "sdk_mods/mods_base.sdkmod").read_bytes(), b"base")

    def test_a_hash_mismatch_writes_nothing_and_leaves_no_temp_file(self):
        bad = dict(self.manifest)
        bad["sdk_mods/mods_base.sdkmod"] = sdk.Entry("sdk_mods/mods_base.sdkmod", 4, sha(b"something else"))
        (self.game / "sdk_mods").mkdir()
        (self.game / "sdk_mods/mods_base.sdkmod").write_bytes(b"keep me")
        result = sdk.extract(self.zip_path, bad, self.game, ["sdk_mods/mods_base.sdkmod"])
        self.assertEqual(result.written, [])
        self.assertIn("changed while", result.failed[0][1])
        self.assertEqual((self.game / "sdk_mods/mods_base.sdkmod").read_bytes(), b"keep me")
        self.assertEqual(sorted(p.name for p in (self.game / "sdk_mods").iterdir()), ["mods_base.sdkmod"])

    def test_nothing_lands_outside_the_game_folder(self):
        evil = {"../escaped.txt": sdk.Entry("../escaped.txt", 1, sha(b"x"))}
        make_zip(self.root / "evil.zip", {"../escaped.txt": b"x"})
        result = sdk.extract(self.root / "evil.zip", evil, self.game, ["../escaped.txt"])
        self.assertEqual(result.written, [])
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertIn("outside the game folder", result.failed[0][1])

    def test_a_locked_file_is_reported_not_raised(self):
        with mock.patch.object(sdk.os, "replace", side_effect=PermissionError("in use")):
            result = sdk.extract(self.zip_path, self.manifest, self.game, ["sdk_mods/mods_base.sdkmod"])
        self.assertEqual(result.written, [])
        self.assertIn("in use", result.failed[0][1])
        self.assertEqual([p for p in self.game.rglob("*") if p.is_file()], [])    # temp file cleaned up

    def test_progress_is_reported_and_can_cancel(self):
        seen = []
        sdk.extract(self.zip_path, self.manifest, self.game, list(self.manifest)[:3],
                    progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen, [(0, 3), (1, 3), (2, 3), (3, 3)])
        result = sdk.extract(self.zip_path, self.manifest, self.game, list(self.manifest),
                             progress=lambda done, total: done < 2)
        self.assertEqual(len(result.written), 2)

    def test_a_name_missing_from_the_archive_fails_cleanly(self):
        manifest = dict(self.manifest)
        manifest["not/in/zip.bin"] = sdk.Entry("not/in/zip.bin", 1, sha(b"x"))
        result = sdk.extract(self.zip_path, manifest, self.game, ["not/in/zip.bin"])
        self.assertEqual((result.written, len(result.failed)), ([], 1))


class Purge(Tmp):
    def setUp(self):
        super().setUp()
        self.manifest = sdk.read_manifest(self.zip())
        self.hasher = sdk.Hasher()
        sdk.extract(self.zip(), self.manifest, self.game, list(self.manifest))

    def test_removes_every_matching_file_and_the_folders_it_empties(self):
        result = sdk.purge(self.manifest, self.game, self.hasher)
        self.assertEqual((len(result.removed), result.kept_modified, result.failed),
                         (len(REAL_LAYOUT), [], []))
        self.assertEqual(list(self.game.iterdir()), [])                      # the game folder itself stays

    def test_a_modified_file_is_kept_and_reported(self):
        (self.game / "OakGame/Binaries/Win64/dsound.dll").write_bytes(b"patched by the user")
        result = sdk.purge(self.manifest, self.game, self.hasher)
        self.assertEqual(result.kept_modified, ["OakGame/Binaries/Win64/dsound.dll"])
        self.assertEqual((self.game / "OakGame/Binaries/Win64/dsound.dll").read_bytes(), b"patched by the user")
        self.assertEqual(len(result.removed), len(REAL_LAYOUT) - 1)

    def test_files_that_are_not_the_sdks_stay_and_so_do_their_folders(self):
        (self.game / "sdk_mods/MyMod.sdkmod").write_bytes(b"mine")
        (self.game / "sdk_mods/settings/mymod.json").write_text("{}")
        (self.game / "OakGame/Binaries/Win64/Borderlands4.exe").write_bytes(b"game")
        sdk.purge(self.manifest, self.game, self.hasher)
        self.assertEqual(sorted(p.relative_to(self.game).as_posix() for p in self.game.rglob("*") if p.is_file()),
                         ["OakGame/Binaries/Win64/Borderlands4.exe", "sdk_mods/MyMod.sdkmod",
                          "sdk_mods/settings/mymod.json"])

    def test_absent_files_are_counted_not_errors(self):
        (self.game / "sdk_mods/mods_base.sdkmod").unlink()
        result = sdk.purge(self.manifest, self.game, self.hasher)
        self.assertEqual(result.absent, ["sdk_mods/mods_base.sdkmod"])

    def test_a_locked_file_is_reported_and_the_rest_still_go(self):
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == "dsound.dll":
                raise PermissionError("the game is running")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", unlink):
            result = sdk.purge(self.manifest, self.game, self.hasher)
        self.assertEqual([rel for rel, _ in result.failed], ["OakGame/Binaries/Win64/dsound.dll"])
        self.assertEqual(len(result.removed), len(REAL_LAYOUT) - 1)
        self.assertTrue((self.game / "OakGame/Binaries/Win64/dsound.dll").exists())

    def test_purging_twice_is_harmless(self):
        sdk.purge(self.manifest, self.game, self.hasher)
        again = sdk.purge(self.manifest, self.game, self.hasher)
        self.assertEqual((again.removed, len(again.absent)), ([], len(REAL_LAYOUT)))


class State(Tmp):
    def test_defaults(self):
        state = sdk.SdkState(self.root / "state.json")
        self.assertEqual((state.configured, state.enabled, state.tag, state.skipped_tag, state.setup_declined),
                         (False, None, "", "", False))

    def test_round_trip(self):
        path = self.root / "sub" / "state.json"
        sdk.SdkState(path).update(enabled=True, tag="v0.3")
        state = sdk.SdkState(path)
        self.assertEqual((state.configured, state.enabled, state.tag), (True, True, "v0.3"))
        state.update(enabled=False)
        self.assertEqual((sdk.SdkState(path).configured, sdk.SdkState(path).enabled), (True, False))

    def test_unknown_keys_are_rejected_and_ignored_in_the_file(self):
        state = sdk.SdkState(self.root / "state.json")
        with self.assertRaises(KeyError):
            state.update(bogus=1)
        (self.root / "state.json").write_text('{"enabled": true, "bogus": 1}')
        self.assertTrue(sdk.SdkState(self.root / "state.json").enabled)
        with self.assertRaises(AttributeError):
            sdk.SdkState(self.root / "state.json").bogus

    def test_a_corrupt_file_means_not_configured(self):
        (self.root / "state.json").write_text("{nope")
        self.assertFalse(sdk.SdkState(self.root / "state.json").configured)


REPO = "bl-sdk/oak2-mod-manager"


def release_payload(**over):
    payload = {
        "tag_name": "v0.3", "draft": False, "prerelease": False,
        "html_url": "https://github.com/bl-sdk/oak2-mod-manager/releases/tag/v0.3",
        "assets": [{"name": "oak2-sdk.zip", "size": 14752091,
                    "digest": "sha256:602675446abed184169fa158be3c8bc81777a71203581e4a248eca8a3d00b5c7",
                    "browser_download_url": "https://github.com/bl-sdk/oak2-mod-manager/releases/download/v0.3/oak2-sdk.zip"}],
    }
    payload.update(over)
    return payload


class Releases(unittest.TestCase):
    def test_parses_the_real_shape(self):
        release = sdk.parse_latest(release_payload(), REPO, "oak2-sdk.zip")
        self.assertEqual((release.tag, release.size, release.asset), ("v0.3", 14752091, "oak2-sdk.zip"))
        self.assertEqual(release.sha256, "602675446abed184169fa158be3c8bc81777a71203581e4a248eca8a3d00b5c7")
        self.assertTrue(release.url.endswith("/v0.3/oak2-sdk.zip"))

    def test_a_missing_or_malformed_digest_is_empty_not_an_error(self):
        for digest in (None, "", "sha1:abc", "sha256:zz", "sha256:" + "a" * 10):
            payload = release_payload()
            payload["assets"][0]["digest"] = digest
            self.assertEqual(sdk.parse_latest(payload, REPO, "oak2-sdk.zip").sha256, "", digest)

    def test_not_a_usable_release(self):
        bad = [release_payload(draft=True), release_payload(prerelease=True), release_payload(assets=[]),
               release_payload(assets=[{"name": "other.zip", "browser_download_url": "x"}]),
               {}, None, [], {"tag_name": "v1"}, release_payload(tag_name=None)]
        for payload in bad:
            with self.subTest(str(payload)[:40]):
                with self.assertRaises(sdk.NoRelease):
                    sdk.parse_latest(payload, REPO, "oak2-sdk.zip")

    def test_a_download_address_outside_the_projects_releases_is_refused(self):
        for url in ("https://evil.example/oak2-sdk.zip", "http://github.com/bl-sdk/oak2-mod-manager/releases/download/v0.3/oak2-sdk.zip",
                    "https://github.com/someone-else/oak2-mod-manager/releases/download/v0.3/oak2-sdk.zip"):
            payload = release_payload()
            payload["assets"][0]["browser_download_url"] = url
            with self.assertRaises(sdk.NoRelease):
                sdk.parse_latest(payload, REPO, "oak2-sdk.zip")

    def fetch(self, status, payload=None, raises=None):
        calls = []

        def fetch(url, data=None, headers=None, timeout=8.0):
            calls.append((url, headers))
            if raises:
                raise raises
            return status, payload

        fetch.calls = calls
        return fetch

    def test_latest_release_asks_github_for_the_projects_latest(self):
        f = self.fetch(200, release_payload())
        self.assertEqual(sdk.latest_release(REPO, "oak2-sdk.zip", f).tag, "v0.3")
        self.assertEqual(f.calls[0][0], "https://api.github.com/repos/bl-sdk/oak2-mod-manager/releases/latest")

    def test_http_failures(self):
        with self.assertRaises(sdk.NoRelease):
            sdk.latest_release(REPO, "a.zip", self.fetch(404))
        for status in (403, 429):
            with self.assertRaises(netio.NetworkError) as ctx:
                sdk.latest_release(REPO, "a.zip", self.fetch(status))
            self.assertIn("limit", str(ctx.exception))
        with self.assertRaises(netio.NetworkError):
            sdk.latest_release(REPO, "a.zip", self.fetch(500))
        with self.assertRaises(netio.NetworkError):
            sdk.latest_release(REPO, "a.zip", self.fetch(0, raises=netio.NetworkError("offline")))


class Versions(unittest.TestCase):
    def test_newer(self):
        for latest, installed in (("v0.4", "v0.3"), ("v0.10", "v0.9"), ("v1.0.1", "v1.0"), ("v2", "v1.9"), ("v0.3", "")):
            self.assertTrue(sdk.is_newer(latest, installed), (latest, installed))

    def test_not_newer(self):
        for latest, installed in (("v0.3", "v0.3"), ("v0.3", "v0.4"), ("v1", "v1.0"), ("V0.3", "v0.3"), ("v1.0.0", "v1")):
            self.assertFalse(sdk.is_newer(latest, installed), (latest, installed))

    def test_tags_without_numbers_compare_as_different(self):
        self.assertTrue(sdk.is_newer("nightly", "v0.3"))
        self.assertFalse(sdk.is_newer("nightly", "nightly"))


class SdkModDetection(Tmp):
    BASE = sdk.base_names(sdk.read_manifest(make_zip(Path(tempfile.gettempdir()) / "base-names-test.zip", REAL_LAYOUT)))

    def folder(self, *names, files=()):
        folder = self.root / "mod" / "SDK_Mods"
        folder.mkdir(parents=True)
        for name in names:
            (folder / name).mkdir()
        for name in files:
            (folder / name).write_text("x")
        return folder

    def test_sdkmod_files_and_package_folders_are_mods(self):
        folder = self.folder("MyPackage", files=["thing.sdkmod", "README.md", "Other.SDKMOD"])
        self.assertEqual(sdk.sdk_mod_names(folder), ["MyPackage", "Other.SDKMOD", "thing.sdkmod"])

    def test_settings_stubs_hidden_and_dotted_folders_are_not_mods(self):
        folder = self.folder("settings", ".stubs", ".hidden", "v1.2", "Real", files=["__main__.py"])
        self.assertEqual(sdk.sdk_mod_names(folder), ["Real"])

    def test_the_sdks_own_files_are_not_mods(self):
        folder = self.folder("keybinds", "Mine", files=["mods_base.sdkmod", "console_mod_menu.sdkmod", "mine.sdkmod"])
        self.assertEqual(sdk.sdk_mod_names(folder, self.BASE), ["Mine", "mine.sdkmod"])

    def test_a_missing_folder_has_none(self):
        self.assertEqual(sdk.sdk_mod_names(self.root / "nope"), [])

    def test_mod_has_sdk_mods_finds_the_folder_ignoring_case(self):
        self.folder(files=["a.sdkmod"])
        self.assertTrue(sdk.mod_has_sdk_mods(self.root / "mod"))
        self.assertFalse(sdk.mod_has_sdk_mods(self.root / "nowhere"))
        (self.root / "other" / "OakGame").mkdir(parents=True)
        self.assertFalse(sdk.mod_has_sdk_mods(self.root / "other"))

    def test_a_mod_with_only_the_sdks_own_files_or_settings_is_not_an_sdk_mod(self):
        self.folder("settings", files=["mods_base.sdkmod"])
        self.assertFalse(sdk.mod_has_sdk_mods(self.root / "mod", self.BASE))

    def test_trees(self):
        tree = FakeTree(E("sdk_mods", True, [E("thing.sdkmod"), E("settings", True, [E("a.json")])]))
        self.assertTrue(sdk.tree_has_sdk_mods(tree))
        self.assertFalse(sdk.tree_has_sdk_mods(FakeTree(E("OakGame", True, [E("x.pak")]))))
        self.assertFalse(sdk.tree_has_sdk_mods(FakeTree(E("sdk_mods", True, [E("settings", True)]))))
        self.assertFalse(sdk.tree_has_sdk_mods(FakeTree(E("sdk_mods", False))))
        self.assertTrue(sdk.tree_has_sdk_mods(FakeTree(E("SDK_MODS", True, [E("Pkg", True, [E("__init__.py")])]))))
        self.assertFalse(sdk.tree_has_sdk_mods(
            FakeTree(E("sdk_mods", True, [E("mods_base.sdkmod")])), self.BASE))


class GameRunning(unittest.TestCase):
    def run_stub(self, out="", raises=None):
        def run(cmd, **kwargs):
            run.cmd = cmd
            if raises:
                raise raises
            return mock.Mock(stdout=out)

        return run

    def test_on_windows(self):
        with mock.patch.object(sdk.sys, "platform", "win32"):
            run = self.run_stub('"Borderlands4.exe","1234","Console","1","500,000 K"')
            self.assertTrue(sdk.game_running("OakGame/Binaries/Win64/Borderlands4.exe", run))
            self.assertIn("IMAGENAME eq Borderlands4.exe", run.cmd)
            self.assertFalse(sdk.game_running("OakGame\\Borderlands4.exe", self.run_stub(
                "INFO: No tasks are running which match the specified criteria.")))

    def test_a_failing_tasklist_means_not_running(self):
        with mock.patch.object(sdk.sys, "platform", "win32"):
            self.assertFalse(sdk.game_running("Game.exe", self.run_stub(raises=OSError("no tasklist"))))

    def test_not_windows(self):
        with mock.patch.object(sdk.sys, "platform", "linux"):
            self.assertFalse(sdk.game_running("Game.exe", self.run_stub("Game.exe")))


class FakeResponse:
    def __init__(self, data, url="https://github.com/x/y/download/a.zip", length=True):
        self._io, self._url = io.BytesIO(data), url
        self.headers = {"Content-Length": str(len(data))} if length else {}

    def read(self, n=-1):
        return self._io.read(n)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Download(Tmp):
    DATA = bytes(range(256)) * 400

    def opener(self, **kw):
        return lambda request, timeout=None: FakeResponse(self.DATA, **kw)

    def test_streams_hashes_and_moves_into_place(self):
        dest = self.root / "cache" / "a.zip"
        digest = netio.download("https://github.com/x/y/download/a.zip", dest, sha(self.DATA),
                                opener=self.opener(), chunk=10_000)
        self.assertEqual((digest, dest.read_bytes()), (sha(self.DATA), self.DATA))
        self.assertEqual([p.name for p in dest.parent.iterdir()], ["a.zip"])

    def test_progress_reports_bytes_and_total(self):
        seen = []
        netio.download("https://x/a", self.root / "a", progress=lambda d, t: seen.append((d, t)),
                       opener=self.opener(), chunk=40_000)
        self.assertEqual(seen[-1], (len(self.DATA), len(self.DATA)))
        self.assertTrue(all(t == len(self.DATA) for _, t in seen))
        seen.clear()
        netio.download("https://x/a", self.root / "b", progress=lambda d, t: seen.append(t),
                       opener=self.opener(length=False))
        self.assertEqual(set(seen), {0})

    def test_cancelling_leaves_nothing_and_keeps_an_existing_file(self):
        dest = self.root / "a.zip"
        dest.write_bytes(b"old copy")
        with self.assertRaises(netio.Cancelled):
            netio.download("https://x/a", dest, progress=lambda d, t: False, opener=self.opener())
        self.assertEqual([(p.name, p.read_bytes()) for p in self.root.glob("a.zip*")], [("a.zip", b"old copy")])

    def test_a_checksum_mismatch_is_refused_and_cleaned_up(self):
        dest = self.root / "a.zip"
        with self.assertRaises(netio.ChecksumError):
            netio.download("https://x/a", dest, "0" * 64, opener=self.opener())
        self.assertEqual(list(self.root.glob("a.zip*")), [])

    def test_the_checksum_comparison_ignores_case(self):
        netio.download("https://x/a", self.root / "a", sha(self.DATA).upper(), opener=self.opener())

    def test_only_https(self):
        with self.assertRaises(netio.NetworkError):
            netio.download("http://x/a", self.root / "a", opener=self.opener())
        with self.assertRaises(netio.NetworkError) as ctx:
            netio.download("https://x/a", self.root / "a", opener=self.opener(url="http://elsewhere/a"))
        self.assertIn("redirected", str(ctx.exception))
        self.assertEqual(list(self.root.glob("a*")), [])

    def test_connection_errors_become_network_errors(self):
        def opener(request, timeout=None):
            raise urllib.error.URLError("no route")

        with self.assertRaises(netio.NetworkError):
            netio.download("https://x/a", self.root / "a", opener=opener)

        def dies_midway(request, timeout=None):
            response = FakeResponse(self.DATA)
            response.read = mock.Mock(side_effect=[b"abc", TimeoutError("stalled")])
            return response

        with self.assertRaises(netio.NetworkError):
            netio.download("https://x/a", self.root / "a", opener=dies_midway)
        self.assertEqual(list(self.root.glob("a*")), [])


if __name__ == "__main__":
    unittest.main()
