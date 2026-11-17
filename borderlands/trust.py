"""Decide whether a program from a mod may be launched.  Pure policy, no network, no Qt.

Nexus Mods installs: the Nexus verdict decides (the VirusTotal count is not consulted).
Everything else: the VirusTotal verdict decides, and anything unverified asks once.
Hash approvals ("I patched this myself") are remembered so the question is not repeated.
"""

from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .nexus import Verdict
from .virustotal import Result

MAX_LABELS_SHOWN = 12


class Action(enum.Enum):
    RUN = "run"
    CONFIRM = "confirm"   # ask the user; they may override
    REFUSE = "refuse"     # no override


@dataclass(frozen=True)
class Decision:
    action: Action
    text: str
    remember: bool = False  # store the hash as approved once the user says yes
    rule: str = ""          # which rule produced this, for the log

class ApprovalStore:
    """SHA-256 values the user has approved, kept in a small JSON file."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = {k.lower(): v for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def is_approved(self, sha256: str) -> bool:
        return sha256.lower() in self._data

    def approve(self, sha256: str, mod: str, rel: str) -> None:
        self._data[sha256.lower()] = {"mod": mod, "path": rel, "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")


def _who(label: str, mod: str, sha256: str) -> str:
    return f"{label}\nFrom mod '{mod}'\nSHA-256 {sha256[:16]}..."


def _vt_lines(vt: Result) -> str:
    shown = [f"  {engine}: {label}" for engine, label in vt.labels[:MAX_LABELS_SHOWN]]
    more = len(vt.labels) - MAX_LABELS_SHOWN
    return "\n".join(shown) + (f"\n  ... and {more} more" if more > 0 else "")


def decide(*, label: str, mod: str, sha256: str, linked: bool, approved: bool,
           nexus: Verdict | None, vt: Result | None, vt_key_set: bool) -> Decision:
    who = _who(label, mod, sha256)
    already = "you approved this exact file before"

    if linked:
        verdict = nexus or Verdict("unavailable", "Nexus Mods could not be checked.")
        if verdict.kind == "clean":
            return Decision(Action.RUN, "", rule="Nexus Mods verified this exact file")
        if verdict.kind == "refuse":
            return Decision(Action.REFUSE, f"{who}\n\nRefused: {verdict.reason}",
                            rule="Nexus Mods refuses it, no override")
        if verdict.kind == "in_progress":
            return Decision(Action.CONFIRM, f"{who}\n\n{verdict.reason}\n\nRun it anyway?",
                            rule="Nexus Mods scan not finished, override allowed")
        if approved:  # mismatch or unavailable, but the user already accepted this exact file
            return Decision(Action.RUN, "", rule=already)
        if verdict.kind == "mismatch":
            return Decision(
                Action.CONFIRM,
                f"{who}\n\nThe file's hash does not match anything Nexus Mods published for this mod.\n"
                "If you changed this file yourself, that is expected. If you did not, do not run it.\n\n"
                "Run it anyway?",
                remember=True, rule="hash differs from every Nexus Mods file, override allowed",
            )
        return Decision(Action.CONFIRM, f"{who}\n\n{verdict.reason}\nIt could not be verified.\n\nRun it anyway?",
                        remember=True, rule="Nexus Mods could not be checked, override allowed")

    # Not installed from Nexus Mods.
    if vt is not None and vt.kind == "refuse":
        return Decision(Action.REFUSE, f"{who}\n\nRefused: VirusTotal says {vt.reason}.\n{_vt_lines(vt)}",
                        rule="VirusTotal refuses it, no override")
    if approved:
        return Decision(Action.RUN, "", rule=already)
    if vt is not None and vt.kind == "clean":
        return Decision(Action.RUN, "", rule="VirusTotal: no engine flags it")
    if vt is not None and vt.kind == "warn":
        return Decision(
            Action.CONFIRM,
            f"{who}\n\nThis program did not come from Nexus Mods. VirusTotal: {vt.reason}.\n"
            f"{_vt_lines(vt)}\n\nGeneric labels like these are often false alarms on packed or little-known "
            "programs, but they can also be real. Run it anyway?",
            remember=True, rule="VirusTotal: generic detections, override allowed",
        )
    if vt is not None:  # unknown to VirusTotal, or the lookup failed
        why = vt.reason
    elif not vt_key_set:
        why = "No VirusTotal API key is set, so it could not be checked."
    else:
        why = "It could not be checked on VirusTotal."
    return Decision(
        Action.CONFIRM,
        f"{who}\n\nThis program did not come from Nexus Mods. {why}\n\nRun it anyway?",
        remember=True, rule="not verifiable anywhere, override allowed",
    )
