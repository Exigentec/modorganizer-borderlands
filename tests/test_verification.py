"""Nexus lookup, VirusTotal classification, trust policy and tool discovery."""

import tempfile
import unittest
from pathlib import Path

import sys

from borderlands import netio, nexus, peinfo, tools, trust, tweaks, virustotal
from borderlands.nexus import NexusClient, Verdict
from borderlands.trust import Action, ApprovalStore, decide
from borderlands.virustotal import Result, VirusTotalClient

EXE = "5d816b042ce2ea4c399dbda0f0726a3584dd9c7a801523a787512dbe538fad61"
OLD_EXE = "11" * 32
OTHER = "22" * 32


def manifest(*hashes):
    return {"files": [{"file_path": f"PhaseLoot/f{i}.exe", "file_size": 1, "file_hashes": {"SHA256": h}}
                      for i, h in enumerate(hashes)]}


def graphql(files, status="published"):
    return {"data": {"mod": {"status": status}, "modFiles": files}}


def nfile(file_id, scan="VERIFIED", category="MAIN", uri=None, name="PhaseLoot", version="1.1"):
    return {"fileId": file_id, "name": name, "version": version, "category": category,
            "scannedV2": scan, "uri": uri or f"u{file_id}"}


class FakeNexus:
    """Routes GraphQL and manifest URLs to canned answers and counts the calls."""

    def __init__(self, gql, manifests, gql_status=200):
        self.gql, self.manifests, self.gql_status = gql, manifests, gql_status
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=8.0):
        self.calls.append(url)
        if url == nexus.GRAPHQL:
            if isinstance(self.gql, Exception):
                raise self.gql
            return self.gql_status, self.gql
        uri = url[len(nexus.MANIFESTS):]
        answer = self.manifests.get(uri)
        if isinstance(answer, Exception):
            raise answer
        return (200, answer) if answer is not None else (404, None)


def client(files, manifests, **kw):
    fake = FakeNexus(graphql(files, **kw), manifests)
    return NexusClient(fake), fake


