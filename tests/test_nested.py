"""Choosing a folder in MO2's manual install dialog ("Set as <game> directory").

MO2's dialog is green only for VALID and never calls fix(), so the plugin reports VALID for
a chosen folder it can place and moves the files on disk right after the install.  These
tests run that whole flow: check the chosen folder, install it into a temp mod folder, fire
the "mod installed" hook, and look at the files.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import fakes

fakes.install()

from borderlands import layout, relocate  # noqa: E402
from borderlands.game import BorderlandsGame, BorderlandsModDataChecker  # noqa: E402
from borderlands.spec import ALL_SPECS, BORDERLANDS_2, BORDERLANDS_3, BORDERLANDS_4  # noqa: E402
from tests.fakes import FakeEntry as E, FakeTree  # noqa: E402

PAK = "pakchunk103-Windows_999_P.pak"
LANGS = ["Z_ShowFirmware_P", "Z_ShowFirware_DE_P", "Z_ShowFirware_ES_P",
         "Z_ShowFirware_FR_P", "Z_ShowFirware_JP_P", "Z_ShowFirware_KO_P"]

VALID, FIXABLE, INVALID = "VALID", "FIXABLE", "INVALID"


def firmware_archive():
    """The archive from the screenshots: ~Mods/<six folders>/<identical pak name>."""
    return FakeTree(E("~Mods", True, [E(name, True, [E(PAK)]) for name in LANGS]))


def distinct_archive():
    """Same shape, but each folder holds its own differently named pak (so none are variants)."""
    return FakeTree(E("~Mods", True, [E(name, True, [E(f"{name}.pak")]) for name in LANGS]))


def chosen_of(*children):
    """A folder "M" holding ``children``, as the dialog hands it over (it has a parent)."""
    return FakeTree(E("M", True, list(children))).find("M")


def write_tree(entry, dest: Path):
    for child in entry:
        path = dest / child.name()
        if child.isDir():
            path.mkdir(parents=True, exist_ok=True)
            write_tree(child, path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")


class Flow(unittest.TestCase):
    """Base: a game plugin wired to a fake organizer, plus a temp mod folder."""

    def setUp(self):
        fakes.LOG.clear()
        fakes.reset_ui()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._n = 0

    hide_docs = False   # these tests are about placing content; DocsHidden turns the hiding on

    def game(self, spec=BORDERLANDS_4, pak_subdir=""):
        game = BorderlandsGame(spec)
        game._pak_subdir = lambda: pak_subdir      # the plugin setting, bypassing the organizer
        org = fakes.FakeOrganizer()
        org.settings[(game.name(), "hide_documentation")] = self.hide_docs
        game.init(org)
        org.managed = game
        game.test_org = org
        game.test_fire = org.installed_callbacks[0]
        return game

    def mod_folder(self, entry=None):
        self._n += 1
        mod = self.root / f"mod{self._n}"
        mod.mkdir()
        if entry is not None:
            write_tree(entry, mod)
        (mod / "meta.ini").write_text("[General]\n")
        return mod

    def install(self, chosen, spec=BORDERLANDS_4, pak_subdir="", name="fake mod"):
        """Check the chosen folder (or archive root), install it, run the hook.

        Returns (verdict, files)."""
        game = self.game(spec, pak_subdir)
        verdict = game._checker.dataLooksValid(chosen)
        mod = self.mod_folder(chosen)
        game.test_fire(fakes.FakeMod(mod, name=name))
        return verdict, self.files(mod)

    @staticmethod
    def files(mod: Path):
        return sorted(p.relative_to(mod).as_posix() for p in mod.rglob("*")
                      if p.is_file() and p.name != "meta.ini")

    @staticmethod
    def checker(spec=BORDERLANDS_4):
        return BorderlandsModDataChecker(spec, lambda: "")


class DocsHidden(Flow):
    """Documentation a mod leaves in its root is hidden (MO2's own .mohidden) so it stays out of the game folder."""

    hide_docs = True
    PAK = E("OakGame", True, [E("Content", True, [E("Paks", True, [E("a.pak")])])])

    def test_loose_readme_style_files_are_hidden_and_keep_their_content(self):
        names = ["INSTALL.txt", "CHANGELOG.txt", "README.md", "WHERE_TO_INSTALL.txt", "WHERE_TO_ISTALL.txt",
                 "Read Me First.txt", "setup.ini", "LICENSE"]
        mod = self.mod_folder(FakeTree(self.PAK, *[E(n) for n in names]))
        for n in names:
            (mod / n).write_text("words")
        self.game().test_fire(fakes.FakeMod(mod, name="m"))
        self.assertEqual(self.files(mod), sorted(["OakGame/Content/Paks/a.pak"] + [n + ".mohidden" for n in names]))
        self.assertEqual((mod / "INSTALL.txt.mohidden").read_text(), "words")
        self.assertIn("hid 8 documentation file(s) in 'm'", " | ".join(m for _, m in fakes.LOG))

    def test_a_docs_folder_has_its_files_hidden_but_other_folders_and_files_are_left_alone(self):
        root = FakeTree(self.PAK, E("docs", True, [E("manual.pdf"), E("Shots", True, [E("a.png")])]),
                        E("Extras", True, [E("README.txt"), E("data.bin")]), E("notes.json"), E("start.bat"))
        verdict, files = self.install(root)
        self.assertEqual(files, ["Extras/README.txt", "Extras/data.bin", "OakGame/Content/Paks/a.pak",
                                 "docs/Shots/a.png.mohidden", "docs/manual.pdf.mohidden", "notes.json", "start.bat"])

    def test_a_program_mods_files_and_the_game_folders_are_never_touched(self):
        root = FakeTree(E("tool.exe"), E("README.txt"), E("OakGame", True, [E("README.txt")]))
        _, files = self.install(root)
        self.assertEqual(files, ["OakGame/README.txt", "Tools/fake mod/README.txt", "Tools/fake mod/tool.exe"])

    def test_the_setting_turns_it_off(self):
        DocsHidden.hide_docs = False
        self.addCleanup(setattr, DocsHidden, "hide_docs", True)
        _, files = self.install(FakeTree(self.PAK, E("INSTALL.txt")))
        self.assertIn("INSTALL.txt", files)

    def test_it_does_nothing_if_mo2_no_longer_skips_the_hidden_suffix(self):
        game = self.game()
        game._hide_is_effective = lambda: False
        mod = self.mod_folder(FakeTree(self.PAK, E("INSTALL.txt")))
        game.test_fire(fakes.FakeMod(mod, name="m"))
        self.assertIn("INSTALL.txt", self.files(mod))
        self.assertTrue([m for _, m in fakes.LOG if "left visible" in m])

    def test_files_that_are_already_hidden_are_skipped(self):
        mod = self.mod_folder(FakeTree(self.PAK, E("INSTALL.txt.mohidden")))
        self.game().test_fire(fakes.FakeMod(mod, name="m"))
        self.assertIn("INSTALL.txt.mohidden", self.files(mod))
        self.assertFalse([m for _, m in fakes.LOG if "hid " in m])


class Screenshots(Flow):
    def test_the_archive_root_is_accepted_like_a_chosen_folder(self):
        verdict, files = self.install(distinct_archive())            # no "Set as directory" needed
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, sorted(f"OakGame/Content/Paks/~Mods/{n}/{n}.pak" for n in LANGS))

    def test_the_archive_root_with_identical_pak_names_is_refused_and_says_so(self):
        verdict, files = self.install(firmware_archive())
        self.assertEqual(verdict, INVALID)
        (_, message), = [m for m in fakes.LOG if m[0] == "warning"]
        self.assertIn("Borderlands: not placing 'the archive'.", message)
        self.assertIn("choose the one folder you want", message)
        self.assertEqual(len(files), 6)

    def test_a_mod_that_is_already_laid_out_is_left_alone(self):
        verdict, files = self.install(FakeTree(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("a.pak")])])])))
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["OakGame/Content/Paks/a.pak"])
        self.assertEqual(fakes.LOG, [])

    def test_choosing_the_parent_folder_refuses_identical_pak_names(self):
        verdict, files = self.install(firmware_archive().find("~Mods"))
        self.assertEqual(verdict, INVALID)
        self.assertEqual(len(files), 6)                                   # nothing moved
        warnings = [m for lvl, m in fakes.LOG if lvl == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"'{PAK}' exists in both 'Z_ShowFirmware_P' and 'Z_ShowFirware_DE_P'", warnings[0])
        self.assertIn("choose the one folder you want", warnings[0])

    def test_refusal_is_logged_once_per_reason(self):
        chosen = firmware_archive().find("~Mods")
        same = self.checker()
        for _ in range(3):
            same.dataLooksValid(chosen)
        self.assertEqual(len(fakes.LOG), 1)

    def test_choosing_the_parent_folder_places_every_subfolder(self):
        verdict, files = self.install(distinct_archive().find("~Mods"))
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, sorted(f"OakGame/Content/Paks/{n}/{n}.pak" for n in LANGS))

    def test_emptied_folders_are_removed(self):
        game = self.game()
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        mod = self.mod_folder(chosen)
        game.test_fire(fakes.FakeMod(mod))
        self.assertEqual(sorted(p.name for p in mod.iterdir()), ["OakGame", "meta.ini"])

    def test_choosing_one_subfolder_places_its_pak_in_paks(self):
        verdict, files = self.install(firmware_archive().find("~Mods/Z_ShowFirmware_P"))
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, [f"OakGame/Content/Paks/{PAK}"])

    def test_pak_subdir_setting_is_honoured(self):
        _, files = self.install(distinct_archive().find("~Mods"), BORDERLANDS_3, "~mods")
        self.assertEqual(files, sorted(f"OakGame/Content/Paks/~mods/{n}/{n}.pak" for n in LANGS))

    def test_notice_is_written_to_the_log_on_install(self):
        chosen = distinct_archive().find("~Mods")
        game = self.game()
        game._checker.dataLooksValid(chosen)
        self.assertEqual(fakes.LOG, [])                                   # checking a valid plan never logs
        game.test_fire(fakes.FakeMod(self.mod_folder(chosen)))
        (level, message), = fakes.LOG
        self.assertEqual(level, "info")
        self.assertIn("The chosen folder holds mod content in subfolders", message)
        self.assertIn("6 pak item(s) into OakGame/Content/Paks/", message)

    def test_loose_pak_placement_is_logged(self):
        self.install(firmware_archive().find("~Mods/Z_ShowFirmware_P"))
        (_, message), = fakes.LOG
        self.assertIn("placed 1 loose file(s)", message)

    def test_games_without_nested_rules_do_not_change(self):
        verdict, files = self.install(distinct_archive().find("~Mods"), BORDERLANDS_2)
        self.assertEqual(verdict, INVALID)
        self.assertEqual(len(files), 6)
        self.assertEqual(fakes.LOG, [])


