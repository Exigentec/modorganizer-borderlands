"""The unreal SDK that most Borderlands 4 mods need: release lookup, a cached copy of the release
archive, and checking/placing/removing its files.  Pure file handling, no MO2 or Qt.

Nothing about the SDK's layout is hard-coded.  The manifest (every file, its size and SHA-256) is
learned from the cached release archive, so a different layout in a future release just works.

Safety rules that apply to everything below:

* An archive is refused outright if any entry is absolute, climbs out with ``..``, has a drive
  letter or backslash, collides with another entry ignoring case, or names a protected file (the
  game's own executable).
* Files are only ever written below the game folder, through a temporary name that is hashed
  before it replaces anything, and only the files a caller names.
* Removal deletes a file only if its SHA-256 still matches the archive; a modified file stays.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import netio

MAX_FILES = 2000
MAX_TOTAL_BYTES = 500 * 1024 * 1024
CHUNK = 1 << 20
PART_SUFFIX = ".sdk-part"

# Folders under sdk_mods that are never a mod of their own.
NOT_MODS = frozenset({"settings", ".stubs"})


class UnsafeArchive(Exception):
    """The release archive cannot be trusted or used; the message says why."""


class NoRelease(Exception):
    """GitHub has no usable release (none published, or the asset is missing or odd)."""


@dataclass(frozen=True)
class Entry:
    rel: str      # path inside the archive and below the game folder, always with "/"
    size: int
    sha256: str


Manifest = dict[str, Entry]


def path_of(root: Path | str, rel: str) -> Path:
    return Path(root).joinpath(*rel.split("/"))


# -- the archive ---------------------------------------------------------------------------


def _check_name(name: str) -> str:
    bad = (not name or name.startswith("/") or "\\" in name or ":" in name or "\x00" in name)
    parts = name.split("/")
    bad = bad or any(p in ("", ".", "..") for p in parts)
    # Windows silently drops trailing dots and spaces, so such a name would land somewhere else
    bad = bad or any(p != p.rstrip(" .") for p in parts)
    if bad:
        raise UnsafeArchive(f"it contains an entry with an unsafe name: {name!r}")
    return name


def read_manifest(zip_path: Path | str, protected: Iterable[str] = ()) -> Manifest:
    """Every file in the archive with its size and SHA-256; raises :class:`UnsafeArchive`."""
    protected_keys = {p.replace("\\", "/").casefold() for p in protected}
    entries: Manifest = {}
    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = [i for i in archive.infolist() if not i.is_dir()]
            if len(infos) > MAX_FILES:
                raise UnsafeArchive(f"it contains {len(infos)} files, more than the {MAX_FILES} allowed")
            for info in infos:
                rel = _check_name(info.filename)
                key = rel.casefold()
                if key in seen:
                    raise UnsafeArchive(f"two entries differ only in case: {rel}")
                seen.add(key)
                if key in protected_keys:
                    raise UnsafeArchive(f"it would replace {rel}, which belongs to the game")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise UnsafeArchive("it is far larger than an SDK release should be")
                digest = hashlib.sha256()
                with archive.open(info) as handle:
                    while block := handle.read(CHUNK):
                        digest.update(block)
                entries[rel] = Entry(rel, info.file_size, digest.hexdigest())
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as err:
        raise UnsafeArchive(f"it is not a usable zip file ({err})") from err
    if not entries:
        raise UnsafeArchive("it contains no files")
    return entries


def base_names(manifest: Manifest) -> frozenset[str]:
    """Case-folded names directly under ``sdk_mods`` that the SDK itself ships (its own mods)."""
    names = set()
    for rel in manifest:
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0].casefold() == "sdk_mods":
            names.add(parts[1].casefold())
    return frozenset(names)


# -- checking ----------------------------------------------------------------------------


class Hasher:
    """SHA-256 of files, remembered per path, size and modification time."""

    def __init__(self):
        self._cache: dict[tuple[str, int, int], str] = {}

    def sha256(self, path: Path) -> str | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in self._cache:
            digest = hashlib.sha256()
            try:
                with open(path, "rb") as handle:
                    while block := handle.read(CHUNK):
                        digest.update(block)
            except OSError:
                return None
            self._cache[key] = digest.hexdigest()
        return self._cache[key]


@dataclass
class Report:
    ok: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)  # present, but not the archive's version

    @property
    def complete(self) -> bool:
        return not self.missing and not self.modified


def verify(manifest: Manifest, game_dir: Path, hasher: Hasher) -> Report:
    """Check every file of the archive in the real game folder, as the vendor's install puts them.

    Files supplied through MO2's virtual file system do not count: the loader and its Python
    runtime have to be real files for the game to start.
    """
    report = Report()
    for rel, entry in manifest.items():
        path = path_of(game_dir, rel)
        if not path.is_file():
            report.missing.append(rel)
        elif hasher.sha256(path) == entry.sha256:
            report.ok.append(rel)
        else:
            report.modified.append(rel)
    return report


# -- placing and removing ----------------------------------------------------------------


@dataclass
class ApplyResult:
    written: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def extract(zip_path: Path | str, manifest: Manifest, game_dir: Path, rels: Iterable[str],
            progress: Callable[[int, int], bool] | None = None) -> ApplyResult:
    """Write the named files into ``game_dir``; each is hashed before it replaces anything."""
    result = ApplyResult()
    rels = list(rels)
    root = Path(game_dir).resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for index, rel in enumerate(rels):
            if progress and progress(index, len(rels)) is False:
                break
            entry = manifest[rel]
            target = path_of(root, rel)
            temp = target.with_name(target.name + PART_SUFFIX)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if root not in target.resolve().parents:
                    raise OSError("it would land outside the game folder")
                digest = hashlib.sha256()
                with archive.open(rel) as source, open(temp, "wb") as out:
                    while block := source.read(CHUNK):
                        digest.update(block)
                        out.write(block)
                if digest.hexdigest() != entry.sha256:
                    raise OSError("the archive changed while it was being read")
                os.replace(temp, target)
                result.written.append(rel)
            except (OSError, KeyError, zipfile.BadZipFile) as err:
                result.failed.append((rel, str(err)))
                try:
                    temp.unlink()
                except OSError:
                    pass
    if progress:
        progress(len(rels), len(rels))
    return result


@dataclass
class PurgeResult:
    removed: list[str] = field(default_factory=list)
    kept_modified: list[str] = field(default_factory=list)   # present, but not the archive's version
    absent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def purge(manifest: Manifest, game_dir: Path, hasher: Hasher) -> PurgeResult:
    """Remove the archive's files from the game folder, one by one, if their hash still matches.

    A file that differs is left alone and reported.  Folders left empty are removed (a folder with
    anything else in it, such as the user's own mods, simply stays).
    """
    result = PurgeResult()
    root = Path(game_dir)
    for rel, entry in manifest.items():
        path = path_of(root, rel)
        if not path.is_file():
            result.absent.append(rel)
        elif hasher.sha256(path) != entry.sha256:
            result.kept_modified.append(rel)
        else:
            try:
                path.unlink()
                result.removed.append(rel)
            except OSError as err:
                result.failed.append((rel, str(err)))
    folders = {parent for rel in result.removed for parent in path_of(root, rel).parents
               if parent != root and root in parent.parents}
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return result


def game_running(binary: str, run=subprocess.run) -> bool:
    """Whether the game's executable is a running process (Windows only; False elsewhere)."""
    if sys.platform != "win32":
        return False
    name = binary.replace("\\", "/").rsplit("/", 1)[-1]
    try:
        out = run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"], capture_output=True,
                  text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return name.casefold() in out.casefold()


# -- state ---------------------------------------------------------------------------------


class SdkState:
    """What the plugin remembers about the SDK, in a small JSON file."""

    FIELDS = {"enabled": None, "tag": "", "zip_sha256": "", "skipped_tag": "", "setup_declined": False}

    def __init__(self, path: Path):
        self._path = Path(path)
        self._data = dict(self.FIELDS)
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data.update({k: v for k, v in loaded.items() if k in self.FIELDS})
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        if name in SdkState.FIELDS:
            return self._data[name]
        raise AttributeError(name)

    @property
    def configured(self) -> bool:
        """Whether the user has set the SDK up (it is either enabled or switched off)."""
        return self._data["enabled"] is not None

    def update(self, **values) -> None:
        for key in values:
            if key not in self.FIELDS:
                raise KeyError(key)
        self._data.update(values)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")


# -- releases ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    tag: str
    asset: str
    size: int
    sha256: str   # from GitHub's asset digest; empty when GitHub has none for this asset
    url: str
    page: str


def parse_latest(payload, repo: str, asset_name: str) -> Release:
    """A :class:`Release` from GitHub's "latest release" answer; raises :class:`NoRelease`."""
    try:
        if payload.get("draft") or payload.get("prerelease"):
            raise NoRelease("the latest release is a draft or pre-release")
        tag = payload["tag_name"]
        if not isinstance(tag, str) or not tag.strip():
            raise NoRelease("the release has no tag")
        asset = next((a for a in payload.get("assets", []) if a.get("name") == asset_name), None)
        if asset is None:
            raise NoRelease(f"release {tag} has no {asset_name}")
        url = str(asset["browser_download_url"])
    except (AttributeError, KeyError, TypeError) as err:
        raise NoRelease("GitHub sent an answer this plugin does not understand") from err
    if not url.startswith(f"https://github.com/{repo}/releases/download/"):
        raise NoRelease("the download address is not on the project's GitHub releases")
    digest = str(asset.get("digest") or "")
    sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else ""
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        sha = ""
    return Release(tag, asset_name, int(asset.get("size") or 0), sha, url,
                   str(payload.get("html_url") or f"https://github.com/{repo}/releases"))


def latest_release(repo: str, asset_name: str, fetch=netio.request_json) -> Release:
    status, payload = fetch(f"https://api.github.com/repos/{repo}/releases/latest",
                            headers={"Accept": "application/vnd.github+json"})
    if status == 404:
        raise NoRelease("the project has no published release")
    if status in (403, 429):
        raise netio.NetworkError("GitHub's request limit was reached; try again later")
    if status != 200:
        raise netio.NetworkError(f"GitHub answered HTTP {status}")
    return parse_latest(payload, repo, asset_name)


def _version_key(tag: str) -> tuple[int, ...] | None:
    numbers = re.findall(r"\d+", tag)
    return tuple(int(n) for n in numbers) if numbers else None


def is_newer(latest: str, installed: str) -> bool:
    """Whether ``latest`` is a newer tag than ``installed``; unparseable tags compare as different."""
    if not installed:
        return True
    a, b = _version_key(latest), _version_key(installed)
    if a is None or b is None:
        return latest != installed
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


# -- which mods are SDK mods ---------------------------------------------------------------


def _is_sdk_mod_entry(name: str, is_dir: bool) -> bool:
    folded = name.casefold()
    if folded in NOT_MODS:
        return False
    if is_dir:
        return not name.startswith(".") and "." not in name  # a Python package folder
    return folded.endswith(".sdkmod")


def sdk_mod_names(sdk_mods_dir: Path, base: frozenset[str] = frozenset()) -> list[str]:
    """Names of the SDK mods directly inside an ``sdk_mods`` folder (the SDK's own ones excluded)."""
    try:
        children = list(Path(sdk_mods_dir).iterdir())
    except OSError:
        return []
    return sorted(c.name for c in children
                  if c.name.casefold() not in base and _is_sdk_mod_entry(c.name, c.is_dir()))


def mod_has_sdk_mods(mod_dir: Path, base: frozenset[str] = frozenset()) -> bool:
    try:
        folder = next((c for c in Path(mod_dir).iterdir() if c.is_dir() and c.name.casefold() == "sdk_mods"), None)
    except OSError:
        return False
    return folder is not None and bool(sdk_mod_names(folder, base))


def tree_has_sdk_mods(tree, base: frozenset[str] = frozenset()) -> bool:
    """Same as :func:`mod_has_sdk_mods`, for an MO2 ``IFileTree``."""
    folder = tree.find("sdk_mods")
    if folder is None or not folder.isDir():
        return False
    return any(child.name().casefold() not in base and _is_sdk_mod_entry(child.name(), child.isDir())
               for child in folder)
