import dataclasses
import unittest

from tests import fakes

fakes.install()

import borderlands  # noqa: E402
from borderlands.game import BorderlandsGame, BorderlandsModDataChecker  # noqa: E402
from borderlands.spec import BORDERLANDS_3, BORDERLANDS_4  # noqa: E402
from tests.fakes import FakeEntry, FakeTree  # noqa: E402


def game_plugins():
    return [p for p in borderlands.createPlugins() if isinstance(p, BorderlandsGame)]


class CreatePlugins(unittest.TestCase):
    def test_a_tools_plugin_comes_with_the_games(self):
        from borderlands.toolsmenu import BorderlandsTools
        plugins = borderlands.createPlugins()
        self.assertEqual(sum(isinstance(p, BorderlandsTools) for p in plugins), 1)
        from borderlands.sdkdiagnose import SdkDiagnose
        self.assertEqual(sum(isinstance(p, SdkDiagnose) for p in plugins), 1)
        self.assertEqual(len(plugins), 7)
        (tools,) = [p for p in plugins if isinstance(p, BorderlandsTools)]
        self.assertIsNotNone(tools.diagnose)

    def test_one_plugin_per_game_with_unique_names(self):
        plugins = game_plugins()
        self.assertEqual(len(plugins), 5)
        self.assertEqual(len({p.name() for p in plugins}), 5)
        self.assertEqual(len({p.gameShortName() for p in plugins}), 5)

    def test_binary_and_steam_ids(self):
        by_short = {p.gameShortName(): p for p in game_plugins()}
        self.assertEqual(by_short["borderlands2"].steamAPPId(), "49520")
        self.assertEqual(by_short["borderlands4"].binaryName(), "OakGame/Binaries/Win64/Borderlands4.exe")
        self.assertEqual(by_short["borderlandsgotyenhanced"].steamAPPId(), "729040")

    def test_nexus_identity_matches_nexusmods(self):
        by_short = {p.gameShortName(): (p.gameNexusName(), p.nexusGameID()) for p in game_plugins()}
        self.assertEqual(by_short, {
            "borderlandsgotyenhanced": ("borderlandsgotyenhanced", 2776),
            "borderlands2": ("borderlands2", 428),
            "borderlandspresequel": ("borderlandspresequel", 875),
            "borderlands3": ("borderlands3", 2953),
            "borderlands4": ("borderlands4", 8148),
        })

    def test_settings_only_for_pak_games(self):
        by_short = {p.gameShortName(): p for p in game_plugins()}
        self.assertEqual([s.key for s in by_short["borderlands3"].settings()], ["hide_documentation", "pak_subdir"])
        self.assertEqual({s.key: s.default for s in by_short["borderlands3"].settings()}["pak_subdir"], "~mods")
        self.assertEqual({s.key: s.default for s in by_short["borderlands4"].settings()}["pak_subdir"], "")
        self.assertEqual([(s.key, s.default) for s in by_short["borderlands2"].settings()], [("hide_documentation", False)])

    def test_paths(self):
        game = BorderlandsGame(BORDERLANDS_4)
        game.setGamePath("C:/Apps/Steam/steamapps/common/Borderlands 4")
        self.assertTrue(game.isInstalled())
        self.assertEqual(game.dataDirectory().absolutePath(), "C:/Apps/Steam/steamapps/common/Borderlands 4")
        self.assertEqual(game.documentsDirectory().absolutePath(), "C:/Users/test/Documents/My Games/Borderlands 4")
        self.assertIn("Saved/Config/Windows/Engine.ini", game.iniFiles())


DESCRIPTOR = ('{"Name":"UltraPlus","Id":"ultraplus-Borderlands4","Author":"someone","Version":"0.2.7",'
              '"Config":"UltraPlusConfig.ini","Dependencies":["dep-a"],"Permalink":"https://example.invalid/p",'
              '"WikiLink":"","SettingsSupport":"false","RelativeLocation":"",'
              '"Data":[{"file":"OakGame/Content/Paks/x.pak"}]}')


