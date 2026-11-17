"""Pure mod-layout rules: decide whether a mod's top level is valid or needs fixing.

No MO2 or Qt imports, so this is unit-testable outside the MO2 process.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Iterable

from .spec import GameSpec

# Documentation and pictures: a folder holding only these is not mod content, so it is left alone.
DOC_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".rtf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})

# Documentation by name, with or without an extension (README, INSTALL, "Read Me First.txt", ...).  Matched
# against the whole file name, ignoring case, so "setup.exe" or "start.bat" are not documentation.
DOC_NAME = re.compile(
    r"(change[ _-]?logs?|changes|history|what'?s[ _-]?new|install(ing|ation)?|where[ _-]?to[ _-]?in?stall|"
    r"instructions?|setup|start(up)?|"
    r"read[ _-]?me([ _-]?first)?|read|licen[cs]e|credits?|notes|faq|about)"
    r"(\.(txt|ini|md|rtf|pdf|html?|nfo|url))?",
    re.IGNORECASE,
)


def is_doc(name: str) -> bool:
    """Documentation or a picture: ignored when deciding whether a folder holds mod content."""
    return _suffix(name) in DOC_SUFFIXES or is_doc_name(name)


def is_doc_name(name: str) -> bool:
    """Named like documentation (README, INSTALL.txt, WHERE_TO_INSTALL, changelog ...), whatever it holds."""
    return DOC_NAME.fullmatch(name) is not None


class Verdict(enum.Enum):
    VALID = "valid"
    FIXABLE = "fixable"
    IGNORED = "ignored"
    INVALID = "invalid"


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].casefold() if dot > 0 else ""


def resolve_target(target: str, pak_subdir: str) -> str:
    """Fill ``{pak_subdir}`` and normalise to ``a/b/c/`` (always one trailing slash)."""
    parts = [p for p in target.replace("{pak_subdir}", pak_subdir.strip("/\\")).split("/") if p]
    return "/".join(parts) + "/"


def move_target(spec: GameSpec, name: str, is_dir: bool, pak_subdir: str) -> str | None:
    """Destination directory for a loose top-level file, or None if it stays put."""
    if is_dir:
        return None
    suffix = _suffix(name)
    for rule in spec.move_rules:
        if suffix in rule.suffixes:
            return resolve_target(rule.target, pak_subdir)
    return None


def classify(spec: GameSpec, name: str, is_dir: bool) -> Verdict:
    if is_dir:
        return Verdict.VALID if name.casefold() in spec.mod_dirs else Verdict.INVALID
    if move_target(spec, name, False, "") is not None:
        return Verdict.FIXABLE
    # Any other loose file (readme.txt, metadata.json, screenshots, ...) is packaging
    # clutter: it neither makes a mod valid nor invalid.
    return Verdict.IGNORED


def check(spec: GameSpec, entries: Iterable[tuple[str, bool]]) -> Verdict:
    """Overall verdict for a mod's top level; entries are ``(name, is_dir)`` pairs.

    An unknown *folder* makes the mod INVALID (the usual sign of a wrong layout, such
    as an extra wrapper folder); otherwise FIXABLE if anything needs moving, VALID if
    at least one entry is a recognised game folder.  Loose unknown files are ignored,
    but a mod made only of those is INVALID.
    """
    verdicts = {classify(spec, name, is_dir) for name, is_dir in entries}
    if Verdict.INVALID in verdicts:
        return Verdict.INVALID
    if Verdict.FIXABLE in verdicts:
        return Verdict.FIXABLE
    if Verdict.VALID in verdicts:
        return Verdict.VALID
    return Verdict.INVALID


# -- nested content ("Set as <game> directory" on a folder that holds mods) -------------


@dataclass(frozen=True)
class Node:
    """Duck-typed copy of an archive tree entry, so planning needs no MO2 objects."""

    name: str
    is_dir: bool
    children: tuple["Node", ...] = ()


@dataclass(frozen=True)
class Action:
    source: tuple[str, ...]  # path of the entry, relative to the chosen folder
    target: str              # directory (with a trailing slash) it is moved into
    rule: str                # NestedRule.name, for messages


@dataclass(frozen=True)
class NestedPlan:
    actions: tuple[Action, ...]
    source_dirs: tuple[str, ...]  # top-level folders to drop once they hold no files
    # Top-level entries that go into ``Tools/<mod name>/``: the mod has a program (.exe)
    # at its top level, so it is a tool and everything except the game folders is its own.
    tool_folder: tuple[str, ...] = ()

    def describe(self, folder: str = "") -> str:
        counts: dict[tuple[str, str], int] = {}
        for a in self.actions:
            counts[(a.rule, a.target)] = counts.get((a.rule, a.target), 0) + 1
        parts = [f"{n} {rule} item(s) into {target}" for (rule, target), n in counts.items()]
        if self.tool_folder:
            parts.append(f"{len(self.tool_folder)} item(s) into Tools/<mod name>/")
        who = f"'{folder}'" if folder else "The chosen folder"
        lead = "holds mod content in subfolders" if self.actions else "holds a program (.exe) at its top level"
        return f"{who} {lead}; placing " + ", ".join(parts) + "."


class TooLarge(Exception):
    pass


def nodes_from_tree(tree, limit: int = 20000) -> list[Node] | None:
    """Copy the children of an archive tree into ``Node``s; None if it is too big to scan."""
    budget = [limit]

    def walk(entry) -> Node:
        budget[0] -= 1
        if budget[0] < 0:
            raise TooLarge
        if not entry.isDir():
            return Node(entry.name(), False)
        return Node(entry.name(), True, tuple(walk(child) for child in entry))

    try:
        return [walk(child) for child in tree]
    except TooLarge:
        return None


def _files(node: Node, path: tuple[str, ...] = ()):
    """Yield ``(path, node)`` for every file under ``node`` (path includes node's name)."""
    here = path + (node.name,)
    if not node.is_dir:
        yield here, node
    else:
        for child in node.children:
            yield from _files(child, here)


def _suffix_files(node: Node, suffixes: tuple[str, ...]) -> list[tuple[str, ...]]:
    return [p for p, n in _files(node) if _suffix(n.name) in suffixes]


def _is_inert(node: Node) -> bool:
    """A folder with no files, or only documentation and pictures."""
    return all(is_doc(n.name) for _, n in _files(node))


def _rule_actions(node: Node, rule, pak_subdir: str) -> list[Action]:
    target = resolve_target(rule.target, pak_subdir)

    if rule.mode == "package":
        if "." in node.name:  # the SDK loader rejects folder names with a dot
            return []
        def has_init(n: Node) -> bool:
            return any(not c.is_dir and c.name.casefold() == "__init__.py" for c in n.children)
        if has_init(node):
            return [Action((node.name,), target, rule.name)]
        inner = next((c for c in node.children if c.is_dir and c.name.casefold() == node.name.casefold()), None)
        # Name/Name/__init__.py: unwrap the outer folder, but only if it holds nothing else
        if inner is not None and has_init(inner) and len(node.children) == 1:
            return [Action((node.name, inner.name), target, rule.name)]
        return []

    matches = _suffix_files(node, rule.suffixes)
    if not matches:
        return []

    if rule.mode == "folder":
        if rule.anchor and node.name.casefold() == rule.anchor.casefold():
            return [Action((node.name, child.name), target, rule.name) for child in node.children]
        return [Action((node.name,), target, rule.name)]

    if rule.mode == "flatten":
        # Only when the folder holds nothing but these files: anything else would be lost
        # or would end up stranded in the game directory.
        if any(_suffix(n.name) not in rule.suffixes for _, n in _files(node)):
            return []
        return [Action(path, target, rule.name) for path in matches]

    return []


def _node_at(top: Node, path: tuple[str, ...]) -> Node:
    node = top
    for name in path[1:]:
        node = next(c for c in node.children if c.name == name)
    return node


def plan_nested(spec: GameSpec, top: list[Node], pak_subdir: str) -> NestedPlan | None:
    """Work out how to place content found in subfolders of a user-chosen folder.

    None means "do not guess"; ``plan_nested_why`` says why.  Loose top-level files are
    left to the ordinary move rules, and normal game folders (``OakGame`` ...) stay put.
    """
    return plan_nested_why(spec, top, pak_subdir)[0]


def plan_nested_why(spec: GameSpec, top: list[Node], pak_subdir: str) -> tuple[NestedPlan | None, str]:
    """``(plan, "")``, or ``(None, reason)`` when the content is not safe to place.

    Refused: a folder nobody recognises, a folder two rules claim, two things that would
    land on the same path, and sibling folders that hold identically named paks (those
    are alternative variants of one mod, and mounting all of them would make only the
    last one count).  ``reason`` is empty when the folder simply holds nothing we know.
    """
    if not spec.nested_rules:
        return None, ""
    actions: list[Action] = []
    sources: list[str] = []

    # A program (.exe) at the top level: the whole mod is a tool.  It wins over everything
    # else, including pak or .sdkmod files, which then belong to the tool (templates, data).
    exclusive_rules = [r for r in spec.nested_rules if r.exclusive]
    exe_suffixes = tuple(s for r in exclusive_rules for s in r.suffixes)
    if any(not n.is_dir and _suffix(n.name) in exe_suffixes for n in top):
        entries = tuple(n.name for n in top if n.name.casefold() not in spec.mod_dirs)
        return NestedPlan((), (), entries), ""

    variant_rules = {r.name for r in spec.nested_rules if r.variants}
    pak_suffixes = tuple(s for r in spec.nested_rules if r.variants for s in r.suffixes)
    seen: dict[str, tuple[str, ...]] = {}  # file name -> the folder that holds it
    inert: list[str] = []  # folders of documentation or pictures, left where they are
    for node in top:
        if not node.is_dir or node.name.casefold() in spec.mod_dirs:
            continue
        # A folder holding a program is a tool, whatever else it holds.  Otherwise a folder
        # holding more than one kind of content (paks and .sdkmods) belongs in different
        # places, so it cannot be handled as a unit.
        owner = next((r for r in exclusive_rules if _suffix_files(node, r.suffixes)), None)
        if owner is not None:
            claims = [a for a in [_rule_actions(node, owner, pak_subdir)] if a]
            kinds = [owner]
        else:
            kinds = [r for r in spec.nested_rules if r.suffixes and _suffix_files(node, r.suffixes)]
            claims = [a for a in (_rule_actions(node, r, pak_subdir) for r in spec.nested_rules) if a]
        if len(kinds) > 1 or len(claims) != 1:  # nothing recognised, or ambiguous
            if not kinds and not claims and _is_inert(node):
                inert.append(node.name)
                continue
            return None, ""
        for action in claims[0]:
            if action.rule not in variant_rules:
                continue
            for path in _suffix_files(_node_at(node, action.source), pak_suffixes):
                folder = action.source[:-1] + path[:-1]  # the directory that holds this file
                other = seen.setdefault(path[-1].casefold(), folder)
                if other != folder:
                    return None, (f"'{path[-1]}' exists in both '{other[-1]}' and '{folder[-1]}'. "
                                  "Identical pak names in different folders are usually alternative "
                                  "variants of one mod; choose the one folder you want instead.")
        actions.extend(claims[0])
        sources.append(node.name)
    if not actions:
        # Nothing to move.  Documentation folders beside the game's own folders are fine as they are.
        known = any(n.is_dir and n.name.casefold() in spec.mod_dirs for n in top)
        return (NestedPlan((), (), ()), "") if inert and known else (None, "")
    destinations = [(a.target + a.source[-1]).casefold() for a in actions]
    if len(set(destinations)) != len(destinations):
        return None, ""
    return NestedPlan(tuple(actions), tuple(sources)), ""