class NexusLookup(unittest.TestCase):
    def test_exe_in_the_current_clean_file_is_clean(self):
        c, _ = client([nfile(2230)], {"u2230": manifest(EXE)})
        v = c.check(EXE, 8148, 397)
        self.assertEqual((v.kind, v.file_name, v.file_version), ("clean", "PhaseLoot", "1.1"))

    def test_hash_from_an_older_version_of_the_mod_is_accepted(self):
        c, _ = client([nfile(2227, category="ARCHIVED", version="1.0"), nfile(2230)],
                      {"u2227": manifest(OLD_EXE), "u2230": manifest(EXE)})
        self.assertEqual(c.check(OLD_EXE, 8148, 397).kind, "clean")

    def test_hash_comparison_ignores_case(self):
        c, _ = client([nfile(1)], {"u1": manifest(EXE.upper())})
        self.assertEqual(c.check(EXE, 8148, 397).kind, "clean")

    def test_unknown_hash_is_a_mismatch(self):
        c, _ = client([nfile(2230)], {"u2230": manifest(EXE)})
        v = c.check(OTHER, 8148, 397)
        self.assertEqual(v.kind, "mismatch")

    def test_a_mod_that_is_not_published_is_refused(self):
        for status in ("hidden", "removed", "wastebinned", "under_moderation", "not_published", ""):
            c, _ = client([nfile(1)], {"u1": manifest(EXE)}, status=status)
            v = c.check(EXE, 8148, 397)
            self.assertEqual(v.kind, "refuse", status)
            self.assertIn("not 'published'", v.reason)

    def test_a_deleted_mod_is_refused(self):
        gone = {"errors": [{"message": "Mod not found", "extensions": {"code": "NOT_FOUND"}}], "data": None}
        c = NexusClient(FakeNexus(gone, {}))
        v = c.check(EXE, 8148, 397)
        self.assertEqual(v.kind, "refuse")
        self.assertIn("no longer exists", v.reason)

    def test_a_file_that_was_removed_is_refused(self):
        c, _ = client([nfile(5, category="REMOVED")], {"u5": manifest(EXE)})
        v = c.check(EXE, 8148, 397)
        self.assertEqual(v.kind, "refuse")
        self.assertIn("removed", v.reason)

    def test_quarantined_is_refused_without_override(self):
        c, _ = client([nfile(5, scan="QUARANTINED")], {"u5": manifest(EXE)})
        v = c.check(EXE, 8148, 397)
        self.assertEqual(v.kind, "refuse")
        self.assertIn("quarantined", v.reason)

    def test_scan_still_running_is_in_progress(self):
        for scan in ("NOT_SCANNED", "QUEUED", "WAITING_REPORT", "PARTIAL"):
            c, _ = client([nfile(5, scan=scan)], {"u5": manifest(EXE)})
            self.assertEqual(c.check(EXE, 8148, 397).kind, "in_progress", scan)

    def test_trusted_statuses_are_clean(self):
        for scan in ("VERIFIED", "INTERNALLY_VERIFIED", "MANUALLY_VERIFIED"):
            c, _ = client([nfile(5, scan=scan)], {"u5": manifest(EXE)})
            self.assertEqual(c.check(EXE, 8148, 397).kind, "clean", scan)

    def test_any_other_scan_status_is_refused(self):
        for scan in ("FILE_NOT_FOUND", "MOD_DOES_NOT_EXIST", "REPORT_ERROR", "TOO_LARGE", "SOMETHING_NEW", ""):
            c, _ = client([nfile(5, scan=scan)], {"u5": manifest(EXE)})
            self.assertEqual(c.check(EXE, 8148, 397).kind, "refuse", scan)

    def test_the_most_favourable_matching_file_wins(self):
        c, _ = client([nfile(1, scan="QUARANTINED"), nfile(2, scan="VERIFIED")],
                      {"u1": manifest(EXE), "u2": manifest(EXE)})
        self.assertEqual(c.check(EXE, 8148, 397).kind, "clean")

    def test_a_live_file_beats_a_removed_one(self):
        c, _ = client([nfile(1, category="REMOVED"), nfile(2)], {"u1": manifest(EXE), "u2": manifest(EXE)})
        self.assertEqual(c.check(EXE, 8148, 397).kind, "clean")

    def test_unreachable_nexus_is_unavailable(self):
        c = NexusClient(FakeNexus(netio.NetworkError("offline"), {}))
        v = c.check(EXE, 8148, 397)
        self.assertEqual(v.kind, "unavailable")
        self.assertIn("offline", v.reason)

    def test_an_odd_answer_is_unavailable_not_trusted(self):
        c = NexusClient(FakeNexus({"data": None}, {}, gql_status=500))
        self.assertEqual(c.check(EXE, 8148, 397).kind, "unavailable")

    def test_unreadable_listings_without_a_match_are_unavailable_not_a_mismatch(self):
        c, _ = client([nfile(1), nfile(2)], {"u1": manifest(OTHER), "u2": netio.NetworkError("x")})
        self.assertEqual(c.check(EXE, 8148, 397).kind, "unavailable")

    def test_a_match_is_trusted_even_if_another_listing_failed(self):
        c, _ = client([nfile(1), nfile(2)], {"u1": manifest(EXE), "u2": None})
        self.assertEqual(c.check(EXE, 8148, 397).kind, "clean")

    def test_results_are_cached_per_mod(self):
        c, fake = client([nfile(2230)], {"u2230": manifest(EXE)})
        c.check(EXE, 8148, 397)
        c.check(OTHER, 8148, 397)
        self.assertEqual(fake.calls.count(nexus.GRAPHQL), 1)
        self.assertEqual(len(fake.calls), 2)  # one query, one manifest

    def test_only_ids_are_sent(self):
        c, fake = client([nfile(1)], {"u1": manifest(EXE)})
        c.check(EXE, 8148, 397)
        self.assertTrue(all(u.startswith(("https://api.nexusmods.com/", "https://mod-file-manifests.nexusmods.com/"))
                            for u in fake.calls))