class ModInstalledHook(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mod_dir = Path(self._tmp.name)
        (self.mod_dir / "OakGame").mkdir()
        (self.mod_dir / "metadata.json").write_text(DESCRIPTOR)

        self.game = BorderlandsGame(BORDERLANDS_4)
        self.org = fakes.FakeOrganizer()
        self.game.init(self.org)
        self.org.managed = self.game  # this game is the managed one
        self.assertEqual(len(self.org.installed_callbacks), 1)
        self.fire = self.org.installed_callbacks[0]

    def test_sets_version_url_and_stashes_rest_then_hides_file(self):
        mod = fakes.FakeMod(self.mod_dir)
        self.fire(mod)
        self.assertEqual(mod.version().text, "0.2.7")
        self.assertEqual(mod.url(), "https://example.invalid/p")
        name = self.game.name()
        self.assertEqual(mod.plugin_settings[(name, "metadata_author")], "someone")
        self.assertEqual(mod.plugin_settings[(name, "metadata_id")], "ultraplus-Borderlands4")
        self.assertEqual(mod.plugin_settings[(name, "metadata_config")], "UltraPlusConfig.ini")
        self.assertEqual(mod.plugin_settings[(name, "metadata_dependencies")], "dep-a")
        self.assertEqual(mod.plugin_settings[(name, "metadata_settings_support")], "false")
        self.assertNotIn((name, "metadata_relative_location"), mod.plugin_settings)  # empty -> not stored
        self.assertFalse((self.mod_dir / "metadata.json").exists())
        self.assertTrue((self.mod_dir / "metadata.json.mohidden").exists())

    def _write_instance_ini(self, skip_line):
        import tempfile
        from pathlib import Path
        instance = tempfile.TemporaryDirectory()
        self.addCleanup(instance.cleanup)
        (Path(instance.name) / "ModOrganizer.ini").write_text(f"[Settings]\n{skip_line}\nskip_directories=.git\n")
        self.org.base_path = instance.name

    def test_hides_when_instance_skips_mohidden(self):
        self._write_instance_ini("skip_file_suffixes=.mohidden")
        self.fire(fakes.FakeMod(self.mod_dir))
        self.assertTrue((self.mod_dir / "metadata.json.mohidden").exists())

    def test_leaves_file_when_mohidden_no_longer_skipped(self):
        self._write_instance_ini("skip_file_suffixes=.bak")
        mod = fakes.FakeMod(self.mod_dir)
        self.fire(mod)
        self.assertTrue((self.mod_dir / "metadata.json").exists())      # not hidden: it would leak into the VFS
        self.assertFalse((self.mod_dir / "metadata.json.mohidden").exists())
        self.assertEqual(mod.version().text, "0.2.7")                     # metadata still applied

    def test_existing_version_and_url_are_kept(self):
        mod = fakes.FakeMod(self.mod_dir, version="9.9", url="https://nexus.invalid/m")
        self.fire(mod)
        self.assertEqual(mod.version().text, "9.9")
        self.assertEqual(mod.url(), "https://nexus.invalid/m")
        self.assertTrue((self.mod_dir / "metadata.json.mohidden").exists())

    def test_ignored_when_another_game_is_managed(self):
        self.org.managed = BorderlandsGame(BORDERLANDS_3)
        mod = fakes.FakeMod(self.mod_dir)
        self.fire(mod)
        self.assertEqual(mod.version().text, "")
        self.assertTrue((self.mod_dir / "metadata.json").exists())

    def test_unrelated_metadata_json_is_left_alone(self):
        (self.mod_dir / "metadata.json").write_text('{"name": "something else"}')
        mod = fakes.FakeMod(self.mod_dir)
        self.fire(mod)
        self.assertEqual(mod.plugin_settings, {})
        self.assertTrue((self.mod_dir / "metadata.json").exists())

    def test_mod_without_descriptor_is_a_noop(self):
        (self.mod_dir / "metadata.json").unlink()
        self.fire(fakes.FakeMod(self.mod_dir))  # must not raise

    def test_errors_never_propagate(self):
        mod = fakes.FakeMod(self.mod_dir)
        mod.setVersion = lambda v: (_ for _ in ()).throw(RuntimeError("boom"))
        self.fire(mod)  # must not raise
        # Failure happened before hiding, so the file stays for a retry.
        self.assertTrue((self.mod_dir / "metadata.json").exists())


class RedirectHooks(unittest.TestCase):
    """The 'SDK Mod Settings' mod: mapping, mod list entry and run hooks (BL4 only)."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.game_dir, self.mods, self.instance = root / "game", root / "mods", root / "instance"
        settings = self.game_dir / "sdk_mods" / "settings"
        settings.mkdir(parents=True)
        (settings / "a.json").write_text("A")
        self.mods.mkdir()
        self.instance.mkdir()

        fakes.LOG.clear()
        # without the SDK's own pinned mod, so these tests are about the redirect mod alone
        self.game = BorderlandsGame(dataclasses.replace(BORDERLANDS_4, sdk=None))
        self.org = fakes.FakeOrganizer()
        self.org.mods_path, self.org.base_path = str(self.mods), str(self.instance)
        self.game.init(self.org)
        self.game.setGamePath(self.game_dir)
        self.org.managed = self.game
        self.target = self.mods / "SDK Mod Settings" / "sdk_mods" / "settings"

    # -- registration --------------------------------------------------------

    def test_hooks_registered_only_for_games_with_redirects(self):
        self.assertEqual(len(self.org.ui_callbacks), 1)
        self.assertEqual(len(self.org.profile_callbacks), 1)
        self.assertEqual(len(self.org.about_to_run_callbacks), 1)
        self.assertEqual(len(self.org.finished_callbacks), 1)
        self.assertEqual(len(self.org.modList().moved_callbacks), 1)
        self.assertEqual(len(self.org.modList().state_callbacks), 1)
        other = fakes.FakeOrganizer()
        BorderlandsGame(BORDERLANDS_3).init(other)
        self.assertEqual(other.ui_callbacks + other.about_to_run_callbacks + other.finished_callbacks, [])
        self.assertEqual(other.modList().moved_callbacks + other.modList().state_callbacks, [])

    # -- mappings ------------------------------------------------------------

    def test_mapping_is_a_create_target_directory_mapping(self):
        settings, keybinds = self.game.mappings()
        self.assertEqual(settings.source, str(self.target))
        self.assertEqual(settings.destination, str(self.game_dir / "sdk_mods" / "settings"))
        self.assertTrue(settings.is_directory)
        self.assertTrue(settings.create_target)
        self.assertEqual(keybinds.source, str(self.mods / "SDK Mod Settings" / "sdk_mods" / "keybinds"))
        self.assertEqual(keybinds.destination, str(self.game_dir / "sdk_mods" / "keybinds"))
        self.assertTrue(keybinds.create_target)

    def test_keybinds_writes_go_to_the_same_mod_and_never_seed_from_the_game_folder(self):
        keybinds = self.game_dir / "sdk_mods" / "keybinds"
        keybinds.mkdir(parents=True)
        (keybinds / "__init__.py").write_text("package code")
        self.game.mappings()
        kb_target = self.mods / "SDK Mod Settings" / "sdk_mods" / "keybinds"
        self.assertTrue(kb_target.is_dir())
        self.assertEqual(list(kb_target.iterdir()), [])                       # the package is not copied
        self.assertEqual((self.target / "a.json").read_text(), "A")           # settings still seed
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()), ["SDK Mod Settings"])

    def test_the_two_folders_keep_separate_records_of_what_they_held(self):
        from borderlands import redirects
        from borderlands.spec import SDK_KEYBINDS, SDK_MOD_SETTINGS
        state = self.instance / "plugin_state" / "borderlands"
        self.assertNotEqual(redirects.manifest_path(state, SDK_MOD_SETTINGS), redirects.manifest_path(state, SDK_KEYBINDS))
        self.assertEqual(redirects.manifest_path(state, SDK_MOD_SETTINGS).name, "sdk_mod_settings.manifest.json")  # as before
        self.game.mappings()
        (self.mods / "SDK Mod Settings" / "sdk_mods" / "keybinds" / "binds.json").write_text("{}")
        self.game._after_run()
        self.game.mappings()
        self.assertTrue((self.target / "a.json").exists())                    # the other folder's files are not "deleted"
        self.assertFalse(list((self.mods / "SDK Mod Settings").rglob("*.deleted")))
        (self.mods / "SDK Mod Settings" / "sdk_mods" / "keybinds" / "binds.json").unlink()
        self.game._before_run("x")
        self.game.mappings()
        self.assertTrue((self.mods / "SDK Mod Settings" / "sdk_mods" / "keybinds" / "binds.json.deleted").exists())
        self.assertFalse((self.target / "a.json.deleted").exists())

    def test_one_pin_serves_both_folders(self):
        pins = self.game._pins()
        self.assertEqual([p.name for p in pins], ["SDK Mod Settings"])
        self.assertIn("sdk_mods/settings/ and sdk_mods/keybinds/", pins[0].why)
        self.org.modList().names[:] = ["alpha"]
        self.org.ui_callbacks[0](object())
        self.assertEqual(self.org.created, ["SDK Mod Settings"])               # once, not once per folder

    def test_mappings_seed_the_mod_from_the_real_folder(self):
        self.game.mappings()
        self.assertEqual((self.target / "a.json").read_text(), "A")

    def test_no_mappings_for_games_without_redirects_or_when_inactive(self):
        bl3 = BorderlandsGame(BORDERLANDS_3)
        bl3.init(self.org)
        self.org.managed = bl3
        self.assertEqual(bl3.mappings(), [])
        self.org.managed = None
        self.assertEqual(self.game.mappings(), [])
        self.assertFalse(self.target.exists())

    # -- mod list entry ------------------------------------------------------

    def test_ui_init_creates_enables_and_pins_last(self):
        self.org.modList().names[:] = ["alpha", "beta"]
        self.org.ui_callbacks[0](object())
        self.assertEqual(self.org.created, ["SDK Mod Settings"])
        self.assertEqual(self.org.modList().names, ["alpha", "beta", "SDK Mod Settings"])
        self.assertIn("SDK Mod Settings", self.org.modList().active)
        self.assertTrue(self.target.is_dir())

    def test_existing_mod_is_not_recreated_and_is_moved_to_the_end(self):
        names = self.org.modList().names
        names[:] = ["SDK Mod Settings", "alpha", "beta"]
        self.org.about_to_run_callbacks[0]("Borderlands 4")
        self.assertEqual(self.org.created, [])
        self.assertEqual(names, ["alpha", "beta", "SDK Mod Settings"])

    def test_already_correct_does_not_touch_the_list(self):
        mod_list = self.org.modList()
        mod_list.names[:] = ["alpha", "SDK Mod Settings"]
        mod_list.active.add("SDK Mod Settings")
        self.org.profile_callbacks[0]("old", "new")
        self.assertEqual(mod_list.calls, [])

    def test_failed_create_does_not_raise(self):
        self.org.create_returns_none = True
        self.org.ui_callbacks[0](object())
        self.assertEqual(self.org.modList().names, [])

    def test_before_run_never_blocks_the_launch(self):
        self.assertIs(self.org.about_to_run_callbacks[0]("x"), True)

    def test_callbacks_ignore_other_managed_games(self):
        self.org.managed = BorderlandsGame(BORDERLANDS_3)
        self.org.ui_callbacks[0](object())
        self.org.finished_callbacks[0]("x", 0)
        self.assertEqual(self.org.created, [])

    # -- installs -------------------------------------------------------------

    def _install(self, name, listed_before_callback=True):
        """Simulate MO2 installing a mod; new mods start at the end (highest priority)."""
        mod_list = self._set_up_list(["alpha", self.SDK])
        fakes.LOG.clear()
        mod = fakes.FakeMod(self.mods / name, name=name)
        if listed_before_callback:
            mod_list.names.append(name)
        for cb in mod_list.installed_callbacks:
            cb(mod)
        if not listed_before_callback:
            mod_list.names.append(name)  # priority only assigned after the callback ran
        return mod_list

    def test_install_that_lands_after_the_special_mod_is_corrected_at_once(self):
        mod_list = self._install("new mod")
        self.assertEqual(mod_list.names, ["alpha", "new mod", self.SDK])
        (level, message), = fakes.LOG
        self.assertEqual(level, "info")
        self.assertIn("'SDK Mod Settings' was kept last, directly before Overwrite", message)
        self.assertIn("after installing 'new mod'", message)

    def test_install_whose_priority_arrives_late_is_corrected_after_the_refresh(self):
        mod_list = self._install("new mod", listed_before_callback=False)
        self.assertEqual(mod_list.names, ["alpha", self.SDK, "new mod"])   # callback saw nothing wrong
        self.assertEqual(fakes.LOG, [])
        self.org.run_next_refresh()
        self.assertEqual(mod_list.names, ["alpha", "new mod", self.SDK])
        (_, message), = fakes.LOG
        self.assertIn("mod list refresh that followed an install", message)

    def test_back_to_back_installs_schedule_one_refresh_check(self):
        mod_list = self._install("one")
        for cb in mod_list.installed_callbacks:
            cb(fakes.FakeMod(self.mods / "two", name="two"))
        self.assertEqual(len(self.org.next_refresh_callbacks), 1)
        self.org.run_next_refresh()
        self.assertEqual(self.org.next_refresh_callbacks, [])
        for cb in mod_list.installed_callbacks:
            cb(fakes.FakeMod(self.mods / "three", name="three"))
        self.assertEqual(len(self.org.next_refresh_callbacks), 1)          # can schedule again after it ran

    def test_install_when_already_correct_is_silent(self):
        mod_list = self._set_up_list(["alpha", self.SDK])
        for cb in mod_list.installed_callbacks:
            cb(fakes.FakeMod(self.mods / "x", name="x"))                   # not listed, nothing displaced
        self.assertEqual(mod_list.calls, [])
        self.assertEqual(fakes.LOG, [])

    def test_install_ignored_for_another_managed_game_or_missing_mod(self):
        mod_list = self._set_up_list(["alpha"], active=False)               # SDK mod absent
        mod_list.names.append("new mod")
        for cb in mod_list.installed_callbacks:
            cb(fakes.FakeMod(self.mods / "new mod", name="new mod"))
        self.assertEqual(mod_list.calls, [])
        self.assertEqual(self.org.created, [])
        self.org.managed = BorderlandsGame(BORDERLANDS_3)
        self.org.next_refresh_callbacks.clear()
        for cb in mod_list.installed_callbacks:
            cb(fakes.FakeMod(self.mods / "new mod", name="new mod"))
        self.assertEqual(self.org.next_refresh_callbacks, [])

    def test_games_without_redirects_schedule_nothing(self):
        other = fakes.FakeOrganizer()
        bl3 = BorderlandsGame(BORDERLANDS_3)
        bl3.init(other)
        other.managed = bl3
        for cb in other.modList().installed_callbacks:
            cb(fakes.FakeMod(self.mods / "x", name="x"))
        self.assertEqual(other.next_refresh_callbacks, [])

    def test_failure_to_schedule_the_refresh_check_does_not_break_the_install(self):
        mod_list = self._set_up_list(["alpha", self.SDK])
        self.org.next_refresh_raises = True
        for cb in mod_list.installed_callbacks:
            cb(fakes.FakeMod(self.mods / "x", name="x"))                   # must not raise
        self.assertFalse(self.game._refresh_pending)                        # not stuck pending

    def test_metadata_is_still_processed_when_order_enforcement_runs(self):
        (self.mods / "meta mod").mkdir()
        (self.mods / "meta mod" / "metadata.json").write_text(DESCRIPTOR)
        mod_list = self._set_up_list(["alpha", self.SDK])
        mod = fakes.FakeMod(self.mods / "meta mod", name="meta mod")
        mod_list.names.append("meta mod")
        for cb in mod_list.installed_callbacks:
            cb(mod)
        self.assertEqual(mod.version().text, "0.2.7")
        self.assertEqual(mod_list.names[-1], self.SDK)

    # -- snap-back enforcement ("locked like Overwrite") -----------------------

    SDK = "SDK Mod Settings"

    def _set_up_list(self, names, active=True):
        mod_list = self.org.modList()
        mod_list.names[:] = names
        if active:
            mod_list.active.add(self.SDK)
        mod_list.calls.clear()
        mod_list.hook_runs = 0
        return mod_list

    def test_deactivating_the_mod_is_undone(self):
        mod_list = self._set_up_list(["alpha", self.SDK])
        mod_list.user_set_active(self.SDK, False)
        self.assertIn(self.SDK, mod_list.active)
        self.assertEqual(mod_list.calls, [("setActive", self.SDK, True)])
        (level, message), = fakes.LOG                        # exactly one entry for one action
        self.assertEqual(level, "warning")
        self.assertIn("'SDK Mod Settings' cannot be deactivated", message)
        self.assertIn("sdk_mods/settings/", message)
        self.assertIn("re-enabled", message)

    def test_deactivating_another_mod_is_left_alone(self):
        mod_list = self._set_up_list(["alpha", self.SDK])
        mod_list.active.add("alpha")
        mod_list.user_set_active("alpha", False)
        self.assertEqual(mod_list.calls, [])
        self.assertNotIn("alpha", mod_list.active)
        self.assertEqual(fakes.LOG, [])

    def test_dragging_the_mod_up_snaps_it_back_to_last(self):
        mod_list = self._set_up_list(["alpha", "beta", self.SDK])
        mod_list.user_move(self.SDK, 0)
        self.assertEqual(mod_list.names, ["alpha", "beta", self.SDK])
        (level, message), = fakes.LOG
        self.assertEqual(level, "warning")
        self.assertIn("'SDK Mod Settings' cannot be moved", message)
        self.assertIn("directly before Overwrite", message)

    def test_dragging_another_mod_past_it_snaps_it_back_to_last(self):
        mod_list = self._set_up_list(["alpha", "beta", self.SDK])
        mod_list.user_move("alpha", 99)                      # dropped below everything
        self.assertEqual(mod_list.names[-1], self.SDK)
        self.assertEqual(sorted(mod_list.names), sorted(["alpha", "beta", self.SDK]))
        (_, message), = fakes.LOG
        self.assertIn("'alpha' cannot be placed below 'SDK Mod Settings'", message)

    def test_deactivating_and_moving_together_logs_each_refusal_once(self):
        mod_list = self._set_up_list(["alpha", "beta", self.SDK])
        mod_list.user_move(self.SDK, 0)
        mod_list.user_set_active(self.SDK, False)
        messages = [m for _, m in fakes.LOG]
        self.assertEqual(len(messages), 2)
        self.assertIn("cannot be moved", messages[0])
        self.assertIn("cannot be deactivated", messages[1])

    def test_startup_corrections_are_silent(self):
        names = self._set_up_list([self.SDK, "alpha"]).names
        self.org.about_to_run_callbacks[0]("Borderlands 4")  # the plugin's own pin, not a user action
        self.assertEqual(names[-1], self.SDK)
        self.assertEqual(fakes.LOG, [])

    def test_log_reaches_mo2_through_qt(self):
        # The message must go through qWarning (MO2's log pane), not just stderr.
        mod_list = self._set_up_list(["alpha", self.SDK])
        mod_list.user_set_active(self.SDK, False)
        self.assertEqual([lvl for lvl, _ in fakes.LOG], ["warning"])

    def test_corrections_do_not_loop(self):
        mod_list = self._set_up_list(["alpha", "beta", self.SDK])
        mod_list.user_move(self.SDK, 0)
        mod_list.user_set_active(self.SDK, False)
        self.assertLess(mod_list.hook_runs, 12)              # a handful of hook calls, not a storm
        self.assertFalse(self.game._enforcing)

    def test_no_churn_when_already_last_even_if_overwrite_is_listed(self):
        mod_list = self._set_up_list(["alpha", self.SDK, "overwrite"])
        mod_list.user_move("alpha", 0)                       # harmless reorder
        self.assertEqual(mod_list.calls, [])
        self.assertEqual(fakes.LOG, [])

    def test_hooks_do_nothing_while_the_mod_does_not_exist(self):
        mod_list = self._set_up_list(["alpha", "beta"], active=False)
        mod_list.user_move("alpha", 5)
        self.assertEqual(mod_list.calls, [])
        self.assertEqual(self.org.created, [])

    def test_hooks_do_nothing_for_another_managed_game(self):
        mod_list = self._set_up_list(["alpha", self.SDK])
        self.org.managed = BorderlandsGame(BORDERLANDS_3)
        mod_list.user_set_active(self.SDK, False)
        mod_list.user_move(self.SDK, 0)
        self.assertEqual(mod_list.calls, [])
        self.assertEqual(fakes.LOG, [])

    def test_a_failing_correction_is_reported_not_raised(self):
        mod_list = self._set_up_list(["alpha", self.SDK])
        mod_list.setActive = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        mod_list.user_set_active(self.SDK, False)             # must not raise
        self.assertFalse(self.game._enforcing)               # guard is released after a failure

    # -- deletions -----------------------------------------------------------

    def test_game_exit_marks_deleted_files(self):
        self.game.mappings()                                 # launch: seeds + records manifest
        (self.target / "a.json").unlink()                    # the game deleted its settings file
        self.org.finished_callbacks[0]("Borderlands 4", 0)   # game exits
        self.assertTrue((self.target / "a.json.deleted").is_file())
        self.game.mappings()                                 # next launch
        self.assertFalse((self.target / "a.json").exists())  # not resurrected from the real folder

    def test_finished_run_without_a_mod_folder_is_a_noop(self):
        self.org.finished_callbacks[0]("x", 0)               # must not raise


class SupportModPin(unittest.TestCase):
    """'SDK Mod Support': pinned at the lowest priority, next to 'SDK Mod Settings' at the highest."""

    SUPPORT, SETTINGS = "SDK Mod Support", "SDK Mod Settings"

    def setUp(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.game_dir, self.mods, self.instance = root / "game", root / "mods", root / "instance"
        for folder in (self.game_dir, self.mods, self.instance):
            folder.mkdir()
        fakes.LOG.clear()
        self.game = BorderlandsGame(BORDERLANDS_4)
        self.org = fakes.FakeOrganizer()
        self.org.mods_path, self.org.base_path = str(self.mods), str(self.instance)
        self.org.settings[("Borderlands Tools", "sdk_support")] = True   # off by default, see SdkSwitchOff
        self.game.init(self.org)
        self.game.setGamePath(self.game_dir)
        self.org.managed = self.game
        self.mod_list = self.org.modList()
        self.mod_list.names[:] = ["alpha", "beta"]
        self.mod_list.active.update(["alpha", "beta"])

    def start(self):
        self.org.ui_callbacks[0](None)

    def warnings(self):
        return [m for lvl, m in fakes.LOG if lvl == "warning"]

    def test_the_folder_is_not_made_before_mo2_creates_the_mod(self):
        """MO2's createMod asks Merge / Replace / Rename (and sets the old folder aside) if the folder exists."""
        seen = []
        real = self.org.createMod

        def create(guessed):
            seen.append((self.mods / guessed.value).exists())
            return real(guessed)

        self.org.createMod = create
        self.start()
        self.assertEqual(seen, [False, False])
        self.assertTrue((self.mods / self.SUPPORT).is_dir())                    # made afterwards for the plugin to fill

    def test_a_folder_on_disk_that_mo2_has_not_listed_is_adopted_not_recreated(self):
        for name in (self.SUPPORT, self.SETTINGS):
            (self.mods / name).mkdir()
        self.org.on_disk_after_refresh = [self.SUPPORT, self.SETTINGS]
        self.start()
        self.assertEqual(self.org.created, [])                                  # no "Mod Exists" prompt, no _backup folder
        self.assertEqual(self.org.refreshed, [False])                           # one refresh lists them all
        self.assertTrue(self.mod_list.state(self.SUPPORT) & fakes.sys.modules["mobase"].ModState.ACTIVE)
        self.assertEqual(self.mod_list.names[0], self.SUPPORT)
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()), sorted([self.SUPPORT, self.SETTINGS]))

    def test_a_folder_that_mo2_still_does_not_list_is_reported_once_not_prompted_about(self):
        (self.mods / self.SUPPORT).mkdir()
        self.start()
        self.org.profile_callbacks[0](None, None)
        self.assertEqual(self.org.created, [self.SETTINGS])
        self.assertEqual(len([m for m in self.warnings() if self.SUPPORT in m and "does not list it" in m]), 2)

    def test_repeated_events_do_not_pile_up_refreshes_while_one_is_pending(self):
        (self.mods / self.SUPPORT).mkdir()
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        self.start()
        self.org.profile_callbacks[0](None, None)
        self.assertEqual(len(fakes.QTimer.queue), 1)
        self.assertEqual(self.org.refreshed, [])                                # not from inside the callback
        fakes.QTimer.run_queued()
        self.assertEqual(self.org.refreshed, [False])

    def test_both_pinned_mods_are_created_enabled_and_placed_at_their_ends(self):
        self.start()
        self.assertEqual(self.org.created, [self.SETTINGS, self.SUPPORT])
        self.assertEqual(self.mod_list.names, [self.SUPPORT, "alpha", "beta", self.SETTINGS])
        self.assertTrue(self.mod_list.state(self.SUPPORT) & fakes.sys.modules["mobase"].ModState.ACTIVE)
        self.assertTrue((self.mods / self.SUPPORT).is_dir())                     # its folder exists for the plugin to fill

    def test_an_existing_support_mod_is_not_recreated_and_is_moved_to_the_start(self):
        self.mod_list.names[:] = ["alpha", self.SUPPORT, "beta"]
        self.mod_list.active.add(self.SUPPORT)
        self.start()
        self.assertNotIn(self.SUPPORT, self.org.created)
        self.assertEqual(self.mod_list.names[0], self.SUPPORT)

    def test_another_mod_cannot_be_placed_above_it(self):
        self.start()
        fakes.LOG.clear()
        self.mod_list.user_move("alpha", 0)
        self.assertEqual(self.mod_list.names[0], self.SUPPORT)
        (warning,) = self.warnings()
        self.assertIn("'alpha' cannot be placed above 'SDK Mod Support'", warning)
        self.assertIn("first, at the lowest priority", warning)
        self.assertIn("every other mod can override its files", warning)

    def test_it_cannot_be_moved(self):
        self.start()
        fakes.LOG.clear()
        self.mod_list.user_move(self.SUPPORT, 2)
        self.assertEqual(self.mod_list.names[0], self.SUPPORT)
        (warning,) = self.warnings()
        self.assertIn("'SDK Mod Support' cannot be moved", warning)

    def test_it_cannot_be_deactivated(self):
        self.start()
        fakes.LOG.clear()
        self.mod_list.user_set_active(self.SUPPORT, False)
        self.assertTrue(self.mod_list.state(self.SUPPORT))
        (warning,) = self.warnings()
        self.assertIn("'SDK Mod Support' cannot be deactivated. It holds the SDK's files", warning)

    def test_the_two_pinned_mods_do_not_fight(self):
        self.start()
        for name, where in (("alpha", 0), ("beta", 99), (self.SETTINGS, 0), (self.SUPPORT, 99), ("alpha", 0)):
            self.mod_list.user_move(name, where)
            self.assertEqual((self.mod_list.names[0], self.mod_list.names[-1]), (self.SUPPORT, self.SETTINGS), (name, where))
        self.assertEqual(sorted(self.mod_list.names), sorted(["alpha", "beta", self.SUPPORT, self.SETTINGS]))

    def test_a_new_install_is_kept_clear_of_both_ends(self):
        self.start()
        fakes.LOG.clear()
        self.mod_list.names.append("new mod")                                    # MO2 adds new mods at the end
        self.org.installed_callbacks[0](fakes.FakeMod(self.mods, name="new mod"))
        self.assertEqual((self.mod_list.names[0], self.mod_list.names[-1]), (self.SUPPORT, self.SETTINGS))
        self.assertIn("'SDK Mod Settings' was kept last, directly before Overwrite, after installing 'new mod'",
                      "\n".join(m for _, m in fakes.LOG))

    def test_a_game_without_an_sdk_has_no_support_mod(self):
        org = fakes.FakeOrganizer()
        org.mods_path, org.base_path = str(self.mods), str(self.instance)
        game = BorderlandsGame(dataclasses.replace(BORDERLANDS_4, sdk=None))
        game.init(org)
        game.setGamePath(self.game_dir)
        org.managed = game
        org.ui_callbacks[0](None)
        self.assertEqual(org.created, [self.SETTINGS])

    def test_the_support_mod_is_set_up_when_the_profile_changes(self):
        self.org.profile_callbacks[0]("old", "new")
        self.assertIn(self.SUPPORT, self.org.created)


