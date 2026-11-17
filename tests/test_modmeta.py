import json
import tempfile
import unittest
from pathlib import Path

from borderlands import modmeta

ULTRA_PLUS = json.dumps({
    "Name": "UltraPlus",
    "Id": "ultraplus-Borderlands4",
    "Author": "lazorr",
    "Version": "0.2.7",
    "Config": "UltraPlusConfig.ini",
    "Dependencies": [],
    "Permalink": "",
    "WikiLink": "",
    "SettingsSupport": "false",
    "RelativeLocation": "",
    "Data": [{"file": "OakGame/Content/Paks/UltraPlus_Windows_999_P.pak"}],
}, indent=2)


class Parse(unittest.TestCase):
    def test_real_ultra_plus_descriptor(self):
        meta = modmeta.parse(ULTRA_PLUS)
        self.assertEqual((meta.name, meta.id, meta.author, meta.version, meta.config),
                         ("UltraPlus", "ultraplus-Borderlands4", "lazorr", "0.2.7", "UltraPlusConfig.ini"))
        self.assertEqual(meta.files, ("OakGame/Content/Paks/UltraPlus_Windows_999_P.pak",))
        self.assertEqual(meta.dependencies, ())
        self.assertEqual(meta.url, "")

    def test_keys_are_case_insensitive(self):
        meta = modmeta.parse('{"id": "x", "data": [], "version": "1.0", "PERMALINK": "https://e.example"}')
        self.assertEqual((meta.id, meta.version, meta.url), ("x", "1.0", "https://e.example"))

    def test_permalink_preferred_over_wiki(self):
        meta = modmeta.parse('{"Id":"x","Data":[],"Permalink":"https://a","WikiLink":"https://b"}')
        self.assertEqual(meta.url, "https://a")
        meta = modmeta.parse('{"Id":"x","Data":[],"WikiLink":"https://b"}')
        self.assertEqual(meta.url, "https://b")

    def test_dependencies_as_strings_or_objects(self):
        meta = modmeta.parse('{"Id":"x","Data":[],"Dependencies":["a",{"Id":"b"},{"Name":"c"},{},3]}')
        self.assertEqual(meta.dependencies, ("a", "b", "c"))

    def test_unrelated_metadata_json_is_not_ours(self):
        for text in ('{"name": "x", "version": "1"}',         # no Id / Data
                     '{"Id": "x"}',                            # no Data list
                     '{"Id": "", "Data": []}',                 # empty id
                     '{"Id": 5, "Data": []}',                  # non-string id
                     '[1, 2]', '"text"', 'not json', ''):
            self.assertIsNone(modmeta.parse(text), text)

    def test_odd_field_types_do_not_crash(self):
        meta = modmeta.parse('{"Id":"x","Data":["str",{"file":5},{"file":"a.pak"}],"Version":2,"Author":null}')
        self.assertEqual(meta.files, ("a.pak",))
        self.assertEqual((meta.version, meta.author), ("", ""))


class SkipSuffixSetting(unittest.TestCase):
    def check(self, line):
        return modmeta.hidden_suffix_is_skipped(f"[General]\nx=1\n[Settings]\n{line}\nskip_directories=.git\n")

    def test_default_value(self):
        self.assertTrue(self.check("skip_file_suffixes=.mohidden"))

    def test_key_absent_means_default(self):
        self.assertTrue(modmeta.hidden_suffix_is_skipped("[Settings]\nskip_directories=.git\n"))
        self.assertTrue(modmeta.hidden_suffix_is_skipped(""))

    def test_list_forms(self):
        self.assertTrue(self.check("skip_file_suffixes=.bak, .mohidden"))
        self.assertTrue(self.check('skip_file_suffixes=".bak", ".MOHIDDEN"'))
        self.assertTrue(self.check("skip_file_suffixes = .mohidden "))

    def test_removed_or_replaced(self):
        self.assertFalse(self.check("skip_file_suffixes=.bak"))
        self.assertFalse(self.check("skip_file_suffixes="))
        self.assertFalse(self.check("skip_file_suffixes=.mohidden2"))

    def test_unreadable_encoding_assumes_default(self):
        self.assertTrue(self.check("skip_file_suffixes=@Variant(\\0\\0\\0\\x0b)"))

    def test_other_key_with_similar_name_is_ignored(self):
        self.assertFalse(self.check("other_skip_file_suffixes=.mohidden\nskip_file_suffixes=.bak"))


class NewFields(unittest.TestCase):
    def test_settings_support_and_relative_location(self):
        meta = modmeta.parse('{"Id":"x","Data":[],"SettingsSupport":"false","RelativeLocation":"Binaries/Win64"}')
        self.assertEqual((meta.settings_support, meta.relative_location), ("false", "Binaries/Win64"))

    def test_defaults_when_absent(self):
        meta = modmeta.parse('{"Id":"x","Data":[]}')
        self.assertEqual((meta.settings_support, meta.relative_location), ("", ""))


class Files(unittest.TestCase):
    def test_find_read_hide_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp)
            (mod / "OakGame").mkdir()
            (mod / "MetaData.JSON").write_text(ULTRA_PLUS, encoding="utf-8-sig")
            path = modmeta.find(mod)
            self.assertEqual(path.name, "MetaData.JSON")
            self.assertEqual(modmeta.read(path).id, "ultraplus-Borderlands4")
            hidden = modmeta.hide(path)
            self.assertEqual(hidden.name, "MetaData.JSON.mohidden")
            self.assertTrue(hidden.is_file())
            self.assertIsNone(modmeta.find(mod))

    def test_hide_replaces_existing_hidden_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp)
            (mod / "metadata.json").write_text("new")
            (mod / "metadata.json.mohidden").write_text("old")
            modmeta.hide(mod / "metadata.json")
            self.assertEqual((mod / "metadata.json.mohidden").read_text(), "new")

    def test_find_ignores_nested_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp)
            (mod / "sub").mkdir()
            (mod / "sub" / "metadata.json").write_text(ULTRA_PLUS)
            self.assertIsNone(modmeta.find(mod))
            self.assertIsNone(modmeta.find(mod / "does-not-exist"))

    def test_oversized_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "metadata.json"
            p.write_text(" " * (modmeta.MAX_BYTES + 1))
            self.assertIsNone(modmeta.read(p))


if __name__ == "__main__":
    unittest.main()