class WhenFilesAreMoved(Flow):
    """The files are only moved for content the checker accepted in the dialog."""

    def test_nothing_moves_when_the_dialog_never_accepted_the_content(self):
        game = self.game()
        mod = self.mod_folder(distinct_archive().find("~Mods"))
        game.test_fire(fakes.FakeMod(mod))                                # no check happened
        self.assertEqual(len(self.files(mod)), 6)
        self.assertFalse((mod / "OakGame").exists())

    def test_nothing_moves_when_the_installed_content_differs_from_what_was_accepted(self):
        game = self.game()
        game._checker.dataLooksValid(distinct_archive().find("~Mods"))
        other = self.mod_folder(chosen_of(E("Other", True, [E("o.pak")])))   # e.g. the user changed the tree
        game.test_fire(fakes.FakeMod(other))
        self.assertEqual(self.files(other), ["Other/o.pak"])

    def test_accepted_content_is_placed_once(self):
        game = self.game()
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        game.test_fire(fakes.FakeMod(self.mod_folder(chosen)))
        again = self.mod_folder(chosen)
        game.test_fire(fakes.FakeMod(again))                              # same names, but nothing pending
        self.assertEqual(len(self.files(again)), 6)
        self.assertFalse((again / "OakGame").exists())

    def test_a_later_refused_choice_cancels_the_pending_placement(self):
        game = self.game()
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        game._checker.dataLooksValid(firmware_archive().find("~Mods"))    # user switched to a folder we refuse
        mod = self.mod_folder(chosen)
        game.test_fire(fakes.FakeMod(mod))
        self.assertFalse((mod / "OakGame").exists())

    def test_checking_an_archive_root_does_not_cancel_it(self):
        game = self.game()
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        game._checker.dataLooksValid(FakeTree(E("OakGame", True)))        # e.g. a mod list check
        mod = self.mod_folder(chosen)
        game.test_fire(fakes.FakeMod(mod))
        self.assertTrue((mod / "OakGame").exists())

    def test_hook_calls_while_another_game_is_active_do_not_consume_it(self):
        game = self.game()
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        game.test_org.managed = None                                      # another game is active
        game.test_fire(fakes.FakeMod(self.mod_folder(chosen)))
        self.assertIsNotNone(game._checker._pending)

    def test_a_destination_taken_on_disk_is_reported_and_left_alone(self):
        game = self.game()
        chosen = chosen_of(E("Foo", True, [E("b.pak")]))
        game._checker.dataLooksValid(chosen)
        mod = self.mod_folder(chosen)
        (mod / "OakGame/Content/Paks/Foo").mkdir(parents=True)            # appeared after the check
        game._checker._pending = relocate.top_level_signature(mod)        # (as if the check saw it)
        game.test_fire(fakes.FakeMod(mod))
        self.assertTrue((mod / "Foo/b.pak").exists())
        warnings = [m for lvl, m in fakes.LOG if lvl == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not place 'Foo' into OakGame/Content/Paks/", warnings[0])

    def test_loose_files_in_a_chosen_folder_are_placed_for_the_willow_games(self):
        verdict, files = self.install(chosen_of(E("Patch.upk"), E("readme.txt")), BORDERLANDS_2)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["WillowGame/CookedPCConsole/Patch.upk", "readme.txt"])

    def test_loose_file_whose_name_is_taken_is_reported_and_kept(self):
        game = self.game()
        chosen = chosen_of(E("x.pak"))
        game._checker.dataLooksValid(chosen)
        mod = self.mod_folder(chosen)
        (mod / "OakGame/Content/Paks").mkdir(parents=True)
        (mod / "OakGame/Content/Paks/x.pak").write_text("")
        game._checker._pending = relocate.top_level_signature(mod)        # (as if the check saw it)
        game.test_fire(fakes.FakeMod(mod))
        self.assertTrue((mod / "x.pak").exists())
        warnings = [m for lvl, m in fakes.LOG if lvl == "warning"]
        self.assertIn("could not place 'x.pak' into OakGame/Content/Paks/", warnings[0])
        self.assertFalse([m for lvl, m in fakes.LOG if "placed" in m])

    def test_a_failure_while_placing_never_breaks_the_install(self):
        game = self.game()
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        mod = self.mod_folder(chosen)
        game._pak_subdir = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        game.test_fire(fakes.FakeMod(mod))                                # must not raise
        self.assertTrue([m for lvl, m in fakes.LOG if "could not place the content" in m])


