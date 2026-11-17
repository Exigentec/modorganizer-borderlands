"""Keep MO2's usvfs (its virtual file system) new enough for SDK mods, and swap versions safely.

Why it matters: the SDK's embedded Python 3.14 checks whether a file exists with
``GetFileInformationByName`` and believes a "not found" answer.  usvfs before 0.5.7.1 does not
redirect that call, so files that exist only in the virtual file system (mods) look missing to the SDK's
mod loader.  MO2 2.5.2 ships usvfs 0.5.6.1.

This module is the file handling and Windows details; the dialogs are in :mod:`usvfsmanager`.

* The four binaries live loose next to ``ModOrganizer.exe``.  A DLL that is loaded cannot be
  overwritten, but it can be renamed, so replacing moves the old file aside first
  (``<name>.replaced``, deleted at the next start) and rolls back if anything fails.
* The release to install is pinned in :data:`TARGET`: the archive's SHA-256 and each binary's SHA-256
  are constants here, so a changed download is refused.
* Before anything is replaced the current four files are saved to ``usvfs_versions/usvfs_<version>.zip``
  next to them; those backups are what "revert" restores.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import netio

FILES = ("usvfs_x64.dll", "usvfs_x86.dll", "usvfs_proxy_x64.exe", "usvfs_proxy_x86.exe")
FIXED_IN = "0.5.7.1"          # the first release that redirects the call
REPLACED_SUFFIX = ".replaced"
BACKUP_DIR = "usvfs_versions"   # next to ModOrganizer.exe, holds the saved versions


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    archive_sha256: str
    folder: str                # where the binaries are inside the archive
    files: dict[str, str]      # name -> SHA-256 of the binary


TARGET = Release(
    version="0.5.7.2",
    url="https://github.com/ModOrganizer2/usvfs/releases/download/v0.5.7.2/usvfs_v0.5.7.2.7z",
    archive_sha256="c6252eed78ee1c307733a4412cb68522cffc48107be4795c4e38b2b8d7c76d01",
    folder="bin/",
    files={
        "usvfs_x64.dll": "7ee7758433ab76713900e661056be8074b9c567971fde38fd0e514c76895e274",
        "usvfs_x86.dll": "de23207b87aa99a1c15ec29185e3cf1e50dce4b1473eeb894c02abdaa23313fc",
        "usvfs_proxy_x64.exe": "491d4d7e3fce9876e904f8cccb648f43def428a2a527d41f0221d9c0ce1d408e",
        "usvfs_proxy_x86.exe": "e37a485fcebbde9583005913a34bfdd2bd49443438c30b6dc75bee83bb261b04",
    },
)


# -- versions ------------------------------------------------------------------------------


def version_key(text: str | None) -> tuple[int, ...] | None:
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in numbers[:4]) if numbers else None


def is_fixed(version: str | None) -> bool:
    key = version_key(version)
    return key is not None and key + (0,) * (4 - len(key)) >= version_key(FIXED_IN)


def file_version(path: Path | str) -> str | None:
    """The file's version resource as ``a.b.c.d``; Windows only, None if there is none."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    try:
        version = ctypes.WinDLL("version")
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None

        class FixedInfo(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint32) for n in
                        ("sig", "sv", "fvms", "fvls", "pvms", "pvls", "m", "f", "os", "ft", "fst", "dh", "dl")]

        info = FixedInfo.from_address(pointer.value)
        return f"{info.fvms >> 16}.{info.fvms & 0xffff}.{info.fvls >> 16}.{info.fvls & 0xffff}"
    except (OSError, ValueError, AttributeError):
        return None


def installed_version(root: Path, reader=file_version) -> str | None:
    """The version of the usvfs files on disk in ``root``."""
    return reader(Path(root) / FILES[0])