def vt_payload(flags):
    """``flags``: {engine: label} of flagging engines; two quiet engines are added."""
    results = {e: {"category": "malicious", "result": l} for e, l in flags.items()}
    results.update({"Quiet1": {"category": "undetected", "result": None}, "Quiet2": {"category": "harmless", "result": None}})
    return {"data": {"attributes": {"last_analysis_results": results}}}


PHASELOOT_LABELS = {
    "APEX": "Malicious", "CrowdStrike": "win/malicious_confidence_60% (D)",
    "Elastic": "malicious (high confidence)", "Kingsoft": "malware.kb.c.937",
    "MaxSecure": "Trojan.Malware.300983.susgen", "McAfeeD": "Real Protect-LS!3336238278AB",
    "Trapmine": "malicious.moderate.ml.score",
}


class VirusTotalVerdicts(unittest.TestCase):
    def test_no_detections_is_clean(self):
        self.assertEqual(virustotal.classify(vt_payload({})).kind, "clean")

    def test_the_real_phaseloot_labels_only_warn(self):
        r = virustotal.classify(vt_payload(PHASELOOT_LABELS))
        self.assertEqual((r.kind, r.flagged), ("warn", 7))
        self.assertIn(("Elastic", "malicious (high confidence)"), r.labels)

    def test_ten_engines_refuse_even_with_generic_labels(self):
        flags = {f"Engine{i}": "Generic.Heur" for i in range(10)}
        r = virustotal.classify(vt_payload(flags))
        self.assertEqual((r.kind, r.flagged), ("refuse", 10))

    def test_nine_generic_engines_still_only_warn(self):
        flags = {f"Engine{i}": "Generic.Heur" for i in range(9)}
        self.assertEqual(virustotal.classify(vt_payload(flags)).kind, "warn")

    def test_a_named_family_from_a_major_engine_refuses_on_its_own(self):
        r = virustotal.classify(vt_payload({"Microsoft": "Trojan:Win32/Emotet.A"}))
        self.assertEqual(r.kind, "refuse")
        self.assertIn("Emotet", r.reason)

    def test_a_named_family_from_a_minor_engine_does_not(self):
        self.assertEqual(virustotal.classify(vt_payload({"SomeSmallAV": "Trojan:Win32/Emotet.A"})).kind, "warn")

    def test_generic_labels_from_major_engines_do_not_count_as_named(self):
        for label in ("Trojan:Win32/Wacatac.B!ml", "HEUR:Trojan.Win32.Generic", "Trojan.Win32.Agent.abcd"):
            self.assertEqual(virustotal.classify(vt_payload({"Microsoft": label})).kind, "warn", label)
        self.assertTrue(virustotal.is_named_family("ESET-NOD32", "A Variant Of Win32/AgentTesla.X"))

    def test_suspicious_counts_as_flagged(self):
        p = vt_payload({})
        p["data"]["attributes"]["last_analysis_results"]["X"] = {"category": "suspicious", "result": "odd"}
        self.assertEqual(virustotal.classify(p).flagged, 1)

    def test_garbage_is_an_error(self):
        self.assertEqual(virustotal.classify({"data": {}}).kind, "error")
        self.assertEqual(virustotal.classify(None).kind, "error")


