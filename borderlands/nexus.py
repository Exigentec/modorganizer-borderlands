"""Check an installed program against what Nexus Mods published for the same mod.

Uses two public, key-less endpoints (verified 2026-11-16):

* the GraphQL API (``api.nexusmods.com/v2/graphql``) for the mod's status and its files,
  each with a virus scan status (``scannedV2``) and a ``uri``;
* ``mod-file-manifests.nexusmods.com/<uri>``, a JSON listing of every file inside that
  archive with its size and SHA-1/256/512/MD5, which is what the website's "Preview file
  contents" box shows.

So the SHA-256 of an extracted ``.exe`` can be compared with the published one without
needing the downloaded archive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import netio

GRAPHQL = "https://api.nexusmods.com/v2/graphql"
MANIFESTS = "https://mod-file-manifests.nexusmods.com/"

CLEAN = frozenset({"VERIFIED", "INTERNALLY_VERIFIED", "MANUALLY_VERIFIED"})
IN_PROGRESS = frozenset({"NOT_SCANNED", "QUEUED", "WAITING_REPORT", "PARTIAL"})
# QUARANTINED (held for manual review), FILE_NOT_FOUND, MOD_DOES_NOT_EXIST, TOO_LARGE,
# REPORT_ERROR and any status Nexus adds later all mean "do not run".

MAX_FILES = 40  # manifests fetched per mod


@dataclass(frozen=True)
class Verdict:
    """``kind``: clean | in_progress | refuse | mismatch | unavailable."""

    kind: str
    reason: str = ""
    file_name: str = ""
    file_version: str = ""


@dataclass
class _ModInfo:
    gone: bool = False
    status: str = ""
    files: list[dict] = field(default_factory=list)
    hashes: dict[int, set[str]] = field(default_factory=dict)  # fileId -> sha256 set
    manifest_failures: int = 0


def _query(game_id: int, mod_id: int) -> str:
    return (
        f"{{ mod(modId:{int(mod_id)}, gameId:{int(game_id)}){{ status }} "
        f"modFiles(modId:{int(mod_id)}, gameId:{int(game_id)}){{ fileId name version category scannedV2 uri }} }}"
    )


def _manifest_hashes(payload) -> set[str] | None:
    try:
        return {f["file_hashes"]["SHA256"].lower() for f in payload["files"] if f.get("file_hashes", {}).get("SHA256")}
    except (KeyError, TypeError, AttributeError):
        return None


def _rank(scan: str) -> int:
    return 0 if scan in CLEAN else 1 if scan in IN_PROGRESS else 2


def _refusal_for(scan: str) -> str:
    if scan == "QUARANTINED":
        return "Nexus Mods has quarantined this file for manual review."
    if scan in ("FILE_NOT_FOUND", "MOD_DOES_NOT_EXIST"):
        return f"Nexus Mods no longer has this file ({scan})."
    return f"Nexus Mods reports the scan status '{scan}', which this plugin does not treat as safe."


class NexusClient:
    """Looks programs up on Nexus Mods; caches per mod for the life of the session."""

    def __init__(self, fetch=netio.request_json):
        self._fetch = fetch
        self._mods: dict[tuple[int, int], _ModInfo] = {}

    def _load(self, game_id: int, mod_id: int) -> _ModInfo:
        key = (game_id, mod_id)
        if key in self._mods:
            return self._mods[key]
        status, payload = self._fetch(GRAPHQL, data={"query": _query(game_id, mod_id)})
        info = _ModInfo()
        errors = (payload or {}).get("errors") if isinstance(payload, dict) else None
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        if errors and any((e.get("extensions") or {}).get("code") == "NOT_FOUND" for e in errors if isinstance(e, dict)):
            info.gone = True
        elif status != 200 or not isinstance(data, dict) or not data.get("mod"):
            raise netio.NetworkError(f"unexpected answer from Nexus Mods (HTTP {status})")
        else:
            info.status = str(data["mod"].get("status") or "")
            info.files = list(data.get("modFiles") or [])
            for f in info.files[:MAX_FILES]:
                uri = f.get("uri")
                if not uri:
                    info.manifest_failures += 1
                    continue
                try:
                    code, manifest = self._fetch(MANIFESTS + uri)
                except netio.NetworkError:
                    info.manifest_failures += 1
                    continue
                hashes = _manifest_hashes(manifest) if code == 200 else None
                if hashes is None:
                    info.manifest_failures += 1
                else:
                    info.hashes[f["fileId"]] = hashes
        self._mods[key] = info
        return info

    def check(self, sha256: str, game_id: int, mod_id: int) -> Verdict:
        """Verdict for the program with this SHA-256 belonging to this Nexus mod."""
        try:
            info = self._load(game_id, mod_id)
        except netio.NetworkError as err:
            return Verdict("unavailable", f"Could not reach Nexus Mods ({err}).")
        if info.gone:
            return Verdict("refuse", "The mod's page no longer exists on Nexus Mods.")
        if info.status != "published":
            return Verdict("refuse", f"The mod's status on Nexus Mods is '{info.status or 'unknown'}', not 'published'.")

        wanted = sha256.lower()
        matches = [f for f in info.files if wanted in info.hashes.get(f.get("fileId"), ())]
        if not matches:
            if info.manifest_failures:
                return Verdict("unavailable", "Could not read the file listings from Nexus Mods.")
            return Verdict("mismatch", "This program does not match any file published for this mod on Nexus Mods.")

        live = [f for f in matches if f.get("category") != "REMOVED"]
        if not live:
            return Verdict("refuse", "The only matching file was removed from Nexus Mods.", matches[0].get("name", ""), matches[0].get("version", ""))
        best = min(live, key=lambda f: (_rank(f.get("scannedV2", "")), -int(f.get("fileId", 0))))
        scan = best.get("scannedV2", "")
        name, version = best.get("name", ""), best.get("version", "")
        if scan in CLEAN:
            return Verdict("clean", "", name, version)
        if scan in IN_PROGRESS:
            return Verdict("in_progress", f"Nexus Mods has not finished scanning this file yet (status {scan}).", name, version)
        return Verdict("refuse", _refusal_for(scan), name, version)