class PakLayouts(Flow):
    def test_a_paks_folder_is_the_target_not_a_wrapper(self):
        chosen = chosen_of(E("Paks", True, [E("a_P.pak"), E("a_P.utoc"), E("Sub", True, [E("b.pak")])]))
        verdict, files = self.install(chosen)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["OakGame/Content/Paks/Sub/b.pak",
                                 "OakGame/Content/Paks/a_P.pak", "OakGame/Content/Paks/a_P.utoc"])

    def test_anchor_match_ignores_case(self):
        _, files = self.install(chosen_of(E("PAKS", True, [E("a.pak")])))
        self.assertEqual(files, ["OakGame/Content/Paks/a.pak"])

    def test_known_game_folder_next_to_nested_content_is_kept(self):
        verdict, files = self.install(chosen_of(E("OakGame", True, [E("x.txt")]), E("Extra", True, [E("e.pak")])))
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["OakGame/Content/Paks/Extra/e.pak", "OakGame/x.txt"])

    def test_nested_pak_depth_is_kept(self):
        _, files = self.install(chosen_of(E("A", True, [E("B", True, [E("deep.pak")])])))
        self.assertEqual(files, ["OakGame/Content/Paks/A/B/deep.pak"])


class SdkLayouts(Flow):
    def test_nested_sdkmods_are_flattened_into_sdk_mods(self):
        verdict, files = self.install(chosen_of(E("Cat", True, [E("one.sdkmod"), E("Sub", True, [E("two.sdkmod")])])))
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["sdk_mods/one.sdkmod", "sdk_mods/two.sdkmod"])

    def test_flatten_refuses_name_collisions(self):
        verdict, _ = self.install(chosen_of(E("A", True, [E("x.sdkmod")]), E("B", True, [E("X.sdkmod")])))
        self.assertEqual(verdict, INVALID)

    def test_flatten_refuses_folders_with_other_files(self):
        verdict, _ = self.install(chosen_of(E("A", True, [E("x.sdkmod"), E("readme.txt")])))
        self.assertEqual(verdict, INVALID)

    def test_python_package_folder_moves_whole(self):
        verdict, files = self.install(chosen_of(E("CoolMod", True, [E("__init__.py"), E("data.json")])))
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["sdk_mods/CoolMod/__init__.py", "sdk_mods/CoolMod/data.json"])

    def test_double_nested_package_is_unwrapped(self):
        _, files = self.install(chosen_of(E("CoolMod", True, [E("CoolMod", True, [E("__init__.py")])])))
        self.assertEqual(files, ["sdk_mods/CoolMod/__init__.py"])

    def test_double_nested_package_with_extra_files_is_refused(self):
        verdict, _ = self.install(chosen_of(E("CoolMod", True, [E("CoolMod", True, [E("__init__.py")]), E("notes.md")])))
        self.assertEqual(verdict, INVALID)

    def test_folder_with_a_dot_is_not_a_python_module(self):
        verdict, _ = self.install(chosen_of(E("My Mod v1.2", True, [E("__init__.py")])))
        self.assertEqual(verdict, INVALID)

    def test_bl3_has_no_sdk_rules(self):
        verdict, _ = self.install(chosen_of(E("Pkg", True, [E("__init__.py")])), BORDERLANDS_3)
        self.assertEqual(verdict, INVALID)


