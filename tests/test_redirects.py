import json
import tempfile
import unittest
from pathlib import Path

from borderlands import redirects
from borderlands.spec import SDK_MOD_SETTINGS, Redirect

R = SDK_MOD_SETTINGS


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.game, self.mods, self.state = root / "game", root / "mods", root / "state"
        self.base = redirects.game_dir(self.game, R)        # real <game>/sdk_mods/settings
        self.target = redirects.target_dir(self.mods, R)    # <mods>/SDK Mod Settings/sdk_mods/settings

    def put(self, folder: Path, rel: str, text: str = "x") -> Path:
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def sync(self, r=R):
        return redirects.sync(r, self.game, self.mods, self.state)

    def rels(self, folder: Path):
        return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


class Paths(Base):
    def test_layout(self):
        self.assertEqual(self.target, self.mods / "SDK Mod Settings" / "sdk_mods" / "settings")
        self.assertEqual(self.base, self.game / "sdk_mods" / "settings")

    def test_slug(self):
        self.assertEqual(R.slug, "sdk_mod_settings")
        self.assertEqual(Redirect("SDK Keybinds", "sdk_mods/keybinds").slug, "sdk_keybinds")

    def test_backslash_game_dir_is_accepted(self):
        r = Redirect("X", "sdk_mods\\keybinds")
        self.assertEqual(redirects.game_dir(self.game, r), self.game / "sdk_mods" / "keybinds")


class Seeding(Base):
    def test_copies_existing_files_and_nested_dirs(self):
        self.put(self.base, "a.json", "A")
        self.put(self.base, "Mod/capture.json", "C")
        result = self.sync()
        self.assertEqual(list(result.seeded), ["Mod/capture.json", "a.json"])
        self.assertEqual((self.target / "a.json").read_text(), "A")
        self.assertEqual(self.rels(self.base), ["Mod/capture.json", "a.json"])  # originals untouched

    def test_never_overwrites_and_skips_pycache(self):
        self.put(self.base, "a.json", "original")
        self.put(self.base, "__pycache__/x.pyc")
        self.put(self.target, "a.json", "edited")
        self.sync()
        self.assertEqual((self.target / "a.json").read_text(), "edited")
        self.assertEqual(self.rels(self.target), ["a.json"])

    def test_seed_can_be_disabled_per_redirect(self):
        r = Redirect("SDK Keybinds", "sdk_mods/keybinds", seed_existing=False)
        self.put(redirects.game_dir(self.game, r), "keybinds.pyd")
        self.assertEqual(self.sync(r).seeded, ())
        self.assertEqual(self.rels(redirects.target_dir(self.mods, r)), [])

    def test_missing_game_folder_is_fine(self):
        self.assertEqual(self.sync().seeded, ())
        self.assertTrue(self.target.is_dir())


