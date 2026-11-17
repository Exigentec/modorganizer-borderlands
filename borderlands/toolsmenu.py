"""MO2 tool plugin: a Tools menu entry that runs programs shipped by mods under ``Tools/``.

The program is started through MO2's virtual file system from its place in the game
folder, so it sees every active mod.  Before it starts, the plugin hashes it and checks it:
Nexus Mods installs against what Nexus published and scanned, everything else on
VirusTotal.  The policy itself lives in :mod:`trust`; this module only talks to MO2 and Qt.
"""

from __future__ import annotations

from pathlib import Path

import mobase
from PyQt6.QtCore import Qt, qInfo, qWarning
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox

from . import peinfo, tools, trust, tweaks, virustotal
from .nexus import NexusClient
from .spec import ALL_SPECS, GameSpec

KEY_SETTING = "virustotal_api_key"
DISMISS_SETTING = "virustotal_prompt_dismissed"
TITLE = "Borderlands tools"


def _truthy(value) -> bool:
    return value is True or str(value).strip().casefold() in ("1", "true", "yes")


def _nexus_text(v) -> str:
    text = v.kind + (f" - {v.reason}" if v.reason else "")
    return text + (f" (file '{v.file_name}' {v.file_version})" if v.file_name else "")


def _vt_text(r) -> str:
    text = r.kind + (f" - {r.reason}" if r.reason else "")
    if r.labels:
        shown = "; ".join(f"{engine}: {label}" for engine, label in r.labels[:12])
        text += f" [{shown}" + (f"; and {len(r.labels) - 12} more" if len(r.labels) > 12 else "") + "]"
    return text