class VirusTotalClientCalls(unittest.TestCase):
    def fetcher(self, status, payload=None, raises=None):
        calls = []

        def fetch(url, data=None, headers=None, timeout=8.0):
            calls.append((url, dict(headers or {})))
            if raises:
                raise raises
            return status, payload

        fetch.calls = calls
        return fetch

    def test_the_key_goes_only_in_the_header_to_virustotal(self):
        f = self.fetcher(200, vt_payload({}))
        VirusTotalClient("  secret-key \n", f).lookup(EXE.upper())
        (url, headers), = f.calls
        self.assertEqual(url, virustotal.URL + EXE)
        self.assertEqual(headers, {"x-apikey": "secret-key"})
        self.assertNotIn("secret", url)

    def test_unknown_file_is_unknown_and_cached(self):
        f = self.fetcher(404)
        c = VirusTotalClient("k", f)
        self.assertEqual(c.lookup(EXE).kind, "unknown")
        c.lookup(EXE)
        self.assertEqual(len(f.calls), 1)

    def test_bad_key_limit_and_server_errors_are_errors_and_not_cached(self):
        for status, word in ((401, "key"), (403, "key"), (429, "limit"), (500, "500")):
            f = self.fetcher(status)
            c = VirusTotalClient("k", f)
            r = c.lookup(EXE)
            self.assertEqual(r.kind, "error", status)
            self.assertIn(word, r.reason)
            c.lookup(EXE)
            self.assertEqual(len(f.calls), 2, status)

    def test_network_failure_is_an_error(self):
        r = VirusTotalClient("k", self.fetcher(0, raises=netio.NetworkError("down"))).lookup(EXE)
        self.assertEqual(r.kind, "error")

    def test_the_key_is_never_in_a_result(self):
        r = VirusTotalClient("secret-key", self.fetcher(401)).lookup(EXE)
        self.assertNotIn("secret-key", repr(r))


V = Verdict


class TrustPolicy(unittest.TestCase):
    def go(self, **kw):
        args = dict(label="PhaseLoot/PhaseLoot.exe", mod="PhaseLoot", sha256=EXE, linked=True,
                    approved=False, nexus=None, vt=None, vt_key_set=True)
        args.update(kw)
        return decide(**args)

    def test_nexus_clean_runs_without_a_prompt(self):
        self.assertEqual(self.go(nexus=V("clean")).action, Action.RUN)

    def test_nexus_refusals_cannot_be_overridden_even_when_approved(self):
        d = self.go(nexus=V("refuse", "Nexus Mods has quarantined this file for manual review."), approved=True)
        self.assertEqual(d.action, Action.REFUSE)
        self.assertIn("quarantined", d.text)

    def test_nexus_scan_in_progress_asks_each_time(self):
        d = self.go(nexus=V("in_progress", "Nexus Mods has not finished scanning this file yet (status QUEUED)."), approved=True)
        self.assertEqual(d.action, Action.CONFIRM)
        self.assertFalse(d.remember)

    def test_hash_mismatch_asks_with_the_explicit_warning(self):
        d = self.go(nexus=V("mismatch", "x"))
        self.assertEqual(d.action, Action.CONFIRM)
        self.assertTrue(d.remember)
        self.assertIn("does not match anything Nexus Mods published", d.text)
        self.assertIn("If you did not, do not run it", d.text)

    def test_an_approved_mismatch_runs(self):
        self.assertEqual(self.go(nexus=V("mismatch", "x"), approved=True).action, Action.RUN)

    def test_offline_asks_unless_approved(self):
        v = V("unavailable", "Could not reach Nexus Mods (offline).")
        self.assertEqual(self.go(nexus=v).action, Action.CONFIRM)
        self.assertEqual(self.go(nexus=v, approved=True).action, Action.RUN)

    def test_linked_mod_ignores_virustotal(self):
        bad = Result("refuse", 20, (("A", "x"),), "20 engines flag it")
        self.assertEqual(self.go(nexus=V("clean"), vt=bad).action, Action.RUN)

    def test_not_from_nexus_virustotal_refuse_wins_even_when_approved(self):
        bad = Result("refuse", 12, (("A", "x"),), "12 engines flag it")
        d = self.go(linked=False, vt=bad, approved=True)
        self.assertEqual(d.action, Action.REFUSE)
        self.assertIn("12 engines", d.text)

    def test_not_from_nexus_clean_runs(self):
        self.assertEqual(self.go(linked=False, vt=Result("clean")).action, Action.RUN)

    def test_not_from_nexus_warn_lists_the_labels_and_asks(self):
        warn = Result("warn", 7, tuple(PHASELOOT_LABELS.items()), "7 engine(s) flag it with generic labels")
        d = self.go(linked=False, vt=warn)
        self.assertEqual(d.action, Action.CONFIRM)
        self.assertTrue(d.remember)
        self.assertIn("Elastic: malicious (high confidence)", d.text)

    def test_approved_not_from_nexus_and_not_refused_runs(self):
        warn = Result("warn", 3, (("A", "x"),), "3 engine(s)")
        self.assertEqual(self.go(linked=False, vt=warn, approved=True).action, Action.RUN)

    def test_unknown_error_or_no_key_ask_once(self):
        for vt, key, word in ((Result("unknown", reason="VirusTotal has never seen this file."), True, "never seen"),
                              (Result("error", reason="VirusTotal rejected the API key."), True, "rejected"),
                              (None, False, "No VirusTotal API key")):
            d = self.go(linked=False, vt=vt, vt_key_set=key)
            self.assertEqual(d.action, Action.CONFIRM)
            self.assertTrue(d.remember)
            self.assertIn(word, d.text)

    def test_long_label_lists_are_trimmed(self):
        labels = tuple((f"E{i:02d}", "generic") for i in range(30))
        d = self.go(linked=False, vt=Result("warn", 30, labels, "30"))
        self.assertIn("and 18 more", d.text)