class RefusesToGuess(Flow):
    def verdict(self, *children):
        return self.checker().dataLooksValid(chosen_of(*children))

    def test_unknown_folder_among_good_ones_makes_it_invalid(self):
        self.assertEqual(self.verdict(E("Good", True, [E("a.pak")]), E("misc", True, [E("data.bin")])), INVALID)

    def test_folder_with_both_paks_and_sdkmods_is_ambiguous(self):
        self.assertEqual(self.verdict(E("Mixed", True, [E("a.pak"), E("b.sdkmod")])), INVALID)

    def test_folder_that_is_both_package_and_pak_is_ambiguous(self):
        self.assertEqual(self.verdict(E("Both", True, [E("__init__.py"), E("a.pak")])), INVALID)

    def test_colliding_destinations_are_refused(self):
        self.assertEqual(self.verdict(E("Paks", True, [E("Foo", True, [E("a.pak")])]),
                                      E("Foo", True, [E("b.pak")])), INVALID)

    def test_identical_pak_names_in_siblings_are_refused_ignoring_case_and_depth(self):
        self.assertEqual(self.verdict(E("A", True, [E("x.pak")]),
                                      E("B", True, [E("Sub", True, [E("X.PAK")])])), INVALID)

    def test_different_pak_names_are_not_a_clash(self):
        self.assertEqual(self.verdict(E("A", True, [E("a.pak")]), E("B", True, [E("b.pak")])), VALID)

    def test_identical_names_in_different_folders_of_one_wrapper_are_refused_too(self):
        # ~Mods/<variant>/x.pak is a wrapper of variants, however deep the variants are
        self.assertEqual(self.verdict(E("A", True, [E("v1", True, [E("x.pak")]), E("v2", True, [E("x.pak")])])), INVALID)

    def test_the_same_name_with_several_extensions_in_one_folder_is_not_a_clash(self):
        self.assertEqual(self.verdict(E("A", True, [E("x.pak"), E("x.utoc"), E("x.ucas")])), VALID)

    def test_identical_names_under_a_paks_folder_are_refused_too(self):
        self.assertEqual(self.verdict(E("Paks", True, [E("A", True, [E("x.pak")]), E("B", True, [E("x.pak")])])), INVALID)

    def test_chosen_folder_with_no_recognised_content(self):
        self.assertEqual(self.verdict(E("Stuff", True, [E("notes.txt")])), INVALID)
        self.assertEqual(self.verdict(), INVALID)

    def test_oversized_tree_is_not_scanned(self):
        tree = FakeTree(*[E(f"d{i}", True, [E("a.pak")]) for i in range(50)])
        self.assertIsNone(layout.nodes_from_tree(tree, limit=40))
        self.assertEqual(len(layout.nodes_from_tree(tree, limit=1000)), 50)

    def test_oversized_mod_folder_is_not_scanned(self):
        mod = self.mod_folder(FakeTree(*[E(f"d{i}", True, [E("a.pak")]) for i in range(50)]))
        self.assertIsNone(relocate.nodes_from_dir(mod, limit=40))
        self.assertEqual(len(relocate.nodes_from_dir(mod, limit=1000)), 50)