class Checker(unittest.TestCase):
    def test_check_results_map_to_mobase_constants(self):
        checker = BorderlandsModDataChecker(BORDERLANDS_4, lambda: "")
        self.assertEqual(checker.dataLooksValid(FakeTree(FakeEntry("OakGame", True))), "VALID")
        self.assertEqual(checker.dataLooksValid(FakeTree(FakeEntry("x.pak"))), "FIXABLE")
        self.assertEqual(checker.dataLooksValid(FakeTree(FakeEntry("nonsense", True))), "INVALID")

    def test_fix_moves_loose_files_using_setting(self):
        tree = FakeTree(FakeEntry("a_P.pak"), FakeEntry("a_P.utoc"), FakeEntry("m.sdkmod"),
                        FakeEntry("OakGame", True), FakeEntry("readme.txt"))
        BorderlandsModDataChecker(BORDERLANDS_4, lambda: "").fix(tree)
        self.assertEqual(tree.moves, [("a_P.pak", "OakGame/Content/Paks/"),
                                      ("a_P.utoc", "OakGame/Content/Paks/"),
                                      ("m.sdkmod", "sdk_mods/")])

    def test_fix_honours_pak_subdir(self):
        tree = FakeTree(FakeEntry("x.pak"))
        BorderlandsModDataChecker(BORDERLANDS_3, lambda: "~mods").fix(tree)
        self.assertEqual(tree.moves, [("x.pak", "OakGame/Content/Paks/~mods/")])


class SdkSwitchOff(unittest.TestCase):
    """With the tools plugin's ``sdk_support`` setting turned off the SDK mod is not pinned (on is the default)."""

    def test_the_support_mod_is_pinned_by_default_and_not_when_the_setting_is_off(self):
        game = BorderlandsGame(BORDERLANDS_4)
        org = fakes.FakeOrganizer()
        game.init(org)
        self.assertIn("SDK Mod Support", [pin.name for pin in game._pins()])      # setting never written: the default
        org.settings[("Borderlands Tools", "sdk_support")] = "false"
        names = [pin.name for pin in game._pins()]
        self.assertNotIn("SDK Mod Support", names)
        self.assertIn("SDK Mod Settings", names)                                  # the redirect mod never depends on it
        org.settings[("Borderlands Tools", "sdk_support")] = True
        self.assertIn("SDK Mod Support", [pin.name for pin in game._pins()])


if __name__ == "__main__":
    unittest.main()