class BorderlandsTools(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self._organizer: mobase.IOrganizer
        self._parent = None
        self._nexus = NexusClient()
        self._vt: virustotal.VirusTotalClient | None = None
        self._vt_key = ""
        self._make_vt = virustotal.VirusTotalClient  # replaced in tests
        self._menu = None            # MO2's Tools menu, once found
        self._menu_actions: list = []  # the per-tool entries we added to it

    # -- plugin identity ---------------------------------------------------------

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        organizer.onUserInterfaceInitialized(self._on_ui)
        return True

    def name(self) -> str:
        return "Borderlands Tools"

    def localizedName(self) -> str:
        return "Borderlands Tools"

    def author(self) -> str:
        return "Borderlands MO2 plugin contributors"

    def description(self) -> str:
        return "Runs programs that mods ship under Tools/, after checking them against Nexus Mods or VirusTotal."

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo("0.1.0")

    def requirements(self):
        return [mobase.PluginRequirementFactory.gameDependency({spec.game_name for spec in ALL_SPECS})]

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting(
                KEY_SETTING,
                "Your free VirusTotal API key (https://www.virustotal.com/gui/join-us). Used to check "
                "programs that did not come from Nexus Mods. Stored as plain text in ModOrganizer.ini.",
                "",
            ),
            mobase.PluginSetting(
                DISMISS_SETTING,
                "Do not ask about the VirusTotal API key at startup.",
                False,
            ),
            *(mobase.PluginSetting(t.setting, t.description, True) for t in tweaks.TWEAKS),
        ]

    def displayName(self) -> str:
        return "Borderlands: Choose a tool..."

    def tooltip(self) -> str:
        return "Run a program that an installed mod ships under Tools/, after checking it."

    def icon(self) -> QIcon:
        return QIcon()

    def setParentWidget(self, widget) -> None:
        self._parent = widget

    # -- helpers -------------------------------------------------------------------

    def _spec(self) -> GameSpec | None:
        managed = self._organizer.managedGame()
        if not managed:
            return None
        return next((s for s in ALL_SPECS if s.plugin_name == managed.name()), None)

    def _setting(self, key: str):
        try:
            return self._organizer.pluginSetting(self.name(), key)
        except Exception:
            return None

    def _active_mods(self) -> list[tuple[str, Path]]:
        """Active mods, lowest priority first (the last one wins, as in the VFS)."""
        mod_list = self._organizer.modList()
        mods = []
        for name in mod_list.allModsByProfilePriority():
            if not mod_list.state(name) & mobase.ModState.ACTIVE:
                continue
            mod = mod_list.getMod(name)
            if mod is not None:
                mods.append((name, Path(mod.absolutePath())))
        return mods

    def _approvals(self) -> trust.ApprovalStore:
        path = Path(self._organizer.basePath()) / "plugin_state" / "borderlands" / "approved_tools.json"
        return trust.ApprovalStore(path)

    def _key(self) -> str:
        key = self._setting(KEY_SETTING)
        return key.strip() if isinstance(key, str) else ""

    def _vt_client(self):
        key = self._key()
        if not key:
            return None
        if self._vt is None or key != self._vt_key:
            self._vt, self._vt_key = self._make_vt(key), key
        return self._vt

    # -- startup prompt ------------------------------------------------------------

    def _on_ui(self, window) -> None:
        if self._spec() is not None:
            self._attach_menu(window)
        try:
            if self._spec() is None or self._key() or _truthy(self._setting(DISMISS_SETTING)):
                return
            self._ask_for_key(window)
        except Exception as err:  # never let a prompt break MO2's startup
            qWarning(f"Borderlands: could not show the VirusTotal key prompt: {err}")

    # -- one menu entry per tool ---------------------------------------------------

    def _attach_menu(self, window) -> None:
        """Add a "Borderlands: <tool>" entry to MO2's Tools menu for every program found.

        MO2 rebuilds that menu from scratch each time it opens (on ``aboutToShow``) and a plugin
        can only register fixed entries, so we connect to the same signal.  Qt runs slots in the
        order they were connected, so ours runs after MO2's rebuild and always sees the current
        mods: a disabled or removed mod's tools simply do not get an entry.
        """
        try:
            find = getattr(window, "findChild", None)
            action = find(QAction, "actionTool") if find else None
            menu = action.menu() if action is not None else None
            if menu is None:
                if find:
                    qWarning("Borderlands: could not find MO2's Tools menu, so tools have no entries of their own; "
                             "use Tools > Borderlands: Choose a tool...")
                return
            self._parent = window
            if self._menu is None:
                self._menu = menu
                menu.aboutToShow.connect(self._refresh_menu)
        except Exception as err:
            qWarning(f"Borderlands: could not add tool entries to the Tools menu: {err}")

    def _refresh_menu(self) -> None:
        menu = self._menu
        try:
            # MO2's rebuild (menu.clear()) normally removed, and Qt deleted, our old entries; touching
            # a deleted one would raise, so only remove those that are still in the menu.
            present = menu.actions()
            for old in self._menu_actions:
                if old in present:
                    menu.removeAction(old)
            self._menu_actions = []
            if self._spec() is None:
                return
            found = tools.discover(self._active_mods())
            if not found:
                return
            labels = peinfo.entry_names([(peinfo.display_name(t.real), t.mod) for t in found])
            self._menu_actions.append(menu.addSeparator())
            for tool, label in zip(found, labels):
                entry = QAction("Borderlands: " + label.replace("&", "&&"), menu)
                entry.setToolTip(f"{tool.rel}  ({tool.mod})")
                entry.triggered.connect(lambda checked=False, t=tool: self._entry_triggered(t))
                menu.addAction(entry)
                self._menu_actions.append(entry)
        except Exception as err:  # a broken entry list must never break MO2's menu
            qWarning(f"Borderlands: could not list tools in the Tools menu: {err}")

    def _entry_triggered(self, tool: tools.Tool) -> None:
        QTimer.singleShot(0, lambda: self._run_entry(tool))  # after the menu has closed, as MO2 does

    def _run_entry(self, tool: tools.Tool) -> None:
        try:
            spec = self._spec()
            if spec is not None:
                self._run(tool, spec)
        except Exception as err:
            qWarning(f"Borderlands: could not run {tool.rel}: {err}")
            QMessageBox.critical(self._parent, TITLE, f"Something went wrong: {err}")

    def _ask_for_key(self, parent) -> None:
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(TITLE)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "<p><b>Set up a VirusTotal key?</b></p>"
            "<p>Borderlands tools can check programs that did not come from Nexus Mods on VirusTotal "
            "before running them. That needs your own free API key (personal use, 500 lookups a day).</p>"
            f"<p>1. <a href=\"{virustotal.SIGN_UP_URL}\">Create a free VirusTotal account</a><br>"
            f"2. Copy your key from <a href=\"{virustotal.KEY_PAGE_URL}\">your account's API key page</a><br>"
            "3. Paste it into <b>Settings &rarr; Plugins &rarr; Borderlands Tools &rarr; "
            f"{KEY_SETTING}</b></p>"
            "<p>Without a key, such programs ask for your confirmation the first time instead.</p>"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        ok = box.button(QMessageBox.StandardButton.Ok)
        cancel = box.button(QMessageBox.StandardButton.Cancel)
        ok.setText("Remind me next time")
        cancel.setText("Don't ask again")
        box.setEscapeButton(ok)  # closing the window must not silence the reminder
        box.exec()
        if box.clickedButton() is cancel:
            self._organizer.setPluginSetting(self.name(), DISMISS_SETTING, True)

    # -- running a tool ------------------------------------------------------------

    def display(self) -> None:
        try:
            self._display()
        except Exception as err:
            qWarning(f"Borderlands: the tools menu failed: {err}")
            QMessageBox.critical(self._parent, TITLE, f"Something went wrong: {err}")

    def _display(self) -> None:
        spec = self._spec()
        if spec is None:
            QMessageBox.information(self._parent, TITLE, "No Borderlands game is being managed in this instance.")
            return
        found = tools.discover(self._active_mods())
        if not found:
            QMessageBox.information(
                self._parent, TITLE,
                "No programs found. Install a mod that ships a Tools/ folder, and make sure it is enabled: "
                "programs from disabled mods are not listed.",
            )
            return
        labels = [f"{t.label}    ({t.mod})" for t in found]
        choice, ok = QInputDialog.getItem(self._parent, TITLE, "Run:", labels, 0, False)
        if ok and choice in labels:
            self._run(found[labels.index(choice)], spec)

    def _log(self, message: str) -> None:
        qInfo(f"Borderlands: {message}")

    def _run(self, tool: tools.Tool, spec: GameSpec) -> None:
        try:
            sha256 = tools.sha256_of(tool.real)
        except OSError as err:
            QMessageBox.critical(self._parent, TITLE, f"Could not read {tool.label}: {err}")
            return

        link = tools.read_nexus_link(tool.mod_path)
        linked = link is not None and link.game_name == spec.nexus_name.casefold()
        store = self._approvals()
        approved = store.is_approved(sha256)
        self._log(f"checking {tool.rel} from '{tool.mod}' (SHA-256 {sha256[:16]}...)")
        self._log("  source: " + (f"Nexus Mods, mod {link.mod_id}" if linked
                                  else "not from Nexus Mods (no matching mod id in the mod's meta.ini)"))
        nexus = vt = None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if linked:
                nexus = self._nexus.check(sha256, spec.nexus_id, link.mod_id)
            else:
                client = self._vt_client()
                vt = client.lookup(sha256) if client else None
        finally:
            QApplication.restoreOverrideCursor()

        if linked:
            self._log("  Nexus Mods ruling: " + _nexus_text(nexus))
            self._log("  VirusTotal: not consulted (the program is from Nexus Mods)")
        elif vt is not None:
            self._log("  VirusTotal ruling: " + _vt_text(vt))
        else:
            self._log("  VirusTotal: not consulted (no API key set)")
        self._log("  approved by you before: " + ("yes" if approved else "no"))

        decision = trust.decide(
            label=tool.label, mod=tool.mod, sha256=sha256, linked=linked,
            approved=approved, nexus=nexus, vt=vt, vt_key_set=bool(self._key()),
        )
        self._log(f"  decision: {decision.action.value} ({decision.rule})")
        if decision.action is trust.Action.REFUSE:
            qWarning(f"Borderlands: refused to run {tool.rel} from '{tool.mod}'.")
            QMessageBox.critical(self._parent, TITLE, decision.text)
            return
        if decision.action is trust.Action.CONFIRM:
            answer = QMessageBox.warning(
                self._parent, TITLE, decision.text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            yes = answer == QMessageBox.StandardButton.Yes
            self._log("  you answered: " + ("yes, run it" if yes else "no"))
            if not yes:
                return
            if decision.remember:
                store.approve(sha256, tool.mod, tool.rel)
                self._log("  remembered this exact file as approved")
        self._start(tool, spec)

    def _tweak_for(self, tool: tools.Tool) -> tweaks.ToolTweak | None:
        """The enabled tweak for this program, if any (logging why not)."""
        tweak = tweaks.match(tools.read_nexus_link(tool.mod_path), tool.real.name)
        if tweak is None:
            return None
        value = self._setting(tweak.setting)
        if value is not None and not _truthy(value):
            self._log(f"tweak for {tweak.title} is switched off ({tweak.setting})")
            return None
        self._log(f"tweak for {tweak.title} is on ({tweak.setting})")
        return tweak

    def _start(self, tool: tools.Tool, spec: GameSpec) -> None:
        # Started from the mod's real folder: MO2 cannot launch a file that exists only in the
        # virtual game folder ("file does not exist").  The process is still hooked into the
        # virtual file system, so it sees every active mod.
        tweak = self._tweak_for(tool)
        handle = self._organizer.startApplication(str(tool.real), [], str(tool.real.parent))
        qInfo(f"Borderlands: started {tool.rel} from '{tool.mod}'.")
        if tweak is not None and tweak.built_file:
            self._after_run(tool, tweak, spec, handle)

    def _after_run(self, tool: tools.Tool, tweak: tweaks.ToolTweak, spec: GameSpec, handle) -> None:
        """Wait for the program to exit, then offer to move what it built into the game folder."""
        try:
            self._log(f"waiting for {tweak.title} to exit")
            # MO2 shows its usual lock screen meanwhile.  refresh=False matters: by default MO2 starts a
            # refresh when the program exits and then waits in a nested event loop until it reports
            # completion, which can hang until some unrelated UI action triggers another refresh.
            self._organizer.waitForApplication(handle, False)
            self._log(f"{tweak.title} has exited")
            built = tweaks.find_built(tool.mod_path, tweak)
            if not built:
                self._log(f"{tweak.title} exited; no file to place ({tweak.built_file})")
                return
            subdir = self._organizer.pluginSetting(spec.plugin_name, "pak_subdir")
            dest_dir = tweaks.destination(tool.mod_path, tweak, subdir if isinstance(subdir, str) else "")
            shown = dest_dir.relative_to(tool.mod_path).as_posix()
            replacing = [p.name for p in built if (dest_dir / p.name).exists()]
            answer = QMessageBox.question(
                self._parent, TITLE,
                tweaks.prompt_text(tweak, [p.name for p in built], shown, replacing),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._log(f"you chose not to move {', '.join(p.name for p in built)}; left in {built[0].parent.name}")
                return
            for path in built:
                dest, replaced = tweaks.move(path, dest_dir)
                self._log(f"moved {path.name} to {shown}" + (" (replaced the previous one)" if replaced else ""))
            self._refresh_mod(tool.mod)
        except Exception as err:  # the tool already ran; never turn a tidy-up failure into a crash
            qWarning(f"Borderlands: could not place what {tweak.title} built: {err}")
            QMessageBox.critical(self._parent, TITLE, f"Could not move the file {tweak.title} built: {err}")

    def _refresh_mod(self, name: str) -> None:
        try:
            mod = self._organizer.modList().getMod(name)
            if mod is not None:
                self._organizer.modDataChanged(mod)
        except Exception:
            pass  # only a refresh of MO2's view; the files are already in place