class Approvals(unittest.TestCase):
    def test_round_trip_and_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "approved.json"
            store = ApprovalStore(path)
            self.assertFalse(store.is_approved(EXE))
            store.approve(EXE, "PhaseLoot", "Tools/PhaseLoot/PhaseLoot.exe")
            self.assertTrue(ApprovalStore(path).is_approved(EXE.upper()))
            self.assertFalse(ApprovalStore(path).is_approved(OTHER))

    def test_a_corrupt_file_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved.json"
            path.write_text("{not json")
            self.assertFalse(ApprovalStore(path).is_approved(EXE))


def make(root: Path, rel: str, text="x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Discovery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_finds_exes_under_tools_only(self):
        mod = self.root / "A"
        make(mod, "Tools/PhaseLoot/PhaseLoot.exe")
        make(mod, "Tools/PhaseLoot/readme.txt")
        make(mod, "OakGame/Binaries/Win64/other.exe")
        found = tools.discover([("A", mod)])
        self.assertEqual([(t.mod, t.rel, t.label) for t in found],
                         [("A", "Tools/PhaseLoot/PhaseLoot.exe", "PhaseLoot/PhaseLoot.exe")])
        self.assertEqual(found[0].real, mod / "Tools/PhaseLoot/PhaseLoot.exe")

    def test_tools_folder_and_extension_ignore_case(self):
        mod = self.root / "A"
        make(mod, "tOOls/App/Run.EXE")
        self.assertEqual([t.rel for t in tools.discover([("A", mod)])], ["Tools/App/Run.EXE"])

    def test_the_higher_priority_mod_shadows_the_same_path(self):
        low, high = self.root / "low", self.root / "high"
        make(low, "Tools/App/app.exe", "old")
        make(high, "Tools/app/APP.exe", "new")
        make(low, "Tools/Other/o.exe")
        found = {t.rel.casefold(): t.mod for t in tools.discover([("low", low), ("high", high)])}
        self.assertEqual(found, {"tools/app/app.exe": "high", "tools/other/o.exe": "low"})

    def test_mods_without_tools_and_missing_folders_are_skipped(self):
        mod = self.root / "A"
        make(mod, "OakGame/x.pak")
        self.assertEqual(tools.discover([("A", mod), ("gone", self.root / "nope")]), [])

    def test_sha256(self):
        path = make(self.root, "f.bin", "abc")
        self.assertEqual(tools.sha256_of(path), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class NexusLinks(unittest.TestCase):
    def link(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "meta.ini").write_text(text, encoding="utf-8")
            return tools.read_nexus_link(Path(tmp))

    def test_reads_the_ids_from_a_real_meta_ini(self):
        text = '[General]\ngameName=borderlands4\nmodid=397\nversion=1.1.0.0\nrepository=Nexus\nnexusDescription="a=b\\nmodid=5"\n'
        self.assertEqual(self.link(text), tools.NexusLink(397, "borderlands4"))

    def test_not_linked_cases(self):
        self.assertIsNone(self.link("[General]\ngameName=borderlands4\nmodid=0\nrepository=Nexus\n"))
        self.assertIsNone(self.link("[General]\ngameName=borderlands4\nmodid=5\nrepository=\n"))
        self.assertIsNone(self.link("[General]\ngameName=borderlands4\nrepository=Nexus\n"))
        self.assertIsNone(self.link("[General]\nmodid=5\nrepository=Nexus\n"))

    def test_missing_meta_ini(self):
        self.assertIsNone(tools.read_nexus_link(Path("does/not/exist")))


class ProgramNames(unittest.TestCase):
    def name(self, strings, filename="Some Tool.EXE"):
        return peinfo.display_name(Path("nowhere") / filename, reader=lambda path: strings)

    def test_the_file_description_wins(self):
        self.assertEqual(self.name({"FileDescription": "Loot Editor", "ProductName": "PhaseLoot"}), "Loot Editor")

    def test_the_product_name_is_next(self):
        self.assertEqual(self.name({"ProductName": "PhaseLoot"}), "PhaseLoot")

    def test_the_file_name_without_extension_is_the_fallback(self):
        self.assertEqual(self.name({}), "Some Tool")
        self.assertEqual(self.name({}, "PhaseLoot.exe"), "PhaseLoot")

    def test_blank_values_count_as_missing_like_phaselootexes_single_space(self):
        self.assertEqual(self.name({"FileDescription": " ", "ProductName": "\t\r\n "}, "PhaseLoot.exe"), "PhaseLoot")
        self.assertEqual(self.name({"FileDescription": " ", "ProductName": "Real Name"}), "Real Name")

    def test_values_are_tidied(self):
        self.assertEqual(self.name({"FileDescription": "  A\x00B\n  C  "}), "A B C")
        long = self.name({"FileDescription": "x" * 200})
        self.assertEqual((len(long), long[-3:]), (peinfo.MAX_LENGTH, "..."))

    def test_a_reader_that_fails_falls_back_to_the_file_name(self):
        def broken(path):
            raise OSError("no resource")

        self.assertEqual(peinfo.display_name(Path("x/Tool.exe"), reader=broken), "Tool")

    def test_none_values_are_handled(self):
        self.assertEqual(self.name({"FileDescription": None, "ProductName": None}), "Some Tool")

    @unittest.skipUnless(sys.platform == "win32", "reads a Windows version resource")
    def test_a_real_windows_program(self):
        notepad = Path(r"C:\Windows\System32\notepad.exe")
        if not notepad.exists():
            self.skipTest("no notepad")
        self.assertEqual(peinfo.display_name(notepad), "Notepad")

    @unittest.skipUnless(sys.platform == "win32", "reads a Windows version resource")
    def test_a_file_without_a_resource_uses_its_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = make(Path(tmp), "Plain Thing.exe", "not a real exe")
            self.assertEqual(peinfo.display_name(fake), "Plain Thing")

    def test_a_missing_file_uses_its_file_name(self):
        self.assertEqual(peinfo.display_name(Path("does/not/exist/Ghost.exe")), "Ghost")


class MenuLabels(unittest.TestCase):
    def test_unique_names_stay_as_they_are(self):
        self.assertEqual(peinfo.entry_names([("A", "m1"), ("B", "m2")]), ["A", "B"])

    def test_duplicates_get_their_mod_ignoring_case(self):
        self.assertEqual(peinfo.entry_names([("App", "m1"), ("app", "m2"), ("Other", "m3")]),
                         ["App (m1)", "app (m2)", "Other"])

    def test_empty(self):
        self.assertEqual(peinfo.entry_names([]), [])


class TweakRegistry(unittest.TestCase):
    LINK = tools.NexusLink(397, "borderlands4")

    def test_phaseloot_matches_on_mod_id_game_and_exe_name(self):
        self.assertIs(tweaks.match(self.LINK, "PhaseLoot.exe"), tweaks.PHASELOOT)
        self.assertIs(tweaks.match(self.LINK, "phaseloot.EXE"), tweaks.PHASELOOT)

    def test_anything_else_does_not_match(self):
        self.assertIsNone(tweaks.match(None, "PhaseLoot.exe"))
        self.assertIsNone(tweaks.match(self.LINK, "Other.exe"))
        self.assertIsNone(tweaks.match(tools.NexusLink(398, "borderlands4"), "PhaseLoot.exe"))
        self.assertIsNone(tweaks.match(tools.NexusLink(397, "borderlands3"), "PhaseLoot.exe"))

    def test_the_setting_is_named_after_the_key(self):
        self.assertEqual(tweaks.PHASELOOT.setting, "tool_support_phaseloot")
        self.assertEqual(len({t.setting for t in tweaks.TWEAKS}), len(tweaks.TWEAKS))

    def test_find_built_matches_the_pattern_whatever_the_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp)
            self.assertEqual(tweaks.find_built(mod, tweaks.PHASELOOT), [])
            make(mod, "Tools/PhaseLoot/Built PAK/zzz_Drop_Rarity_Custom_1200_P.pak")
            make(mod, "Tools/PhaseLoot/Built PAK/zzz_Drop_Rarity_Custom_1100_P.pak")
            make(mod, "Tools/PhaseLoot/Built PAK/zzz_Other_P.pak")
            make(mod, "Tools/PhaseLoot/Built PAK/zzz_Drop_Rarity_Custom_1100_P.txt")
            self.assertEqual([p.name for p in tweaks.find_built(mod, tweaks.PHASELOOT)],
                             ["zzz_Drop_Rarity_Custom_1100_P.pak", "zzz_Drop_Rarity_Custom_1200_P.pak"])

    def test_a_folder_matching_the_pattern_is_not_a_built_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Tools/PhaseLoot/Built PAK/zzz_Drop_Rarity_Custom_1100_P.pak").mkdir(parents=True)
            self.assertEqual(tweaks.find_built(Path(tmp), tweaks.PHASELOOT), [])

    def test_a_tweak_without_a_built_file_finds_nothing(self):
        self.assertEqual(tweaks.find_built(Path("."), tweaks.ToolTweak("x", "X", "g", 1, "x.exe", "d")), [])

    def test_destination_adds_the_pak_subfolder_only_for_paks(self):
        mod = Path("m")
        self.assertEqual(tweaks.destination(mod, tweaks.PHASELOOT), Path("m/OakGame/Content/Paks"))
        self.assertEqual(tweaks.destination(mod, tweaks.PHASELOOT, "~mods"), Path("m/OakGame/Content/Paks/~mods"))
        self.assertEqual(tweaks.destination(mod, tweaks.PHASELOOT, "\\a/b\\"), Path("m/OakGame/Content/Paks/a/b"))
        other = tweaks.ToolTweak("x", "X", "g", 1, "x.exe", "d", "a/b.bin", "Data/Out")
        self.assertEqual(tweaks.destination(mod, other, "~mods"), Path("m/Data/Out"))

    def test_move_moves_and_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make(Path(tmp), "built/a.pak", "new")
            existing = make(Path(tmp), "out/a.pak", "old")
            dest, replaced = tweaks.move(src, Path(tmp) / "out")
            self.assertEqual((dest, replaced), (existing, True))
            self.assertEqual(dest.read_text(), "new")
            self.assertFalse(src.exists())

    def test_move_creates_the_destination_and_reports_no_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make(Path(tmp), "built/a.pak", "x")
            dest, replaced = tweaks.move(src, Path(tmp) / "deep/er")
            self.assertEqual((dest.read_text(), replaced), ("x", False))

    def test_move_refuses_to_replace_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make(Path(tmp), "built/a.pak")
            (Path(tmp) / "out/a.pak").mkdir(parents=True)
            with self.assertRaises(IsADirectoryError):
                tweaks.move(src, Path(tmp) / "out")
            self.assertTrue(src.exists())


if __name__ == "__main__":
    unittest.main()
