import tempfile
import unittest
from pathlib import Path

from borderlands import steam

NEW_FORMAT = '''"libraryfolders"
{
\t"0"
\t{
\t\t"path"\t\t"C:\\\\Apps\\\\Steam"
\t\t"apps"
\t\t{
\t\t\t"49520"\t\t"26544375178"
\t\t}
\t}
\t"1"
\t{
\t\t"path"\t\t"D:\\\\SteamLibrary"
\t}
}
'''

OLD_FORMAT = '"LibraryFolders"\n{\n\t"TimeNextStatsReport"\t"123"\n\t"1"\t\t"E:\\\\Games\\\\Steam"\n}\n'


class Vdf(unittest.TestCase):
    def test_new_format_paths_and_no_bogus_app_entries(self):
        self.assertEqual(steam.library_paths(NEW_FORMAT), [Path("C:/Apps/Steam"), Path("D:/SteamLibrary")])

    def test_old_format(self):
        self.assertEqual(steam.library_paths(OLD_FORMAT), [Path("E:/Games/Steam")])

    def test_parse_field(self):
        acf = '"AppState"\n{\n\t"appid"\t\t"1285190"\n\t"installdir"\t\t"Borderlands 4"\n\t"buildid"\t\t"26007962"\n}'
        self.assertEqual(steam.parse_field(acf, "installdir"), "Borderlands 4")
        self.assertEqual(steam.parse_field(acf, "BUILDID"), "26007962")
        self.assertIsNone(steam.parse_field(acf, "missing"))


class FindInstall(unittest.TestCase):
    def _library(self, root: Path, app_id: str, installdir: str, create_game_dir=True):
        (root / "steamapps" / "common").mkdir(parents=True)
        (root / "steamapps" / f"appmanifest_{app_id}.acf").write_text(
            f'"AppState"\n{{\n\t"appid"\t"{app_id}"\n\t"installdir"\t"{installdir}"\n\t"buildid"\t"42"\n}}\n'
        )
        if create_game_dir:
            (root / "steamapps" / "common" / installdir).mkdir()

    def test_finds_game_in_second_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_dir, lib2 = Path(tmp) / "Steam", Path(tmp) / "Lib2"
            (steam_dir / "steamapps").mkdir(parents=True)
            self._library(lib2, "397540", "Borderlands 3")
            vdf_path = str(lib2).replace("\\", "\\\\")
            (steam_dir / "steamapps" / "libraryfolders.vdf").write_text(
                f'"libraryfolders"\n{{\n\t"1"\n\t{{\n\t\t"path"\t"{vdf_path}"\n\t}}\n}}\n'
            )
            found = steam.find_install_dir("397540", steam_dir)
            self.assertEqual(found, lib2 / "steamapps" / "common" / "Borderlands 3")
            self.assertEqual(steam.read_build_id(found, "397540"), "42")

    def test_manifest_without_game_dir_is_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_dir = Path(tmp) / "Steam"
            self._library(steam_dir, "49520", "Borderlands 2", create_game_dir=False)
            self.assertIsNone(steam.find_install_dir("49520", steam_dir))

    def test_unknown_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "steamapps").mkdir()
            self.assertIsNone(steam.find_install_dir("1", Path(tmp)))


if __name__ == "__main__":
    unittest.main()
