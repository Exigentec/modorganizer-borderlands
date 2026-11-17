"""Look a program's SHA-256 up on VirusTotal (public API v3, needs the user's own free key).

Advisory only: VirusTotal's terms say the public API is not a substitute for antivirus
software, and it is for personal, non-commercial use (500 requests/day, 4 per minute).
Nothing is ever uploaded; a hash VirusTotal has never seen simply comes back as unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import netio

URL = "https://www.virustotal.com/api/v3/files/"
SIGN_UP_URL = "https://www.virustotal.com/gui/join-us"
KEY_PAGE_URL = "https://www.virustotal.com/gui/my-apikey"

REFUSE_AT = 10  # engines flagging it: this many or more means "do not run"

# Engines whose label for a real, named malware family we trust enough to refuse on one.
MAJOR_ENGINES = frozenset({
    "Microsoft", "Kaspersky", "ESET-NOD32", "BitDefender", "Symantec", "Sophos", "Avast", "AVG",
    "Malwarebytes", "TrendMicro", "F-Secure", "Fortinet", "GData", "DrWeb", "Emsisoft",
})

# Labels that say "looks suspicious" rather than naming a family: heuristics, machine
# learning, generic signatures (checked against the labels seen on a real, benign packed exe).
_GENERIC = re.compile(
    "|".join([
        r"generic", r"heur", r"(?<![a-z])ml(?![a-z])", r"machine", r"susgen", r"suspicious",
        r"unsafe", r"confidence", r"real ?protect", r"\bkb\b", r"^malicious\b", r"malware\.",
        r"\bgen\b", r"[:.!/-]gen\b", r"undefined", r"\bagent\b", r"score", r"packed",
        r"riskware", r"\bpu[ap]\b", r"unwanted", r"obfus", r"behav", r"\bai\b", r"\bevo\b",
    ]),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Result:
    """``kind``: clean | warn | refuse | unknown | error."""

    kind: str
    flagged: int = 0
    labels: tuple[tuple[str, str], ...] = ()  # (engine, label) for every flagging engine
    reason: str = ""


def is_named_family(engine: str, label: str) -> bool:
    return engine in MAJOR_ENGINES and not _GENERIC.search(label or "")


def classify(payload) -> Result:
    """Turn a v3 ``/files/<hash>`` answer into a verdict."""
    try:
        results = payload["data"]["attributes"]["last_analysis_results"]
    except (KeyError, TypeError):
        return Result("error", reason="VirusTotal sent an answer this plugin does not understand.")
    labels = tuple(sorted(
        (engine, str(r.get("result") or r.get("category")))
        for engine, r in results.items() if r.get("category") in ("malicious", "suspicious")
    ))
    if not labels:
        return Result("clean")
    named = [(e, l) for e, l in labels if is_named_family(e, l)]
    if len(labels) >= REFUSE_AT or named:
        why = (f"{len(labels)} engines flag it" if len(labels) >= REFUSE_AT
               else f"{named[0][0]} names a malware family: {named[0][1]}")
        return Result("refuse", len(labels), labels, why)
    return Result("warn", len(labels), labels, f"{len(labels)} engine(s) flag it with generic labels")


class VirusTotalClient:
    def __init__(self, api_key: str, fetch=netio.request_json):
        self._key = api_key.strip()
        self._fetch = fetch
        self._cache: dict[str, Result] = {}

    def lookup(self, sha256: str) -> Result:
        sha256 = sha256.lower()
        if sha256 in self._cache:
            return self._cache[sha256]
        try:
            status, payload = self._fetch(URL + sha256, headers={"x-apikey": self._key})
        except netio.NetworkError as err:
            return Result("error", reason=f"Could not reach VirusTotal ({err}).")  # not cached: may be a blip
        if status == 200:
            result = classify(payload)
        elif status == 404:
            result = Result("unknown", reason="VirusTotal has never seen this file.")
        elif status in (401, 403):
            return Result("error", reason="VirusTotal rejected the API key.")
        elif status == 429:
            return Result("error", reason="VirusTotal's request limit was reached; try again in a minute.")
        else:
            return Result("error", reason=f"VirusTotal answered HTTP {status}.")
        self._cache[sha256] = result
        return result