class NameClashes(Flow):
    """A destination that already exists in the archive means "do not guess"."""

    def test_destination_already_in_the_archive(self):
        chosen = chosen_of(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("Foo", True, [E("a.pak")])])])]),
                           E("Foo", True, [E("b.pak")]))
        self.assertEqual(self.checker().dataLooksValid(chosen), INVALID)

    def test_clash_check_ignores_case(self):
        chosen = chosen_of(E("oakgame", True, [E("content", True, [E("paks", True, [E("FOO", True, [E("a.pak")])])])]),
                           E("foo", True, [E("b.pak")]))
        self.assertEqual(self.checker().dataLooksValid(chosen), INVALID)

    def test_existing_paks_folder_without_a_clash_is_merged_into(self):
        chosen = chosen_of(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("Other", True, [E("o.pak")])])])]),
                           E("Foo", True, [E("b.pak")]))
        verdict, files = self.install(chosen)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["OakGame/Content/Paks/Foo/b.pak", "OakGame/Content/Paks/Other/o.pak"])


class Planner(unittest.TestCase):
    def test_plan_describes_counts_per_rule(self):
        top = [layout.Node("A", True, (layout.Node("a.pak", False),)),
               layout.Node("B", True, (layout.Node("b.sdkmod", False),))]
        plan = layout.plan_nested(BORDERLANDS_4, top, "")
        text = plan.describe("X")
        self.assertIn("1 pak item(s) into OakGame/Content/Paks/", text)
        self.assertIn("1 sdk mod item(s) into sdk_mods/", text)

    def test_no_plan_when_everything_is_a_known_game_folder(self):
        top = [layout.Node("OakGame", True, (layout.Node("x.pak", False),))]
        self.assertIsNone(layout.plan_nested(BORDERLANDS_4, top, ""))


PHASELOOT = [
    E("oo2core_9_win64.dll"), E("PhaseLoot.exe"), E("README.txt"),
    E("Presets", True, [E("Borderlands 4.json")]),
    E("Settings", True, [E("Rarity_Settings.json"), E("Workbench_Build.log")]),
    E("tools", True, [E("editor.py"), E("footer_template.pak"), E("template.ncs")]),
]


def phaseloot_archive():
    """The real archive: PhaseLoot/<exe, dll, folders, and a .pak template under tools/>."""
    return FakeTree(E("PhaseLoot", True, [E(c.name(), c.isDir(), list(c.children)) for c in PHASELOOT]))