def loaded_version() -> str | None:
    """The version of the usvfs this process (MO2) actually has loaded, via the library's own export.

    Both the old and the new library export ``usvfsVersionString``.  None if usvfs is not loaded
    here or the call is not possible.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    try:
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetProcAddress.restype = ctypes.c_void_p
        kernel.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        module = kernel.GetModuleHandleW(FILES[0])
        address = module and kernel.GetProcAddress(module, b"usvfsVersionString")
        if not address:
            return None
        text = ctypes.WINFUNCTYPE(ctypes.c_char_p)(address)()
        found = re.search(rb"\d+(?:\.\d+)+", text or b"")
        return found.group(0).decode() if found else None
    except (OSError, ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class Status:
    disk: str | None     # the files on disk
    loaded: str | None   # what MO2 is running right now

    @property
    def needs_upgrade(self) -> bool:
        return not is_fixed(self.disk)

    @property
    def pending_restart(self) -> bool:
        """New files are on disk but MO2 still runs the old library."""
        return is_fixed(self.disk) and self.loaded is not None and not is_fixed(self.loaded)

    @property
    def running_fixed(self) -> bool:
        return is_fixed(self.loaded)


def status(root: Path, reader=file_version, loaded=loaded_version) -> Status:
    return Status(installed_version(root, reader), loaded())


# -- backups -------------------------------------------------------------------------------

_BACKUP_NAME = re.compile(r"^usvfs_(\d+(?:\.\d+)*)(?:_\d+)?\.zip$", re.I)


@dataclass(frozen=True)
class Backup:
    version: str
    path: Path


def _zip_members(path: Path) -> dict[str, bytes] | None:
    try:
        with zipfile.ZipFile(path) as z:
            names = [i.filename for i in z.infolist() if not i.is_dir()]
            if sorted(names) != sorted(FILES):
                return None
            return {n: z.read(n) for n in names}
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None


def backup(root: Path, version: str) -> Path:
    """Save the four current files as ``usvfs_versions/usvfs_<version>.zip`` (an identical one is reused)."""
    root = Path(root)
    current = {name: (root / name).read_bytes() for name in FILES}  # FileNotFoundError if one is missing
    folder = root / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path, n = folder / f"usvfs_{version}.zip", 1
    while path.exists():
        if _zip_members(path) == current:
            return path
        n += 1
        path = folder / f"usvfs_{version}_{n}.zip"
    temp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            z.writestr(name, current[name])
    os.replace(temp, path)
    return path


def list_backups(root: Path) -> list[Backup]:
    """The usable backups in ``root/usvfs_versions``, oldest version first."""
    found = []
    try:
        entries = sorted((Path(root) / BACKUP_DIR).iterdir())
    except OSError:
        return []
    for path in entries:
        match = _BACKUP_NAME.match(path.name)
        if match and path.is_file() and _zip_members(path) is not None:
            found.append(Backup(match.group(1), path))
    return sorted(found, key=lambda b: (version_key(b.version) or (), b.path.name))


def migrate_backups(root: Path) -> list[Path]:
    """Move backups saved loose next to ``ModOrganizer.exe`` (by an earlier version) into ``usvfs_versions``."""
    root = Path(root)
    moved = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return moved
    for path in entries:
        match = _BACKUP_NAME.match(path.name)
        if not (match and path.is_file() and _zip_members(path) is not None):
            continue
        folder = root / BACKUP_DIR
        folder.mkdir(parents=True, exist_ok=True)
        target, n = folder / path.name, 1
        while target.exists():
            n += 1
            target = folder / f"usvfs_{match.group(1)}_{n}.zip"
        try:
            os.replace(path, target)
        except OSError:
            continue
        moved.append(target)
    return moved


def extract_backup(backup_: Backup, stage: Path, reader=file_version) -> dict[str, Path]:
    """Unpack a backup into ``stage``; refuses one whose x64 DLL is not the version its name says."""
    members = _zip_members(backup_.path)
    if members is None:
        raise ValueError(f"{backup_.path.name} is not a usvfs backup")
    stage = Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, data in members.items():
        (stage / name).write_bytes(data)
        files[name] = stage / name
    found = reader(files[FILES[0]])
    if found is not None and version_key(found) != version_key(backup_.version):
        raise ValueError(f"{backup_.path.name} holds usvfs {found}, not {backup_.version}")
    return files


# -- replacing -----------------------------------------------------------------------------


def _aside_name(path: Path) -> Path:
    candidate, n = path.with_name(path.name + REPLACED_SUFFIX), 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}{REPLACED_SUFFIX}{n}")
    return candidate


def replace_files(root: Path, new_files: dict[str, Path]) -> list[str]:
    """Put ``new_files`` into ``root``, moving each old file aside first (a loaded DLL can be renamed).

    All or nothing: on any failure the files already swapped are put back.  Returns the names replaced.
    """
    root = Path(root)
    moved: list[tuple[Path, Path]] = []
    placed: list[Path] = []
    try:
        for name, source in new_files.items():
            target = root / name
            if target.exists():
                aside = _aside_name(target)
                os.replace(target, aside)
                moved.append((target, aside))
            shutil.copy2(source, target)
            placed.append(target)
    except OSError:
        for target in placed:
            try:
                target.unlink()
            except OSError:
                pass
        for target, aside in reversed(moved):
            try:
                os.replace(aside, target)
            except OSError:
                pass
        raise
    return list(new_files)


def cleanup_replaced(root: Path) -> int:
    """Delete the old files set aside by an earlier replacement (they were still loaded then)."""
    removed = 0
    try:
        leftovers = [p for p in Path(root).iterdir()
                     if p.is_file() and p.name.startswith("usvfs_") and REPLACED_SUFFIX in p.name]
    except OSError:
        return 0
    for path in leftovers:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass  # still loaded; the next start tries again
    return removed


# -- downloading ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _tar(run=subprocess.run) -> str:
    """The ``tar`` that can read 7z: Windows' own (bsdtar), never just whichever is first on PATH.

    Git for Windows and Cygwin put a GNU tar on PATH, often ahead of Windows', and GNU tar cannot read
    7z ("This does not look like a tar archive").
    """
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    if system.exists():
        return str(system)
    found = shutil.which("tar")
    if found:
        try:
            out = run([found, "--version"], capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        if "bsdtar" in out.casefold() or "libarchive" in out.casefold():
            return found
    raise RuntimeError("Windows' tar.exe (bsdtar) was not found, so the 7z archive cannot be unpacked")


def fetch(release: Release, workdir: Path, download=netio.download, progress=None,
          run=subprocess.run) -> dict[str, Path]:
    """Download ``release``, check it, and return ``{name: path}`` of its four binaries.

    The archive must match its pinned SHA-256 (``netio.download`` checks it), only the four named files
    are unpacked (with Windows' own tar), and each is checked against its pinned SHA-256 as well.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    archive = workdir / Path(release.url).name
    download(release.url, archive, release.archive_sha256, progress)
    tar = _tar()
    listing = run([tar, "-tf", str(archive)], capture_output=True, text=True,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if listing.returncode != 0:
        raise ValueError(f"the usvfs archive could not be read ({Path(tar).name} exit {listing.returncode}: "
                         f"{(listing.stderr or '').strip()[:200] or 'no message'})")
    names = [n.strip() for n in listing.stdout.splitlines()]
    members = [release.folder + name for name in FILES]
    missing = [m for m in members if m not in names]
    if missing:
        raise ValueError(f"the usvfs archive does not hold the expected files ({', '.join(missing)})")
    stage = workdir / "extract"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    done = run([tar, "-xf", str(archive), "-C", str(stage), *members], capture_output=True, text=True,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        raise ValueError(f"unpacking the usvfs archive failed: {done.stderr.strip()[:200]}")
    files = {}
    for name in FILES:
        path = stage / release.folder / name
        if not path.is_file() or stage.resolve() not in path.resolve().parents:
            raise ValueError(f"{name} was not unpacked")
        if _sha256(path) != release.files[name]:
            raise netio.ChecksumError(f"{name} does not match the checksum this plugin expects for usvfs {release.version}")
        files[name] = path
    return files


def new_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="borderlands-usvfs-"))


# -- restarting MO2 ------------------------------------------------------------------------


def _ps(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def restart_command(exe: Path, pid: int, instance: str | None = None, profile: str | None = None,
                    log: Path | None = None) -> list[str]:
    """A PowerShell command line that waits for MO2 (``pid``) to exit, tidies up and starts it again.

    It only starts MO2 if the old process really ended within 90 seconds, so cancelling MO2's own
    "are you sure you want to exit" does not lead to a second copy.  Once MO2 has exited the old usvfs
    files set aside by an upgrade (``*.replaced``, loaded until now) are deleted.  Every step is
    appended to ``log`` (if given), so a restart that does not happen can be diagnosed.
    """
    args = []
    if instance:
        args += ["-i", instance]
    if profile:
        args += ["-p", profile]
    # Start-Process joins an argument array with plain spaces, so "Borderlands 4" would reach MO2 as two
    # arguments: pass one string with the quoting done here (neither name can contain a double quote)
    if any('"' in a for a in args):
        raise ValueError("an instance or profile name with a double quote cannot be passed on the command line")
    line = " ".join(f'"{a}"' if " " in a else a for a in args)
    argument_list = f"-ArgumentList {_ps(line)}" if args else ""
    folder = Path(exe).parent
    pid = int(pid)
    note = (f"function L($m) {{ try {{ Add-Content -LiteralPath {_ps(str(log))} -Value ((Get-Date -Format s) + ' ' + $m) }} "
            "catch {} }; ") if log else "function L($m) {}; "
    script = (
        note
        + f"L 'helper started; waiting for MO2 (pid {pid}) to exit'; "
        + f"try {{ Wait-Process -Id {pid} -Timeout 90 -ErrorAction Stop }} catch {{ L ('wait: ' + $_) }}; "
        + f"if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) {{ "
        + f"L 'MO2 has exited'; "
        + f"foreach ($f in Get-ChildItem -LiteralPath {_ps(str(folder))} -File -ErrorAction SilentlyContinue "
        + f"| Where-Object {{ $_.Name -like 'usvfs_*' -and $_.Name -like '*{REPLACED_SUFFIX}*' }}) "
        + "{ try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop; L ('removed ' + $f.Name) } "
        + "catch { L ('could not remove ' + $f.Name + ': ' + $_) } }; "
        + f"try {{ Start-Process -FilePath {_ps(str(exe))} {argument_list} -WorkingDirectory {_ps(str(folder))} "
        + "-ErrorAction Stop; L 'started MO2' } catch { L ('could not start MO2: ' + $_) } "
        + "} else { L 'MO2 is still running after 90 s (its exit was cancelled?): not starting a second copy' }"
    )
    return ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script]


def spawn_restart(command: list[str], spawn=subprocess.Popen) -> None:
    """Start the helper so that it outlives MO2.

    Not ``DETACHED_PROCESS``: with it PowerShell starts and then never runs its command (tested), so MO2
    closed and nothing reopened it.  ``CREATE_NO_WINDOW`` gives it a hidden console instead.  Breaking away
    from MO2's job object (if it has one) is tried first and dropped if the job forbids it.
    """
    base = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    streams = dict(close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        spawn(command, creationflags=base | breakaway, **streams)
    except PermissionError:
        spawn(command, creationflags=base, **streams)
