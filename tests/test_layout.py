import unittest

from borderlands import layout
from borderlands.layout import Verdict
from borderlands.spec import ALL_SPECS, BORDERLANDS_2, BORDERLANDS_3, BORDERLANDS_4


class ResolveTarget(unittest.TestCase):
    def test_empty_subdir_collapses(self):
        self.assertEqual(layout.resolve_target("OakGame/Content/Paks/{pak_subdir}", ""), "OakGame/Content/Paks/")

    def test_subdir_is_inserted_and_slashes_trimmed(self):
        self.assertEqual(layout.resolve_target("OakGame/Content/Paks/{pak_subdir}", "/~mods\\"),
                         "OakGame/Content/Paks/~mods/")

    def test_plain_target(self):
        self.assertEqual(layout.resolve_target("sdk_mods/", "ignored"), "sdk_mods/")


class MoveTarget(unittest.TestCase):
    def test_bl4_pak_trio_goes_to_paks_root_by_default(self):
        for name in ("a_P.pak", "a_P.utoc", "a_P.UCAS"):
            self.assertEqual(layout.move_target(BORDERLANDS_4, name, False, ""), "OakGame/Content/Paks/")

    def test_bl4_sdkmod(self):
        self.assertEqual(layout.move_target(BORDERLANDS_4, "x.sdkmod", False, ""), "sdk_mods/")

    def test_bl3_pak_uses_setting(self):
        self.assertEqual(layout.move_target(BORDERLANDS_3, "x.pak", False, "~mods"), "OakGame/Content/Paks/~mods/")

    def test_bl3_has_no_sdkmod_rule(self):
        self.assertIsNone(layout.move_target(BORDERLANDS_3, "x.sdkmod", False, ""))

    def test_directories_are_never_moved(self):
        self.assertIsNone(layout.move_target(BORDERLANDS_4, "thing.pak", True, ""))

    def test_bl2_upk_goes_to_cookedpcconsole(self):
        self.assertEqual(layout.move_target(BORDERLANDS_2, "x.upk", False, ""), "WillowGame/CookedPCConsole/")

    def test_dotfile_has_no_suffix(self):
        self.assertIsNone(layout.move_target(BORDERLANDS_4, ".pak", False, ""))


class Check(unittest.TestCase):
    def test_game_folder_is_valid_case_insensitively(self):
        self.assertIs(layout.check(BORDERLANDS_4, [("OakGame", True)]), Verdict.VALID)
        self.assertIs(layout.check(BORDERLANDS_4, [("SDK_MODS", True)]), Verdict.VALID)

    def test_tools_folder_is_valid_for_every_game(self):
        for spec in ALL_SPECS:
            for name in ("Tools", "tools", "TOOLS"):
                self.assertIs(layout.check(spec, [(name, True)]), Verdict.VALID, (spec.short_name, name))

    def test_tools_folder_is_not_mistaken_for_nested_content(self):
        top = [layout.Node("Tools", True, (layout.Node("PhaseLoot", True, (layout.Node("a.pak", False),)),))]
        self.assertIsNone(layout.plan_nested(BORDERLANDS_4, top, ""))

    def test_loose_pak_is_fixable(self):
        self.assertIs(layout.check(BORDERLANDS_4, [("x.pak", False)]), Verdict.FIXABLE)

    def test_fixable_beats_valid(self):
        self.assertIs(layout.check(BORDERLANDS_4, [("OakGame", True), ("x.sdkmod", False)]), Verdict.FIXABLE)

    def test_ultra_plus_layout_with_metadata_json_is_valid(self):
        # Regression: a real BL4 mod (OakGame/ plus metadata.json) was rejected.
        self.assertIs(layout.check(BORDERLANDS_4, [("OakGame", True), ("metadata.json", False)]), Verdict.VALID)

    def test_unknown_loose_files_never_invalidate(self):
        entries = [("OakGame", True), ("whatever.bin", False), ("LICENSE", False)]
        self.assertIs(layout.check(BORDERLANDS_4, entries), Verdict.VALID)

    def test_only_unknown_files_is_invalid(self):
        self.assertIs(layout.check(BORDERLANDS_4, [("metadata.json", False)]), Verdict.INVALID)

    def test_readme_is_ignored_but_not_enough(self):
        self.assertIs(layout.check(BORDERLANDS_4, [("readme.txt", False)]), Verdict.INVALID)
        self.assertIs(layout.check(BORDERLANDS_4, [("x.pak", False), ("readme.txt", False)]), Verdict.FIXABLE)

    def test_unknown_entry_is_invalid(self):
        self.assertIs(layout.check(BORDERLANDS_4, [("OakGame", True), ("random", True)]), Verdict.INVALID)

    def test_empty_mod_is_invalid(self):
        self.assertIs(layout.check(BORDERLANDS_4, []), Verdict.INVALID)

    def test_game_folders_do_not_leak_between_games(self):
        self.assertIs(layout.check(BORDERLANDS_3, [("sdk_mods", True)]), Verdict.INVALID)
        self.assertIs(layout.check(BORDERLANDS_2, [("OakGame", True)]), Verdict.INVALID)


class Specs(unittest.TestCase):
    def test_identifiers_are_unique(self):
        for attr in ("plugin_name", "game_name", "short_name", "steam_id", "binary"):
            values = [getattr(s, attr) for s in ALL_SPECS]
            self.assertEqual(len(values), len(set(values)), attr)

    def test_pak_setting_only_where_paks_are_moved(self):
        for spec in ALL_SPECS:
            uses_pak = any("{pak_subdir}" in r.target for r in spec.move_rules)
            self.assertEqual(spec.default_pak_subdir is not None, uses_pak, spec.short_name)


if __name__ == "__main__":
    unittest.main()