class ProgramRules(Flow):
    def test_an_exe_in_a_folder_at_the_archive_root_goes_to_tools_with_its_contents(self):
        verdict, files = self.install(phaseloot_archive())
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, sorted(f"Tools/PhaseLoot/{p}" for p in [
            "PhaseLoot.exe", "README.txt", "oo2core_9_win64.dll", "Presets/Borderlands 4.json",
            "Settings/Rarity_Settings.json", "Settings/Workbench_Build.log",
            "tools/editor.py", "tools/footer_template.pak", "tools/template.ncs"]))

    def test_a_pak_inside_a_program_folder_is_not_game_content(self):
        _, files = self.install(phaseloot_archive())
        self.assertFalse([f for f in files if f.startswith("OakGame")])   # the template stays with its tool

    def test_a_chosen_program_folder_is_not_mistaken_for_a_wrapper(self):
        chosen = FakeTree(E("Wrapper", True, [E("PhaseLoot", True, [E("PhaseLoot.exe"), E("t.pak")])])).find("Wrapper")
        verdict, files = self.install(chosen)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["Tools/PhaseLoot/PhaseLoot.exe", "Tools/PhaseLoot/t.pak"])

    def test_an_exe_at_the_archive_root_takes_everything_into_a_folder_named_after_the_mod(self):
        root = FakeTree(E("tool.exe"), E("readme.txt"), E("data", True, [E("d.bin")]), E("libs", True, [E("x.dll")]))
        verdict, files = self.install(root, name="My Cool Tool")
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["Tools/My Cool Tool/data/d.bin", "Tools/My Cool Tool/libs/x.dll",
                                 "Tools/My Cool Tool/readme.txt", "Tools/My Cool Tool/tool.exe"])

    def test_game_folders_next_to_a_root_exe_stay_where_they_are(self):
        root = FakeTree(E("tool.exe"), E("OakGame", True, [E("Content", True, [E("Paks", True, [E("a.pak")])])]),
                        E("notes.txt"))
        _, files = self.install(root, name="Tool Mod")
        self.assertEqual(files, ["OakGame/Content/Paks/a.pak", "Tools/Tool Mod/notes.txt", "Tools/Tool Mod/tool.exe"])

    def test_an_exe_at_the_root_wins_over_pak_and_sdk_folders(self):
        root = FakeTree(E("t.exe"), E("data", True, [E("x.pak")]), E("m", True, [E("a.sdkmod")]))
        _, files = self.install(root, name="T")
        self.assertEqual(files, ["Tools/T/data/x.pak", "Tools/T/m/a.sdkmod", "Tools/T/t.exe"])

    def test_the_mod_name_is_made_safe_as_a_folder_name(self):
        _, files = self.install(FakeTree(E("t.exe")), name='A:B? "x"/y.')
        self.assertEqual(files, ["Tools/A_B_ _x__y/t.exe"])
        self.assertEqual(relocate.safe_folder_name("   "), "Tool")

    def test_a_chosen_folder_with_an_exe_works_the_same_way(self):
        _, files = self.install(chosen_of(E("tool.exe"), E("readme.txt")), name="Chosen")
        self.assertEqual(files, ["Tools/Chosen/readme.txt", "Tools/Chosen/tool.exe"])

    def test_programs_folders_paks_and_sdk_mods_side_by_side_each_go_where_they_belong(self):
        root = FakeTree(
            E("PhaseLoot", True, [E("PhaseLoot.exe"), E("t.pak")]),
            E("Pak Mod", True, [E("a_P.pak")]),
            E("SdkStuff", True, [E("one.sdkmod")]),
            E("Pkg", True, [E("__init__.py")]),
        )
        verdict, files = self.install(root)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, [
            "OakGame/Content/Paks/Pak Mod/a_P.pak",
            "Tools/PhaseLoot/PhaseLoot.exe", "Tools/PhaseLoot/t.pak",
            "sdk_mods/Pkg/__init__.py", "sdk_mods/one.sdkmod"])

    def test_one_unrecognised_folder_still_refuses_the_whole_archive(self):
        root = FakeTree(E("PhaseLoot", True, [E("PhaseLoot.exe")]), E("misc", True, [E("data.bin")]))
        verdict, files = self.install(root)
        self.assertEqual(verdict, INVALID)
        self.assertEqual(files, ["PhaseLoot/PhaseLoot.exe", "misc/data.bin"])

    def test_every_game_places_programs(self):
        for spec in ALL_SPECS:
            verdict, files = self.install(phaseloot_archive(), spec)
            self.assertEqual(verdict, VALID, spec.short_name)
            self.assertIn("Tools/PhaseLoot/PhaseLoot.exe", files, spec.short_name)

    def test_the_destination_already_in_the_archive_refuses(self):
        root = FakeTree(E("PhaseLoot", True, [E("PhaseLoot.exe")]),
                        E("Tools", True, [E("PhaseLoot", True, [E("old.exe")])]))
        self.assertEqual(self.checker().dataLooksValid(root), INVALID)

    def test_a_tools_folder_on_disk_that_already_has_the_name_is_reported(self):
        game = self.game()
        root = FakeTree(E("t.exe"))
        game._checker.dataLooksValid(root)
        mod = self.mod_folder(root)
        (mod / "Tools/Mod").mkdir(parents=True)
        (mod / "Tools/Mod/t.exe").write_text("")                        # a previous copy, e.g. a merge install
        game._checker._pending = relocate.top_level_signature(mod)
        game.test_fire(fakes.FakeMod(mod, name="Mod"))
        self.assertTrue((mod / "t.exe").exists())                         # left alone
        self.assertTrue([m for lvl, m in fakes.LOG if lvl == "warning" and "could not place 't.exe'" in m])

    def test_the_log_says_what_was_done(self):
        self.install(phaseloot_archive())
        self.assertTrue([m for lvl, m in fakes.LOG if lvl == "info" and "holds a program (.exe) at its top level" in m
                         or "1 program item(s) into Tools/" in m])


