"""The Tools-menu plugin, driven end to end with scripted dialogs and stubbed lookups."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import fakes

fakes.install()

from borderlands import tools, tweaks, virustotal  # noqa: E402
from borderlands.game import BorderlandsGame  # noqa: E402
from borderlands.nexus import Verdict  # noqa: E402
from borderlands.spec import ALL_SPECS, BORDERLANDS_2, BORDERLANDS_4  # noqa: E402
from borderlands.toolsmenu import DISMISS_SETTING, KEY_SETTING, BorderlandsTools  # noqa: E402
from borderlands.virustotal import Result  # noqa: E402

GAME_DIR = "C:/Apps/Steam/steamapps/common/Borderlands 4"
LINKED_META = "[General]\ngameName=borderlands4\nmodid=397\nrepository=Nexus\n"


class StubNexus:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, []

    def check(self, sha256, game_id, mod_id):
        self.calls.append((sha256, game_id, mod_id))
        return self.verdict


class StubVT:
    def __init__(self, key, result=Result("clean")):
        self.key, self.result, self.calls = key, result, []

    def lookup(self, sha256):
        self.calls.append(sha256)
        return self.result


class Base(unittest.TestCase):
    def setUp(self):
        fakes.LOG.clear()
        fakes.reset_ui()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.instance = self.root / "instance"
        self.mods = self.root / "mods"

        self.org = fakes.FakeOrganizer()
        self.org.base_path = str(self.instance)
        self.game = BorderlandsGame(BORDERLANDS_4)
        self.game.setGamePath(GAME_DIR)
        self.game.init(self.org)
        self.org.managed = self.game

        self.plugin = BorderlandsTools()
        self.plugin.init(self.org)
        self.plugin.setParentWidget(None)
        self.nexus = StubNexus(Verdict("clean"))
        self.plugin._nexus = self.nexus
        self.vt_clients = []

        def make_vt(key):
            client = StubVT(key, self.vt_result)
            self.vt_clients.append(client)
            return client

        self.vt_result = Result("clean")
        self.plugin._make_vt = make_vt

    def add_mod(self, name, files, meta=None, active=True):
        folder = self.mods / name
        for rel, text in files.items():
            path = folder / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if meta is not None:
            (folder / "meta.ini").write_text(meta)
        modlist = self.org.modList()
        modlist.names.append(name)
        modlist.paths[name] = str(folder)
        if active:
            modlist.active.add(name)
        return folder

    def dialogs(self, kind):
        return [d for d in fakes.DIALOGS if d[0] == kind]

    def started(self):
        return self.org.started

    def pick(self, label_part):
        items = None

        def chooser(parent, title, label, labels, current=0, editable=False):
            nonlocal items
            items = list(labels)
            fakes.DIALOGS.append(("choose", title, items))
            return next(l for l in labels if label_part in l), True

        return mock.patch("borderlands.toolsmenu.QInputDialog.getItem", chooser)


class RunningATool(Base):
    def setUp(self):
        super().setUp()
        self.add_mod("PhaseLoot", {"Tools/PhaseLoot/PhaseLoot.exe": "exe-bytes", "Tools/PhaseLoot/readme.txt": "x"},
                     meta=LINKED_META)
        self.sha = tools.sha256_of(self.mods / "PhaseLoot/Tools/PhaseLoot/PhaseLoot.exe")

    def test_a_clean_nexus_program_starts_through_the_game_folder_without_a_prompt(self):
        self.plugin.display()
        (exe, args, cwd), = self.started()
        real = self.mods / "PhaseLoot/Tools/PhaseLoot/PhaseLoot.exe"
        self.assertEqual(exe, str(real))   # MO2 cannot launch a file that only exists in the virtual folder
        self.assertEqual((args, cwd), ([], str(real.parent)))
        self.assertEqual(self.nexus.calls, [(self.sha, 8148, 397)])
        self.assertEqual(fakes.CURSOR, ["set", "restore"])
        self.assertEqual([d[0] for d in fakes.DIALOGS], ["choose"])
        self.assertTrue(any("started Tools/PhaseLoot/PhaseLoot.exe" in m for _, m in fakes.LOG))

    def test_the_log_shows_the_rule_chain_and_each_ruling(self):
        self.plugin.display()
        lines = [m for lvl, m in fakes.LOG if lvl == "info"]
        text = "\n".join(lines)
        self.assertIn("checking Tools/PhaseLoot/PhaseLoot.exe from 'PhaseLoot' (SHA-256 " + self.sha[:16], text)
        self.assertIn("source: Nexus Mods, mod 397", text)
        self.assertIn("Nexus Mods ruling: clean", text)
        self.assertIn("VirusTotal: not consulted (the program is from Nexus Mods)", text)
        self.assertIn("approved by you before: no", text)
        self.assertIn("decision: run (Nexus Mods verified this exact file)", text)
        self.assertLess(text.index("source:"), text.index("ruling:"))
        self.assertLess(text.index("decision:"), text.index("started Tools/"))

    def test_the_log_shows_a_declined_question_and_a_remembered_approval(self):
        self.nexus.verdict = Verdict("mismatch", "x")
        fakes.UI["confirm"] = "No"
        self.plugin.display()
        self.assertIn("you answered: no", "\n".join(m for _, m in fakes.LOG))
        fakes.LOG.clear()
        fakes.UI["confirm"] = "Yes"
        self.plugin.display()
        text = "\n".join(m for _, m in fakes.LOG)
        self.assertIn("decision: confirm (hash differs from every Nexus Mods file, override allowed)", text)
        self.assertIn("remembered this exact file as approved", text)
        fakes.LOG.clear()
        self.plugin.display()
        self.assertIn("decision: run (you approved this exact file before)", "\n".join(m for _, m in fakes.LOG))

    def test_the_menu_lists_each_program_with_its_mod(self):
        self.plugin.display()
        (_, _, items), = self.dialogs("choose")
        self.assertEqual(items, ["PhaseLoot/PhaseLoot.exe    (PhaseLoot)"])

    def test_a_refusal_has_no_override(self):
        self.nexus.verdict = Verdict("refuse", "Nexus Mods has quarantined this file for manual review.")
        self.plugin.display()
        self.assertEqual(self.started(), [])
        (_, _, text), = self.dialogs("critical")
        self.assertIn("quarantined", text)
        self.assertEqual(self.dialogs("warning"), [])
        self.assertTrue(any("refused to run" in m for _, m in fakes.LOG))

    def test_a_scan_in_progress_asks_and_no_means_no(self):
        self.nexus.verdict = Verdict("in_progress", "Nexus Mods has not finished scanning this file yet (status QUEUED).")
        fakes.UI["confirm"] = "No"
        self.plugin.display()
        self.assertEqual(self.started(), [])
        self.assertEqual(len(self.dialogs("warning")), 1)

    def test_a_scan_in_progress_asks_every_time(self):
        self.nexus.verdict = Verdict("in_progress", "still scanning")
        self.plugin.display()
        self.plugin.display()
        self.assertEqual(len(self.dialogs("warning")), 2)
        self.assertEqual(len(self.started()), 2)

    def test_a_hash_mismatch_asks_once_then_remembers(self):
        self.nexus.verdict = Verdict("mismatch", "x")
        self.plugin.display()
        (_, _, text), = self.dialogs("warning")
        self.assertIn("does not match anything Nexus Mods published", text)
        self.assertEqual(len(self.started()), 1)
        store = json.loads((self.instance / "plugin_state/borderlands/approved_tools.json").read_text())
        self.assertIn(self.sha, store)

        fakes.DIALOGS.clear()
        self.plugin.display()
        self.assertEqual(self.dialogs("warning"), [])          # remembered
        self.assertEqual(len(self.started()), 2)

    def test_a_changed_file_is_asked_about_again(self):
        self.nexus.verdict = Verdict("mismatch", "x")
        self.plugin.display()
        (self.mods / "PhaseLoot/Tools/PhaseLoot/PhaseLoot.exe").write_text("patched again")
        fakes.DIALOGS.clear()
        self.plugin.display()
        self.assertEqual(len(self.dialogs("warning")), 1)

    def test_declining_a_mismatch_remembers_nothing(self):
        self.nexus.verdict = Verdict("mismatch", "x")
        fakes.UI["confirm"] = "No"
        self.plugin.display()
        self.assertFalse((self.instance / "plugin_state/borderlands/approved_tools.json").exists())

    def test_a_remembered_file_is_still_refused_when_nexus_now_refuses_it(self):
        self.nexus.verdict = Verdict("mismatch", "x")
        self.plugin.display()
        self.nexus.verdict = Verdict("refuse", "The mod's page no longer exists on Nexus Mods.")
        self.org.started.clear()
        self.plugin.display()
        self.assertEqual(self.started(), [])

    def test_virustotal_is_not_consulted_for_nexus_installs(self):
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "k"
        self.plugin.display()
        self.assertEqual(self.vt_clients, [])


class NotFromNexus(Base):
    def setUp(self):
        super().setUp()
        self.add_mod("Loose Tool", {"tools/App/app.exe": "bytes"})  # no meta.ini: not from Nexus
        self.sha = tools.sha256_of(self.mods / "Loose Tool/tools/App/app.exe")

    def test_the_log_says_virustotal_was_not_consulted_without_a_key(self):
        self.plugin.display()
        text = "\n".join(m for _, m in fakes.LOG)
        self.assertIn("source: not from Nexus Mods", text)
        self.assertIn("VirusTotal: not consulted (no API key set)", text)
        self.assertIn("decision: confirm (not verifiable anywhere, override allowed)", text)

    def test_the_log_lists_virustotal_labels(self):
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "k"
        self.vt_result = Result("warn", 1, (("Elastic", "malicious (high confidence)"),), "1 engine(s) flag it with generic labels")
        self.plugin.display()
        self.assertIn("VirusTotal ruling: warn - 1 engine(s) flag it with generic labels [Elastic: malicious (high confidence)]",
                      "\n".join(m for _, m in fakes.LOG))

    def test_without_a_key_it_asks_and_says_why(self):
        self.plugin.display()
        (_, _, text), = self.dialogs("warning")
        self.assertIn("did not come from Nexus Mods", text)
        self.assertIn("No VirusTotal API key", text)
        self.assertEqual(self.nexus.calls, [])
        self.assertEqual(len(self.started()), 1)

    def test_a_mod_linked_to_another_game_counts_as_not_from_nexus(self):
        folder = self.mods / "Loose Tool"
        (folder / "meta.ini").write_text("[General]\ngameName=borderlands2\nmodid=5\nrepository=Nexus\n")
        self.plugin.display()
        self.assertEqual(self.nexus.calls, [])

    def test_with_a_key_a_clean_lookup_runs_silently(self):
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "the-key"
        self.plugin.display()
        self.assertEqual(self.dialogs("warning"), [])
        self.assertEqual(len(self.started()), 1)
        (client,) = self.vt_clients
        self.assertEqual((client.key, client.calls), ("the-key", [self.sha]))

    def test_a_virustotal_refusal_blocks(self):
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "k"
        self.vt_result = Result("refuse", 12, (("Microsoft", "Trojan:Win32/Emotet.A"),), "Microsoft names a malware family: Trojan:Win32/Emotet.A")
        self.plugin.display()
        self.assertEqual(self.started(), [])
        (_, _, text), = self.dialogs("critical")
        self.assertIn("Emotet", text)

    def test_generic_detections_warn_with_labels(self):
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "k"
        self.vt_result = Result("warn", 2, (("Elastic", "malicious (high confidence)"), ("APEX", "Malicious")), "2 engine(s) flag it with generic labels")
        self.plugin.display()
        (_, _, text), = self.dialogs("warning")
        self.assertIn("Elastic: malicious (high confidence)", text)

    def test_the_vt_client_is_reused_and_follows_a_changed_key(self):
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "one"
        self.plugin.display()
        self.plugin.display()
        self.assertEqual(len(self.vt_clients), 1)
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "two"
        self.plugin.display()
        self.assertEqual([c.key for c in self.vt_clients], ["one", "two"])


class MenuEdges(Base):
    def test_no_managed_borderlands_game(self):
        self.org.managed = None
        self.plugin.display()
        self.assertEqual(len(self.dialogs("information")), 1)

    def test_a_game_that_is_not_one_of_ours(self):
        other = mock.Mock()
        other.name.return_value = "Skyrim"
        self.org.managed = other
        self.plugin.display()
        self.assertEqual(len(self.dialogs("information")), 1)

    def test_no_tools_installed(self):
        self.add_mod("Plain", {"OakGame/x.pak": "x"})
        self.plugin.display()
        (_, _, text), = self.dialogs("information")
        self.assertIn("No programs found", text)

    def test_disabled_mods_are_not_offered(self):
        self.add_mod("Off", {"Tools/A/a.exe": "x"}, active=False)
        self.plugin.display()
        self.assertEqual(len(self.dialogs("information")), 1)

    def test_a_higher_priority_mod_shadows_the_same_path(self):
        self.add_mod("Low", {"Tools/A/a.exe": "old"})
        self.add_mod("High", {"Tools/A/a.exe": "new"})
        self.plugin.display()
        (_, _, items), = self.dialogs("choose")
        self.assertEqual(items, ["A/a.exe    (High)"])

    def test_cancelling_the_chooser_starts_nothing(self):
        self.add_mod("M", {"Tools/A/a.exe": "x"}, meta=LINKED_META)
        fakes.UI["item"] = None
        self.plugin.display()
        self.assertEqual(self.started(), [])
        self.assertEqual(self.nexus.calls, [])

    def test_an_unreadable_file_is_reported(self):
        self.add_mod("M", {"Tools/A/a.exe": "x"}, meta=LINKED_META)
        with mock.patch("borderlands.toolsmenu.tools.sha256_of", side_effect=OSError("locked")):
            self.plugin.display()
        self.assertEqual(self.started(), [])
        (_, _, text), = self.dialogs("critical")
        self.assertIn("locked", text)

    def test_display_never_raises(self):
        with mock.patch("borderlands.toolsmenu.tools.discover", side_effect=RuntimeError("boom")):
            self.plugin.display()
        (_, _, text), = self.dialogs("critical")
        self.assertIn("boom", text)

    def test_the_empty_menu_says_the_mod_must_be_enabled(self):
        self.add_mod("Off", {"Tools/A/a.exe": "x"}, active=False)
        self.plugin.display()
        (_, _, text), = self.dialogs("information")
        self.assertIn("make sure it is enabled", text)

    def test_the_cursor_is_restored_when_a_lookup_fails(self):
        self.add_mod("M", {"Tools/A/a.exe": "x"}, meta=LINKED_META)
        self.nexus.check = mock.Mock(side_effect=RuntimeError("net"))
        self.plugin.display()
        self.assertEqual(fakes.CURSOR, ["set", "restore"])
        self.assertEqual(self.started(), [])

    def test_identity_requirements_and_settings(self):
        self.assertEqual(self.plugin.name(), "Borderlands Tools")
        (kind, names), = self.plugin.requirements()
        self.assertEqual(names, frozenset(s.game_name for s in ALL_SPECS))
        self.assertEqual({s.key: s.default for s in self.plugin.settings()},
                         {KEY_SETTING: "", DISMISS_SETTING: False, "sdk_support": True, "sdk_update_check": True,
                          "tool_support_phaseloot": True})
        self.assertEqual(self.plugin.displayName(), "Borderlands: Choose a tool...")


BUILT = "Tools/PhaseLoot/Built PAK/zzz_Drop_Rarity_Custom_1100_P.pak"
BUILT_DIR = "Tools/PhaseLoot/Built PAK"
SETTING = ("Borderlands Tools", "tool_support_phaseloot")


class PhaseLootTweak(Base):
    def setUp(self):
        super().setUp()
        self.mod = self.add_mod("PhaseLoot", {"Tools/PhaseLoot/PhaseLoot.exe": "exe"}, meta=LINKED_META)
        self.built = self.mod / BUILT
        self.dest = self.mod / "OakGame/Content/Paks/zzz_Drop_Rarity_Custom_1100_P.pak"

    def run_tool(self, builds=True, content="pak-bytes"):
        def tool_runs():
            if builds:
                self.built.parent.mkdir(parents=True, exist_ok=True)
                self.built.write_text(content)

        self.org.while_running = tool_runs
        self.plugin.display()

    def log(self):
        return "\n".join(m for _, m in fakes.LOG)

    def test_the_built_pak_is_offered_and_moved_not_copied(self):
        self.run_tool()
        self.assertEqual(self.org.waited, [1])                                  # it waited for the program
        (_, _, text), = self.dialogs("question")
        self.assertIn("zzz_Drop_Rarity_Custom_1100_P.pak", text)
        self.assertIn("OakGame/Content/Paks", text)
        self.assertIn("moved, not copied", text)
        self.assertEqual(self.dest.read_text(), "pak-bytes")
        self.assertFalse(self.built.exists())                                   # gone from Built PAK
        self.assertTrue(self.built.parent.is_dir())                             # the folder stays for the next build
        self.assertEqual(len(self.org.data_changed), 1)
        self.assertIn("moved zzz_Drop_Rarity_Custom_1100_P.pak to OakGame/Content/Paks", self.log())

    def test_a_new_version_name_is_found_by_the_pattern(self):
        self.built = self.mod / BUILT_DIR / "zzz_Drop_Rarity_Custom_1200_P.pak"
        self.dest = self.mod / "OakGame/Content/Paks/zzz_Drop_Rarity_Custom_1200_P.pak"
        self.run_tool()
        self.assertEqual(self.dest.read_text(), "pak-bytes")
        self.assertFalse(self.built.exists())

    def test_several_matching_paks_are_listed_and_moved_together(self):
        def tool_runs():
            folder = self.mod / BUILT_DIR
            folder.mkdir(parents=True, exist_ok=True)
            for name in ("zzz_Drop_Rarity_Custom_1100_P.pak", "zzz_Drop_Rarity_Custom_1200_P.pak"):
                (folder / name).write_text(name)

        self.org.while_running = tool_runs
        self.plugin.display()
        (_, _, text), = self.dialogs("question")
        self.assertIn("1100_P.pak", text)
        self.assertIn("1200_P.pak", text)
        self.assertIn("Move them into", text)
        self.assertIn("They are moved, not copied", text)
        self.assertEqual(sorted(p.name for p in (self.mod / "OakGame/Content/Paks").iterdir()),
                         ["zzz_Drop_Rarity_Custom_1100_P.pak", "zzz_Drop_Rarity_Custom_1200_P.pak"])
        self.assertEqual(list((self.mod / BUILT_DIR).iterdir()), [])

    def test_other_files_in_built_pak_are_not_touched(self):
        def tool_runs():
            folder = self.mod / BUILT_DIR
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "zzz_Drop_Rarity_Custom_1100_P.pak").write_text("p")
            (folder / "notes.txt").write_text("n")
            (folder / "other_P.pak").write_text("o")

        self.org.while_running = tool_runs
        self.plugin.display()
        self.assertEqual(sorted(p.name for p in (self.mod / BUILT_DIR).iterdir()), ["notes.txt", "other_P.pak"])

    def test_it_waits_without_mo2s_refresh_that_can_hang(self):
        """MO2's default waits in a nested event loop for a refresh that may never be reported."""
        self.run_tool()
        self.assertEqual(self.org.wait_refresh, [False])
        self.assertIn("PhaseLoot has exited", self.log())

    def test_nothing_is_asked_when_no_pak_was_built(self):
        self.run_tool(builds=False)
        self.assertEqual(self.dialogs("question"), [])
        self.assertIn("no file to place", self.log())

    def test_declining_leaves_the_file_and_asks_again_next_run(self):
        fakes.UI["confirm"] = "No"
        self.run_tool()
        self.assertTrue(self.built.exists())
        self.assertFalse(self.dest.exists())
        self.assertIn("you chose not to move", self.log())
        fakes.DIALOGS.clear()
        fakes.UI["confirm"] = "Yes"
        self.run_tool(builds=False)                                              # still there from before
        self.assertEqual(len(self.dialogs("question")), 1)
        self.assertTrue(self.dest.exists())

    def test_a_rebuild_replaces_the_previous_pak_and_the_prompt_says_so(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_text("old build")
        self.run_tool(content="new build")
        (_, _, text), = self.dialogs("question")
        self.assertIn("This replaces zzz_Drop_Rarity_Custom_1100_P.pak, which is already there", text)
        self.assertEqual(self.dest.read_text(), "new build")
        self.assertIn("(replaced the previous one)", self.log())

    def test_the_setting_switches_it_off(self):
        self.org.settings[SETTING] = False
        self.run_tool()
        self.assertEqual(self.org.waited, [])                                    # does not even wait
        self.assertEqual(self.dialogs("question"), [])
        self.assertFalse(self.dest.exists())
        self.assertEqual(len(self.org.started), 1)                               # the tool still runs
        self.assertIn("tweak for PhaseLoot is switched off (tool_support_phaseloot)", self.log())

    def test_it_is_on_when_the_setting_was_never_stored(self):
        self.run_tool()
        self.assertIn("tweak for PhaseLoot is on (tool_support_phaseloot)", self.log())

    def test_stored_values_in_any_form_are_understood(self):
        for stored, expect_prompt in ((True, True), ("true", True), ("True", True), (1, True),
                                      (False, False), ("false", False), ("False", False), (0, False)):
            fakes.DIALOGS.clear()
            fakes.UI["confirm"] = "No"                                           # leave the file, just count prompts
            self.org.settings[SETTING] = stored
            self.run_tool()
            self.assertEqual(len(self.dialogs("question")) == 1, expect_prompt, stored)

    def test_it_needs_the_mod_id_not_just_the_exe_name(self):
        (self.mod / "meta.ini").write_text("[General]\ngameName=borderlands4\nmodid=398\nrepository=Nexus\n")
        self.run_tool()
        self.assertEqual(self.org.waited, [])
        self.assertEqual(self.dialogs("question"), [])

    def test_a_mod_that_is_not_from_nexus_gets_no_tweak(self):
        (self.mod / "meta.ini").unlink()
        self.run_tool()
        self.assertEqual(self.org.waited, [])

    def test_the_mod_id_of_another_game_does_not_match(self):
        (self.mod / "meta.ini").write_text("[General]\ngameName=borderlands3\nmodid=397\nrepository=Nexus\n")
        self.run_tool()
        self.assertEqual(self.org.waited, [])

    def test_the_executable_name_must_match_ignoring_case(self):
        other = self.add_mod("PhaseLoot Two", {"Tools/PL/Other.exe": "x"}, meta=LINKED_META)
        self.org.modList().active.discard("PhaseLoot")                          # only the other mod is enabled
        with self.pick("Other.exe"):
            self.plugin.display()
        self.assertEqual(self.org.waited, [])
        self.assertEqual(tweaks.match(tools.NexusLink(397, "borderlands4"), "PHASELOOT.EXE"), tweaks.PHASELOOT)

    def test_the_pak_subfolder_setting_is_honoured(self):
        self.org.settings[(BORDERLANDS_4.plugin_name, "pak_subdir")] = "~mods"
        self.run_tool()
        self.assertTrue((self.mod / "OakGame/Content/Paks/~mods/zzz_Drop_Rarity_Custom_1100_P.pak").exists())

    def test_a_failed_move_is_reported_and_never_raises(self):
        with mock.patch("borderlands.toolsmenu.tweaks.move", side_effect=PermissionError("in use")):
            self.run_tool()
        (_, _, text), = self.dialogs("critical")
        self.assertIn("in use", text)
        self.assertTrue(self.built.exists())

    def test_a_failure_while_waiting_is_reported_and_never_raises(self):
        self.org.waitForApplication = mock.Mock(side_effect=RuntimeError("lost the process"))
        self.plugin.display()
        (_, _, text), = self.dialogs("critical")
        self.assertIn("lost the process", text)

    def test_a_refused_launch_never_waits(self):
        self.nexus.verdict = Verdict("refuse", "quarantined")
        self.run_tool()
        self.assertEqual(self.org.waited, [])
        self.assertEqual(self.org.started, [])

    def test_a_failing_refresh_does_not_matter(self):
        self.org.modDataChanged = mock.Mock(side_effect=RuntimeError("no refresh"))
        self.run_tool()
        self.assertTrue(self.dest.exists())
        self.assertEqual(self.dialogs("critical"), [])


class ToolEntries(Base):
    """One "Borderlands: <name>" entry per program in MO2's Tools menu."""

    def setUp(self):
        super().setUp()
        self.menu = fakes.QMenu()
        # MO2's own slot is connected first: on every open it clears the menu and registers the
        # plugin tools (ours is the "choose a tool" entry).
        self.menu.aboutToShow.connect(self.mo2_rebuilds)
        action = fakes.QAction("Tools")
        action._menu = self.menu
        window = mock.Mock()
        window.findChild.side_effect = lambda cls, name: action if name == "actionTool" else None
        self.window = window
        self.org.ui_callbacks[-1](window)

    def mo2_rebuilds(self):
        self.menu.clear()
        self.menu.addAction(fakes.QAction("Borderlands: Choose a tool..."))

    def entries(self):
        """The tool entries (the SDK entries, which Borderlands 4 always has, are tested separately)."""
        return [a.text() for a in self.menu.actions()
                if not a.separator and not a.text().startswith("Borderlands SDK:")]

    def show(self):
        self.menu.show()
        return self.entries()

    def entry(self, text):
        return next(a for a in self.menu.actions() if a.text() == text)

    def test_the_window_is_searched_for_mo2s_tools_action(self):
        self.window.findChild.assert_any_call(fakes.QAction, "actionTool")

    def test_each_program_gets_an_entry_after_mo2s_own(self):
        self.add_mod("Plain", {"OakGame/x.pak": "x"})
        self.add_mod("PhaseLoot", {"Tools/PhaseLoot/PhaseLoot.exe": "x"})
        self.add_mod("Other", {"Tools/Thing/thing.exe": "x"})
        self.assertEqual(self.show(), ["Borderlands: Choose a tool...", "Borderlands: PhaseLoot", "Borderlands: thing"])
        self.assertTrue(self.menu.actions()[1].separator)                       # set apart from MO2's entries

    def test_with_no_tools_the_menu_is_left_alone(self):
        self.add_mod("Plain", {"OakGame/x.pak": "x"})
        self.assertEqual(self.show(), ["Borderlands: Choose a tool..."])

    def test_entries_follow_the_enabled_mods_and_never_pile_up(self):
        self.add_mod("PhaseLoot", {"Tools/PhaseLoot/PhaseLoot.exe": "x"})
        self.add_mod("Other", {"Tools/Thing/thing.exe": "x"})
        self.assertEqual(len(self.show()), 3)
        self.assertEqual(len(self.show()), 3)                                   # opening again adds no copies
        self.org.modList().active.discard("Other")                              # mod disabled
        self.assertEqual(self.show(), ["Borderlands: Choose a tool...", "Borderlands: PhaseLoot"])
        self.org.modList().names.remove("PhaseLoot")                            # mod removed
        self.assertEqual(self.show(), ["Borderlands: Choose a tool..."])

    def test_our_old_entries_are_removed_even_if_mo2_did_not_clear_the_menu(self):
        self.add_mod("PhaseLoot", {"Tools/PhaseLoot/PhaseLoot.exe": "x"})
        self.menu.aboutToShow._slots.remove(self.mo2_rebuilds)                  # nothing clears the menu
        self.menu.addAction(fakes.QAction("Borderlands: Choose a tool..."))
        self.show()
        self.assertEqual(self.show(), ["Borderlands: Choose a tool...", "Borderlands: PhaseLoot"])

    def test_entries_deleted_by_mo2s_rebuild_are_never_touched_again(self):
        """Real Qt: menu.clear() deletes the actions, and passing a deleted one to removeAction raises."""
        self.add_mod("PhaseLoot", {"Tools/PhaseLoot/PhaseLoot.exe": "x"})
        live = self.menu.actions

        def strict_remove(action):
            if action not in live():
                raise RuntimeError("wrapped C/C++ object of type QAction has been deleted")
            fakes.QMenu.removeAction(self.menu, action)

        self.menu.removeAction = strict_remove
        self.show()
        self.assertEqual(self.show(), ["Borderlands: Choose a tool...", "Borderlands: PhaseLoot"])
        self.assertEqual(self.show(), ["Borderlands: Choose a tool...", "Borderlands: PhaseLoot"])
        self.assertFalse([m for lvl, m in fakes.LOG if "could not list tools" in m])

    def test_same_named_programs_are_told_apart_by_their_mod(self):
        self.add_mod("Mod A", {"Tools/App/app.exe": "x"})
        self.add_mod("Mod B", {"Tools/App2/app.exe": "x"})
        self.assertEqual(self.show()[1:], ["Borderlands: app (Mod A)", "Borderlands: app (Mod B)"])

    def test_the_name_comes_from_the_executables_metadata(self):
        self.add_mod("Mod", {"Tools/App/app.exe": "x"})
        with mock.patch("borderlands.peinfo._read_version_strings",
                        return_value={"FileDescription": "Loot Editor", "ProductName": "PhaseLoot"}):
            self.assertEqual(self.show()[1:], ["Borderlands: Loot Editor"])

    def test_an_ampersand_in_a_name_is_not_a_menu_shortcut(self):
        self.add_mod("Mod", {"Tools/App/app.exe": "x"})
        reader = lambda path: {"FileDescription": "Tom & Jerry"}
        with mock.patch("borderlands.peinfo.display_name", lambda p: "Tom & Jerry"):
            self.assertEqual(self.show()[1:], ["Borderlands: Tom && Jerry"])

    def test_clicking_an_entry_runs_that_tool_through_the_checks(self):
        self.add_mod("PhaseLoot", {"Tools/PhaseLoot/PhaseLoot.exe": "exe"}, meta=LINKED_META)
        self.show()
        self.entry("Borderlands: PhaseLoot").triggered.emit(False)
        (exe, _, cwd), = self.started()
        self.assertTrue(exe.replace("\\", "/").endswith("mods/PhaseLoot/Tools/PhaseLoot/PhaseLoot.exe"))
        self.assertEqual(len(self.nexus.calls), 1)                              # it was checked first
        self.assertEqual(self.dialogs("choose"), [])                            # and no chooser dialog

    def test_a_refused_tool_is_refused_from_its_entry_too(self):
        self.add_mod("PhaseLoot", {"Tools/PhaseLoot/PhaseLoot.exe": "exe"}, meta=LINKED_META)
        self.nexus.verdict = Verdict("refuse", "quarantined")
        self.show()
        self.entry("Borderlands: PhaseLoot").triggered.emit(False)
        self.assertEqual(self.started(), [])
        self.assertEqual(len(self.dialogs("critical")), 1)

    def test_an_entry_runs_the_tool_it_was_made_for(self):
        self.add_mod("A", {"Tools/One/one.exe": "1"}, meta=LINKED_META)
        self.add_mod("B", {"Tools/Two/two.exe": "2"}, meta=LINKED_META)
        self.show()
        self.entry("Borderlands: two").triggered.emit(False)
        self.assertTrue(self.started()[0][0].replace("\\", "/").endswith("B/Tools/Two/two.exe"))

    def test_an_entry_clicked_after_the_game_went_away_does_nothing(self):
        self.add_mod("M", {"Tools/App/app.exe": "x"})
        self.show()
        self.org.managed = None
        self.entry("Borderlands: app").triggered.emit(False)
        self.assertEqual(self.started(), [])

    def test_a_tool_that_vanished_since_the_menu_opened_is_reported(self):
        folder = self.add_mod("M", {"Tools/App/app.exe": "x"}, meta=LINKED_META)
        self.show()
        (folder / "Tools/App/app.exe").unlink()
        self.entry("Borderlands: app").triggered.emit(False)
        self.assertEqual(self.started(), [])
        self.assertEqual(len(self.dialogs("critical")), 1)

    def test_a_failure_while_listing_never_breaks_the_menu(self):
        with mock.patch("borderlands.toolsmenu.tools.discover", side_effect=RuntimeError("boom")):
            self.assertEqual(self.show(), ["Borderlands: Choose a tool..."])
        self.assertTrue([m for lvl, m in fakes.LOG if "could not list tools" in m])

    def test_an_unexpected_failure_while_running_an_entry_is_reported(self):
        self.add_mod("M", {"Tools/App/app.exe": "x"}, meta=LINKED_META)
        self.show()
        with mock.patch("borderlands.toolsmenu.tools.sha256_of", side_effect=RuntimeError("odd")):
            self.entry("Borderlands: app").triggered.emit(False)
        self.assertEqual(self.started(), [])

    def test_the_chooser_entry_is_still_there_and_still_works(self):
        self.add_mod("M", {"Tools/App/app.exe": "x"}, meta=LINKED_META)
        self.assertEqual(self.plugin.displayName(), "Borderlands: Choose a tool...")
        self.plugin.display()
        self.assertEqual(len(self.dialogs("choose")), 1)

    def test_the_menu_is_only_connected_once(self):
        self.org.ui_callbacks[-1](self.window)                                  # e.g. a second startup callback
        self.assertEqual(len(self.menu.aboutToShow._slots), 2)                  # MO2's, and ours

    def test_without_a_borderlands_game_nothing_is_attached(self):
        org = fakes.FakeOrganizer()
        plugin = BorderlandsTools()
        plugin.init(org)
        menu = fakes.QMenu()
        action = fakes.QAction("Tools")
        action._menu = menu
        window = mock.Mock()
        window.findChild.side_effect = lambda cls, name: action
        org.ui_callbacks[-1](window)
        self.assertEqual(menu.aboutToShow._slots, [])

    def test_a_window_without_the_tools_menu_is_tolerated_with_a_hint(self):
        org = fakes.FakeOrganizer()
        game = BorderlandsGame(BORDERLANDS_4)
        game.init(org)
        org.managed = game
        plugin = BorderlandsTools()
        plugin.init(org)
        window = mock.Mock()
        window.findChild.return_value = None
        org.ui_callbacks[-1](window)
        self.assertTrue([m for lvl, m in fakes.LOG if "could not find MO2's Tools menu" in m])

    def test_a_window_that_cannot_be_searched_is_tolerated(self):
        org = fakes.FakeOrganizer()
        game = BorderlandsGame(BORDERLANDS_4)
        game.init(org)
        org.managed = game
        plugin = BorderlandsTools()
        plugin.init(org)
        org.ui_callbacks[-1](object())                                          # no findChild at all: silent


class KeyPrompt(Base):
    def fire(self):
        (callback,) = self.org.ui_callbacks[-1:]
        callback(object())

    def setUp(self):
        super().setUp()
        # the game plugin registers a UI callback only when it has redirects (BL4 does); ours is last
        self.ui = self.org.ui_callbacks[-1]

    def test_it_asks_at_startup_until_dismissed(self):
        self.ui(object())
        (_, _, text), = self.dialogs("prompt")
        self.assertIn(virustotal.SIGN_UP_URL, text)
        self.assertIn(virustotal.KEY_PAGE_URL, text)
        self.assertIn(KEY_SETTING, text)
        self.assertTrue(fakes.PROMPTS[0].rich)
        self.assertEqual(fakes.PROMPTS[0].texts, {"Ok": "Remind me next time", "Cancel": "Don't ask again"})
        self.assertNotIn(("Borderlands Tools", DISMISS_SETTING), self.org.settings)

        fakes.reset_ui()
        self.ui(object())                                          # next startup: asks again
        self.assertEqual(len(self.dialogs("prompt")), 1)

    def test_cancel_means_do_not_ask_again(self):
        fakes.UI["prompt"] = "cancel"
        self.ui(object())
        self.assertIs(self.org.settings[("Borderlands Tools", DISMISS_SETTING)], True)
        fakes.reset_ui()
        self.ui(object())
        self.assertEqual(self.dialogs("prompt"), [])

    def test_closing_the_window_is_not_a_dismissal(self):
        fakes.UI["prompt"] = "close"
        self.ui(object())
        self.assertNotIn(("Borderlands Tools", DISMISS_SETTING), self.org.settings)

    def test_a_key_in_settings_silences_it(self):
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "k"
        self.ui(object())
        self.assertEqual(self.dialogs("prompt"), [])

    def test_a_blank_key_does_not(self):
        self.org.settings[("Borderlands Tools", KEY_SETTING)] = "   "
        self.ui(object())
        self.assertEqual(len(self.dialogs("prompt")), 1)

    def test_a_stored_true_in_any_form_silences_it(self):
        for stored in (True, "true", "True", 1):
            fakes.reset_ui()
            self.org.settings[("Borderlands Tools", DISMISS_SETTING)] = stored
            self.ui(object())
            self.assertEqual(self.dialogs("prompt"), [], stored)

    def test_no_prompt_when_no_borderlands_game_is_managed(self):
        self.org.managed = None
        self.ui(object())
        self.assertEqual(self.dialogs("prompt"), [])

    def test_a_failure_never_breaks_startup(self):
        with mock.patch("borderlands.toolsmenu.QMessageBox", side_effect=RuntimeError("no gui")):
            self.ui(object())
        self.assertTrue(any("VirusTotal key prompt" in m for _, m in fakes.LOG))

    def test_it_asks_for_other_borderlands_games_too(self):
        org = fakes.FakeOrganizer()
        game = BorderlandsGame(BORDERLANDS_2)
        game.init(org)
        org.managed = game
        plugin = BorderlandsTools()
        plugin.init(org)
        org.ui_callbacks[-1](object())
        self.assertEqual(len(self.dialogs("prompt")), 1)


if __name__ == "__main__":
    unittest.main()