class Deletions(Base):
    def test_deleted_file_gets_marker_after_run(self):
        self.put(self.base, "a.json")
        self.sync()
        (self.target / "a.json").unlink()                    # the game deletes its copy
        done = redirects.after_run(R, self.mods, self.state)
        self.assertEqual(done.marked, ("a.json",))
        self.assertTrue((self.target / "a.json.deleted").is_file())
        self.assertEqual((self.target / "a.json.deleted").read_text(), "")  # touched, empty

    def test_marker_sits_next_to_nested_file(self):
        self.put(self.base, "Mod/c.json")
        self.sync()
        (self.target / "Mod" / "c.json").unlink()
        redirects.after_run(R, self.mods, self.state)
        self.assertTrue((self.target / "Mod" / "c.json.deleted").is_file())

    def test_deleted_directory_marks_every_file(self):
        self.put(self.base, "Mod/one.json")
        self.put(self.base, "Mod/two.json")
        self.sync()
        import shutil
        shutil.rmtree(self.target / "Mod")
        done = redirects.after_run(R, self.mods, self.state)
        self.assertEqual(done.marked, ("Mod/one.json", "Mod/two.json"))

    def test_marked_file_is_not_reseeded_on_next_launch(self):
        self.put(self.base, "a.json")
        self.sync()
        (self.target / "a.json").unlink()
        redirects.after_run(R, self.mods, self.state)
        again = self.sync()
        self.assertEqual(again.seeded, ())
        self.assertFalse((self.target / "a.json").exists())
        self.assertTrue((self.target / "a.json.deleted").exists())

    def test_deletion_between_runs_is_caught_at_next_launch(self):
        self.put(self.base, "a.json")
        self.sync()
        (self.target / "a.json").unlink()                    # e.g. deleted in MO2's file tree
        again = self.sync()
        self.assertEqual(again.marked, ("a.json",))

    def test_file_that_comes_back_loses_its_marker(self):
        self.put(self.base, "a.json")
        self.sync()
        (self.target / "a.json").unlink()
        redirects.after_run(R, self.mods, self.state)
        self.put(self.target, "a.json", "recreated by the game")
        done = redirects.after_run(R, self.mods, self.state)
        self.assertEqual(done.restored, ("a.json",))
        self.assertFalse((self.target / "a.json.deleted").exists())

    def test_marker_for_a_file_created_in_the_mod_not_seeded(self):
        self.put(self.target, "new.json")                    # created by the game, not in base
        self.sync()
        (self.target / "new.json").unlink()
        self.assertEqual(redirects.after_run(R, self.mods, self.state).marked, ("new.json",))

    def test_marker_names_are_not_treated_as_files(self):
        self.put(self.target, "x.json.deleted")
        self.assertEqual(redirects.files_under(self.target, R), set())
        self.assertTrue(redirects.is_marker("X.JSON.DELETED", R))

    def test_custom_suffix(self):
        r = Redirect("SDK Keybinds", "sdk_mods/keybinds", deleted_suffix=".gone")
        target = redirects.target_dir(self.mods, r)
        self.put(target, "k.ini")
        redirects.sync(r, self.game, self.mods, self.state)
        (target / "k.ini").unlink()
        redirects.after_run(r, self.mods, self.state)
        self.assertTrue((target / "k.ini.gone").exists())


class FreshStartAndState(Base):
    def test_removing_the_mod_folder_resets_instead_of_marking_everything(self):
        self.put(self.base, "a.json")
        self.sync()
        import shutil
        shutil.rmtree(self.mods / "SDK Mod Settings")        # user deleted the mod to reset it
        again = self.sync()
        self.assertEqual(again.marked, ())
        self.assertEqual(list(again.seeded), ["a.json"])     # seeded afresh

    def test_after_run_without_mod_folder_does_nothing(self):
        self.assertIsNone(redirects.after_run(R, self.mods, self.state))

    def test_no_state_dir_still_seeds_but_cannot_detect_deletions(self):
        self.put(self.base, "a.json")
        result = redirects.sync(R, self.game, self.mods, None)
        self.assertEqual(list(result.seeded), ["a.json"])
        self.assertIsNone(redirects.after_run(R, self.mods, None))

    def test_manifest_is_sorted_json_and_survives_corruption(self):
        self.put(self.base, "b.json")
        self.put(self.base, "a.json")
        self.sync()
        path = redirects.manifest_path(self.state, R)
        self.assertEqual(json.loads(path.read_text()), ["a.json", "b.json"])
        path.write_text("not json")
        self.assertIsNone(redirects.read_manifest(path))
        path.write_text('{"a": 1}')
        self.assertIsNone(redirects.read_manifest(path))
        self.assertIsNone(redirects.read_manifest(self.state / "missing.json"))
        (self.target / "a.json").unlink()
        self.assertEqual(self.sync().marked, ())             # unreadable manifest: nothing assumed deleted


if __name__ == "__main__":
    unittest.main()