class DocumentationFolders(Flow):
    """Folders of text and pictures are left alone instead of making the archive invalid."""

    def test_a_docs_folder_next_to_a_program_folder_stays_in_the_mod_root(self):
        root = FakeTree(E("PhaseLoot", True, [E("PhaseLoot.exe")]),
                        E("docs", True, [E("manual.pdf"), E("Shots", True, [E("a.png")])]))
        verdict, files = self.install(root)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["Tools/PhaseLoot/PhaseLoot.exe", "docs/Shots/a.png", "docs/manual.pdf"])

    def test_a_docs_folder_next_to_a_pak_folder(self):
        root = FakeTree(E("Mod", True, [E("a_P.pak")]), E("Screenshots", True, [E("a.JPG"), E("b.webp")]))
        verdict, files = self.install(root)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["OakGame/Content/Paks/Mod/a_P.pak", "Screenshots/a.JPG", "Screenshots/b.webp"])

    def test_a_docs_folder_next_to_the_game_folders_is_fine_as_it_is(self):
        root = FakeTree(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("a.pak")])])]),
                        E("docs", True, [E("readme.md")]))
        verdict, files = self.install(root)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files, ["OakGame/Content/Paks/a.pak", "docs/readme.md"])
        self.assertEqual(fakes.LOG, [])                                   # nothing was moved, nothing to report

    def test_docs_folders_are_recognised_by_file_name_too(self):
        """README / INSTALL / changelog style names, with or without an extension, are documentation."""
        notes = ["README", "Read Me First.txt", "INSTALL.md", "Installation.ini", "setup.ini", "start", "Changelog",
                 "changes.txt", "LICENSE", "credits.txt", "FAQ.md", "what's new.txt"]
        root = FakeTree(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("a.pak")])])]),
                        E("About", True, [E(n) for n in notes]))
        verdict, files = self.install(root)
        self.assertEqual(verdict, VALID)
        self.assertEqual(files[0], "About/Changelog")                      # untouched, in the mod root
        self.assertIn("OakGame/Content/Paks/a.pak", files)
        self.assertEqual(len(files), len(notes) + 1)

    def test_a_folder_with_an_unknown_file_is_not_documentation_even_beside_readmes(self):
        for stray in ("start.bat", "data.bin", "setup.cfg"):                   # a name like "start" alone is not enough
            root = FakeTree(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("a.pak")])])]),
                            E("Extras", True, [E("README.txt"), E(stray)]))
            verdict, _ = self.install(root)
            self.assertNotEqual(verdict, VALID, stray)

    def test_setup_exe_is_a_program_not_documentation(self):
        root = FakeTree(E("OakGame", True, [E("Content", True, [E("Paks", True, [E("a.pak")])])]),
                        E("Extras", True, [E("README.txt"), E("setup.exe")]))
        _, files = self.install(root)
        self.assertIn("Tools/Extras/setup.exe", files)

    def test_an_empty_folder_counts_as_documentation(self):
        root = FakeTree(E("OakGame", True, [E("x.txt")]), E("empty", True))
        self.assertEqual(self.checker().dataLooksValid(root), VALID)

    def test_a_docs_folder_alone_is_still_invalid(self):
        self.assertEqual(self.checker().dataLooksValid(FakeTree(E("docs", True, [E("a.txt")]))), INVALID)

    def test_a_folder_with_anything_else_in_it_is_not_documentation(self):
        root = FakeTree(E("PhaseLoot", True, [E("PhaseLoot.exe")]), E("misc", True, [E("a.txt"), E("b.dll")]))
        self.assertEqual(self.checker().dataLooksValid(root), INVALID)

    def test_documentation_inside_a_root_exe_mod_moves_with_the_rest(self):
        root = FakeTree(E("t.exe"), E("docs", True, [E("a.txt")]))
        _, files = self.install(root, name="T")
        self.assertEqual(files, ["Tools/T/docs/a.txt", "Tools/T/t.exe"])


class RefreshAfterPlacing(Flow):
    """MO2 judges a mod's layout before our install callback runs, so it must be asked to look again."""

    def refreshes(self, game):
        return len(game.test_org.data_changed)

    def test_a_refresh_follows_a_placement(self):
        game = self.game()
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        game.test_fire(fakes.FakeMod(self.mod_folder(chosen), name="Mod"))
        self.assertEqual(self.refreshes(game), 1)

    def test_a_refresh_follows_a_program_placement_and_a_loose_pak(self):
        for archive in (phaseloot_archive(), chosen_of(E("a_P.pak"))):
            game = self.game()
            game._checker.dataLooksValid(archive)
            game.test_fire(fakes.FakeMod(self.mod_folder(archive), name="Mod"))
            self.assertEqual(self.refreshes(game), 1)

    def test_no_refresh_when_nothing_was_placed(self):
        game = self.game()
        mod = self.mod_folder(FakeTree(E("OakGame", True, [E("x.pak")])))
        game._checker.dataLooksValid(FakeTree(E("OakGame", True, [E("x.pak")])))
        game.test_fire(fakes.FakeMod(mod, name="Mod"))
        self.assertEqual(self.refreshes(game), 0)

    def test_no_refresh_when_the_dialog_never_accepted_the_content(self):
        game = self.game()
        game.test_fire(fakes.FakeMod(self.mod_folder(distinct_archive().find("~Mods")), name="Mod"))
        self.assertEqual(self.refreshes(game), 0)

    def test_no_refresh_when_every_move_was_blocked(self):
        game = self.game()
        chosen = chosen_of(E("x.pak"))
        game._checker.dataLooksValid(chosen)
        mod = self.mod_folder(chosen)
        (mod / "OakGame/Content/Paks").mkdir(parents=True)
        (mod / "OakGame/Content/Paks/x.pak").write_text("")
        game._checker._pending = relocate.top_level_signature(mod)
        game.test_fire(fakes.FakeMod(mod, name="Mod"))
        self.assertEqual(self.refreshes(game), 0)

    def test_the_refresh_waits_until_the_callback_has_returned(self):
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        game = self.game()
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        game.test_fire(fakes.FakeMod(self.mod_folder(chosen), name="Mod"))
        self.assertEqual(self.refreshes(game), 0)                      # not from inside MO2's callback
        fakes.QTimer.run_queued()
        self.assertEqual(self.refreshes(game), 1)

    def test_back_to_back_installs_share_one_refresh(self):
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        game = self.game()
        for name in ("One", "Two"):
            archive = distinct_archive().find("~Mods")
            game._checker.dataLooksValid(archive)
            game.test_fire(fakes.FakeMod(self.mod_folder(archive), name=name))
        fakes.QTimer.run_queued()
        self.assertEqual(self.refreshes(game), 1)
        game._checker.dataLooksValid(distinct_archive().find("~Mods"))
        game.test_fire(fakes.FakeMod(self.mod_folder(distinct_archive().find("~Mods")), name="Three"))
        fakes.QTimer.run_queued()
        self.assertEqual(self.refreshes(game), 2)                      # a later install gets its own

    def test_the_mods_current_object_is_used_when_it_is_in_the_list(self):
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        game = self.game()
        game.test_org.modList().names.append("Mod")
        game.test_org.modList().paths["Mod"] = str(self.root)
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        old = fakes.FakeMod(self.mod_folder(chosen), name="Mod")
        game.test_fire(old)
        fakes.QTimer.run_queued()
        (current,) = game.test_org.data_changed
        self.assertIsNot(current, old)                                  # re-fetched, not the stale pointer
        self.assertEqual(current.name(), "Mod")

    def test_a_failing_refresh_never_breaks_the_install(self):
        game = self.game()
        game.test_org.modDataChanged = mock.Mock(side_effect=RuntimeError("no refresh"))
        chosen = distinct_archive().find("~Mods")
        game._checker.dataLooksValid(chosen)
        game.test_fire(fakes.FakeMod(self.mod_folder(chosen), name="Mod"))
        self.assertTrue([m for lvl, m in fakes.LOG if "could not refresh MO2's view of 'Mod'" in m])

    def test_a_failed_refresh_does_not_block_the_next_one(self):
        game = self.game()
        calls = []
        game.test_org.modDataChanged = lambda mod: (calls.append(1), (_ for _ in ()).throw(RuntimeError("x")))
        for name in ("A", "B"):
            archive = distinct_archive().find("~Mods")
            game._checker.dataLooksValid(archive)
            game.test_fire(fakes.FakeMod(self.mod_folder(archive), name=name))
        self.assertEqual(len(calls), 2)


class ProgramAnnouncement(Flow):
    def announcements(self):
        return [d for d in fakes.DIALOGS if d[0] == "information"]

    def test_a_mod_with_programs_is_announced_after_install(self):
        self.install(phaseloot_archive(), name="PhaseLoot")
        (_, title, text), = self.announcements()
        self.assertEqual(title, "Borderlands tools")
        self.assertIn("'PhaseLoot' contains 1 program(s)", text)
        self.assertIn("PhaseLoot/PhaseLoot.exe", text)
        self.assertIn("Tools menu", text)
        self.assertIn("not started automatically", text)
        self.assertIn("Enable the mod first", text)

    def test_a_mod_that_already_had_a_tools_folder_is_announced_too(self):
        root = FakeTree(E("Tools", True, [E("App", True, [E("app.exe")])]))
        verdict, _ = self.install(root, name="App Mod")
        self.assertEqual(verdict, VALID)
        self.assertIn("App/app.exe", self.announcements()[0][2])

    def test_mods_without_programs_are_not_announced(self):
        self.install(distinct_archive())
        self.install(FakeTree(E("OakGame", True, [E("x.pak")])))
        self.assertEqual(self.announcements(), [])

    def test_a_long_list_is_trimmed(self):
        root = FakeTree(E("Tools", True, [E(f"t{i:02d}", True, [E(f"t{i:02d}.exe")]) for i in range(13)]))
        self.install(root)
        self.assertIn("and 3 more", self.announcements()[0][2])

    def test_it_never_interrupts_a_game_that_is_not_active(self):
        game = self.game()
        game.test_org.managed = None
        game.test_fire(fakes.FakeMod(self.mod_folder(phaseloot_archive()), name="x"))
        self.assertEqual(self.announcements(), [])

    def test_a_failure_to_list_never_breaks_the_install(self):
        with mock.patch("borderlands.game.tools.discover", side_effect=RuntimeError("boom")):
            self.install(phaseloot_archive())
        self.assertTrue([m for lvl, m in fakes.LOG if "could not list the programs" in m])


if __name__ == "__main__":
    unittest.main()
