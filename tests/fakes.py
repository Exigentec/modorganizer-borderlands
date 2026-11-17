"""Minimal stand-ins for ``mobase`` and ``PyQt6`` so the glue module imports outside MO2.

They only cover what ``borderlands/game.py`` touches.  They cannot prove the real MO2
API matches; they catch typos, wrong arity and logic errors in our own code.
"""

from __future__ import annotations

import sys
import types


LOG: list[tuple[str, str]] = []  # (level, message) sent through qInfo/qWarning/qCritical
DIALOGS: list[tuple] = []        # (kind, title, text) for every Qt dialog the plugin showed
PROMPTS: list = []               # the QMessageBox instances that were exec()'d
CURSOR: list[str] = []           # "set" / "restore" calls on the override cursor
PROGRESS: list = []              # the progress dialogs that were opened
APP_DIR = ["C:/fake/MO2"]        # what QCoreApplication.applicationDirPath() says
QUIT: list[int] = []             # QApplication.quit() calls
UI = {"confirm": "Yes", "prompt": "ok", "item": 0, "choices": []}  # scripted answers


def reset_ui():
    DIALOGS.clear(), PROMPTS.clear(), CURSOR.clear(), PROGRESS.clear()
    UI.update({"confirm": "Yes", "prompt": "ok", "item": 0, "choices": []})
    if "QProgressDialog" in globals():
        globals()["QProgressDialog"].cancel_after = None


def install() -> types.ModuleType:
    if "mobase" in sys.modules and hasattr(sys.modules["mobase"], "_IS_FAKE"):
        return sys.modules["mobase"]

    mobase = types.ModuleType("mobase")
    mobase._IS_FAKE = True

    class IPluginGame:
        def __init__(self):
            pass

    class ISaveGame:
        def __init__(self):
            pass

    class ModDataChecker:
        INVALID, FIXABLE, VALID = "INVALID", "FIXABLE", "VALID"
        CheckReturn = str

        def __init__(self):
            pass

    class IFileTree:
        pass

    class VersionInfo:
        def __init__(self, text=""):
            self.text = text

        def isValid(self):
            return bool(self.text)

    class PluginSetting:
        def __init__(self, key, description, default):
            self.key, self.description, self.default = key, description, default

    class ExecutableInfo:
        def __init__(self, title, info):
            self.title, self.info = title, info

    class ExecutableForcedLoadSetting:
        pass

    class ProfileSetting:
        CONFIGURATION = 1

    class IPluginFileMapper:
        def __init__(self):
            pass

    class Mapping:
        def __init__(self, source, destination, is_directory, create_target=False):
            self.source, self.destination = source, destination
            self.is_directory, self.create_target = is_directory, create_target

    class GuessedString:
        def __init__(self, value=""):
            self.value = value

    class ModState:
        ACTIVE = 2

    class IPluginTool:
        def __init__(self):
            pass

    class IPluginDiagnose:
        def __init__(self):
            self.invalidations = 0

        def _invalidate(self):
            self.invalidations += 1

    class ModDataContent:
        class Content:
            def __init__(self, id, name, icon, filter_only=False):
                self.id, self.name, self.icon, self.filter_only = id, name, icon, filter_only

        def __init__(self):
            pass

    class PluginRequirementFactory:
        @staticmethod
        def gameDependency(names):
            return ("game", frozenset(names))

    for cls in (IPluginGame, ISaveGame, ModDataChecker, IFileTree, VersionInfo, PluginSetting,
                ExecutableInfo, ExecutableForcedLoadSetting, ProfileSetting,
                IPluginFileMapper, Mapping, GuessedString, ModState, IPluginTool,
                PluginRequirementFactory, IPluginDiagnose, ModDataContent):
        setattr(mobase, cls.__name__, cls)
    mobase.IOrganizer = object
    mobase.getFileVersion = lambda path: ""
    mobase.getIconForExecutable = lambda path: None
    sys.modules["mobase"] = mobase

    qt = types.ModuleType("PyQt6")
    core = types.ModuleType("PyQt6.QtCore")
    gui = types.ModuleType("PyQt6.QtGui")

    class QDir:
        def __init__(self, path=""):
            self._path = str(path).replace("\\", "/")

        def absoluteFilePath(self, name):
            return f"{self._path}/{name}"

        def absolutePath(self):
            return self._path

        def exists(self, name):
            return False  # the fake never touches the filesystem

    class QFileInfo:
        def __init__(self, path):
            self._path = str(path).replace("\\", "/")

        def fileName(self):
            return self._path.rsplit("/", 1)[-1]

    class QDateTime:
        @staticmethod
        def fromSecsSinceEpoch(secs):
            return secs

    class QStandardPaths:
        class StandardLocation:
            DocumentsLocation = 0

        @staticmethod
        def writableLocation(_loc):
            return "C:/Users/test/Documents"

    class _Signal:
        def __init__(self):
            self._slots = []

        def connect(self, fn):
            self._slots.append(fn)

        def emit(self, *args):
            for fn in list(self._slots):
                fn(*args)

    class QAction:
        def __init__(self, text="", parent=None):
            self._text, self._menu = text, None
            self.triggered, self.tooltip, self.separator = _Signal(), "", False

        def text(self):
            return self._text

        def setToolTip(self, text):
            self.tooltip = text

        def menu(self):
            return self._menu

    class QMenu:
        """MO2's Tools menu: ``show()`` emits aboutToShow, which is when MO2 rebuilds it."""

        def __init__(self):
            self.aboutToShow, self._actions = _Signal(), []

        def addAction(self, action):
            self._actions.append(action)

        def addSeparator(self):
            action = QAction("-")
            action.separator = True
            self._actions.append(action)
            return action

        def removeAction(self, action):
            if action in self._actions:  # like Qt: removing an absent action is a no-op
                self._actions.remove(action)

        def clear(self):
            self._actions.clear()

        def actions(self):
            return list(self._actions)

        def show(self):
            self.aboutToShow.emit()

    class QCoreApplication:
        @staticmethod
        def applicationDirPath():
            return APP_DIR[0]

        @staticmethod
        def applicationFilePath():
            return APP_DIR[0] + "/ModOrganizer.exe"

    class QThread:
        level = [1]  # the event loop nesting of the "current thread": 0 before exec(), 1 in MO2's main loop

        class _Current:
            @staticmethod
            def loopLevel():
                return QThread.level[0]

        @staticmethod
        def currentThread():
            return QThread._Current()

    class QTimer:
        queue = None  # a list while a test wants timers held back until the "event loop" runs them

        @staticmethod
        def singleShot(msec, fn):
            if QTimer.queue is None:
                fn()  # the fake has no event loop: run it at once
            else:
                QTimer.queue.append(fn)

        @staticmethod
        def run_queued():
            while QTimer.queue:
                QTimer.queue.pop(0)()

    class QObject:
        def __init__(self, *args):
            pass

    class pyqtSignal:
        """A per-instance signal, delivered at once (the fake has no threads of its own)."""

        def __init__(self, *types):
            self._name = None

        def __set_name__(self, owner, name):
            self._name = "_signal_" + name

        def __get__(self, instance, owner):
            if instance is None:
                return self
            if self._name not in instance.__dict__:
                instance.__dict__[self._name] = _Signal()
            return instance.__dict__[self._name]

    class Qt:
        class Orientation:
            Horizontal = "horizontal"

        class WindowModality:
            WindowModal = "window"

        class CursorShape:
            WaitCursor = "wait"

        class TextFormat:
            RichText = "rich"

    core.QDir, core.QFileInfo, core.QDateTime, core.QStandardPaths = QDir, QFileInfo, QDateTime, QStandardPaths
    core.Qt = Qt
    core.QObject = QObject
    core.pyqtSignal = pyqtSignal
    core.QTimer = QTimer
    core.QThread = QThread
    globals()["QThread"] = QThread  # exposed as fakes.QThread so tests can set the loop level
    core.QCoreApplication = QCoreApplication
    globals()["QTimer"] = QTimer  # exposed as fakes.QTimer so tests can hold timers back
    # Messages the plugin sends to MO2's log pane (qInfo/qWarning/qCritical).
    core.qInfo = lambda msg: LOG.append(("info", msg))
    core.qWarning = lambda msg: LOG.append(("warning", msg))
    core.qCritical = lambda msg: LOG.append(("critical", msg))
    gui.QIcon = object
    gui.QAction = QAction
    widgets_menu = QMenu  # exposed to the tests through fakes.QMenu
    globals()["QMenu"] = widgets_menu
    globals()["QAction"] = QAction

    widgets = types.ModuleType("PyQt6.QtWidgets")

    class _Btn(str):
        """A button constant; like Qt's flags it can be combined with ``|``."""

        def __or__(self, other):
            return _Btn(f"{self}|{other}")

    class _Buttons:
        Yes, No, Ok, Cancel = _Btn("Yes"), _Btn("No"), _Btn("Ok"), _Btn("Cancel")

    class _Button:
        def __init__(self, name):
            self.name, self.text = name, name

        def setText(self, text):
            self.text = text

    class QMessageBox:
        """Static dialogs are answered from ``UI`` and recorded in ``DIALOGS``."""

        class StandardButton(_Buttons):
            pass

        class Icon:
            Information = "info"
            Question = "question"

        class ButtonRole:
            AcceptRole, RejectRole, ActionRole = "accept", "reject", "action"

        @staticmethod
        def _record(kind, title, text):
            DIALOGS.append((kind, title, text))

        @staticmethod
        def critical(parent, title, text):
            DIALOGS.append(("critical", title, text))

        @staticmethod
        def question(parent, title, text, buttons=None, default=None):
            DIALOGS.append(("question", title, text))
            return UI["confirm"]

        @staticmethod
        def information(parent, title, text):
            DIALOGS.append(("information", title, text))

        @staticmethod
        def warning(parent, title, text, buttons=None, default=None):
            DIALOGS.append(("warning", title, text))
            return UI["confirm"]

        def __init__(self, parent=None):
            self.rich, self.text_value, self._buttons, self._escape = False, "", {}, None
            self.clicked, self._added, self._default = None, [], None

        def setIcon(self, icon):
            pass

        def addButton(self, text, role):
            button = _Button(text)
            self._added.append(button)
            return button

        def setDefaultButton(self, button):
            self._default = button

        def setWindowTitle(self, title):
            self.title = title

        def setTextFormat(self, fmt):
            self.rich = fmt == Qt.TextFormat.RichText

        def setText(self, text):
            self.text_value = text

        def setStandardButtons(self, buttons):
            self._buttons = {"Ok": _Button("Ok"), "Cancel": _Button("Cancel")}

        def button(self, which):
            return self._buttons[which]

        def setEscapeButton(self, button):
            self._escape = button

        def exec(self):
            if self._added:  # a question with custom buttons: answered from UI["choices"], else the default
                labels = [b.text for b in self._added]
                DIALOGS.append(("ask", getattr(self, "title", ""), self.text_value, labels))
                wanted = UI["choices"].pop(0) if UI["choices"] else None
                self.clicked = next((b for b in self._added if b.text == wanted), self._default or self._added[0])
                return
            DIALOGS.append(("prompt", getattr(self, "title", ""), self.text_value))
            answer = UI["prompt"]  # "ok", "cancel" or "close" (window closed: escape button)
            self.clicked = self._escape if answer == "close" else self._buttons["Ok" if answer == "ok" else "Cancel"]
            self.texts = {k: b.text for k, b in self._buttons.items()}
            PROMPTS.append(self)

        def clickedButton(self):
            return self.clicked

    class QInputDialog:
        @staticmethod
        def getItem(parent, title, label, items, current=0, editable=False):
            DIALOGS.append(("choose", title, list(items)))
            pick = UI["item"]
            return (items[pick], True) if pick is not None else ("", False)

    class QProgressDialog:
        """Records its label and values; ``cancel_after`` cancels once that many updates have been made."""

        cancel_after = None

        def __init__(self, label, cancel, minimum, maximum, parent=None):
            self.label, self.values, self._closed = label, [], False
            PROGRESS.append(self)

        def setWindowTitle(self, title):
            pass

        def setWindowModality(self, modality):
            pass

        def setMinimumDuration(self, ms):
            pass

        def setAutoClose(self, flag):
            pass

        def setAutoReset(self, flag):
            pass

        def setValue(self, value):
            self.values.append(value)

        def wasCanceled(self):
            limit = QProgressDialog.cancel_after
            return limit is not None and len(self.values) > limit

        def close(self):
            self._closed = True

    class QApplication:
        @staticmethod
        def processEvents():
            pass

        @staticmethod
        def quit():
            QUIT.append(1)

        @staticmethod
        def setOverrideCursor(cursor):
            CURSOR.append("set")

        @staticmethod
        def restoreOverrideCursor():
            CURSOR.append("restore")

        @staticmethod
        def activeWindow():
            return None

    widgets.QMessageBox, widgets.QInputDialog, widgets.QApplication = QMessageBox, QInputDialog, QApplication
    widgets.QProgressDialog = QProgressDialog
    widgets.QTreeView = type("QTreeView", (), {})
    globals()["QProgressDialog"] = QProgressDialog
    sys.modules.update({"PyQt6": qt, "PyQt6.QtCore": core, "PyQt6.QtGui": gui, "PyQt6.QtWidgets": widgets})
    return mobase


class FakeMod:
    """Stand-in for ``mobase.IModInterface`` that records what the plugin sets."""

    def __init__(self, path, version="", url="", name="fake mod"):
        self._path, self._version, self._url, self._name = str(path), version, url, name
        self.plugin_settings: dict[tuple[str, str], str] = {}

    def name(self):
        return self._name

    def absolutePath(self):
        return self._path

    def version(self):
        return sys.modules["mobase"].VersionInfo(self._version)

    def setVersion(self, version):
        self._version = version.text

    def url(self):
        return self._url

    def setUrl(self, url):
        self._url = url

    def setPluginSetting(self, plugin, key, value):
        self.plugin_settings[(plugin, key)] = value
        return True


class FakeModList:
    """Ordered mod list: index 0 is lowest priority, the last entry wins."""

    def __init__(self, organizer):
        self._organizer = organizer
        self.names: list[str] = []
        self.active: set[str] = set()
        self.installed_callbacks = []
        self.moved_callbacks, self.state_callbacks = [], []
        self.removed_callbacks = []
        self.calls: list[tuple] = []          # changes made by the plugin (not by the "user")
        self.hook_runs = 0                    # how many hook invocations happened in total
        self.paths: dict[str, str] = {}       # mod name -> folder, for getMod().absolutePath()

    def onModInstalled(self, cb):
        self.installed_callbacks.append(cb)
        return True

    def onModMoved(self, cb):
        self.moved_callbacks.append(cb)
        return True

    def onModRemoved(self, cb):
        self.removed_callbacks.append(cb)
        return True

    def onModStateChanged(self, cb):
        self.state_callbacks.append(cb)
        return True

    # Like MO2, every change fires the hooks, whoever made it.
    def _set_active(self, name, active):
        (self.active.add if active else self.active.discard)(name)
        self.hook_runs += len(self.state_callbacks)
        for cb in list(self.state_callbacks):
            cb({name: self.state(name)})

    def _move(self, name, priority):
        old = self.names.index(name)
        self.names.remove(name)
        self.names.insert(min(priority, len(self.names)), name)
        new = self.names.index(name)
        self.hook_runs += len(self.moved_callbacks)
        for cb in list(self.moved_callbacks):
            cb(name, old, new)

    def user_set_active(self, name, active):
        self._set_active(name, active)

    def user_move(self, name, priority):
        self._move(name, priority)

    def getMod(self, name):
        if name in self.paths:
            return FakeMod(self.paths[name], name=name)
        return object() if name in self.names else None

    def state(self, name):
        return sys.modules["mobase"].ModState.ACTIVE if name in self.active else 0

    def setActive(self, name, active):
        self.calls.append(("setActive", name, active))
        self._set_active(name, active)
        return True

    def allModsByProfilePriority(self):
        return list(self.names)

    def setPriority(self, name, priority):
        self.calls.append(("setPriority", name, priority))
        self._move(name, priority)
        return True


class FakeOrganizer:
    """Stands in for ``mobase.IOrganizer``: records hooks, mods, paths and the managed game."""

    def __init__(self):
        self.managed = None
        self.base_path = "does-not-exist"  # instance dir holding ModOrganizer.ini
        self.mods_path = "does-not-exist"
        self.created: list[str] = []
        self.refreshed: list[bool] = []
        self.on_disk_after_refresh: list[str] = []   # mods MO2 finds on disk when it refreshes
        self.create_returns_none = False
        self.ui_callbacks, self.profile_callbacks = [], []
        self.about_to_run_callbacks, self.finished_callbacks = [], []
        self.next_refresh_callbacks = []
        self.next_refresh_raises = False
        self.settings: dict[tuple[str, str], object] = {}
        self.started: list[tuple] = []
        self.waited: list[int] = []
        self.wait_refresh: list[bool] = []
        self.data_changed: list = []
        self.while_running = None

        self._mod_list = FakeModList(self)

        class _Features:
            def __init__(self):
                self.registered = []  # (game, feature, priority, replace) for each registerFeature call

            def registerFeature(self, *args):
                self.registered.append(args)
                return True

        self._features = _Features()

    @property
    def installed_callbacks(self):
        return self._mod_list.installed_callbacks

    def modList(self):
        return self._mod_list

    def gameFeatures(self):
        return self._features

    def managedGame(self):
        return self.managed

    def basePath(self):
        return self.base_path

    def modsPath(self):
        return self.mods_path

    def createMod(self, guessed):
        if self.create_returns_none:
            return None
        self.created.append(guessed.value)
        self._mod_list.names.append(guessed.value)  # new mods start at the end
        return object()

    def pluginSetting(self, plugin, key):
        return self.settings.get((plugin, key))

    def setPluginSetting(self, plugin, key, value):
        self.settings[(plugin, key)] = value

    def startApplication(self, exe, args=(), cwd=""):
        self.started.append((exe, list(args), cwd))
        return len(self.started)

    def waitForApplication(self, handle, refresh=True):
        self.waited.append(handle)
        self.wait_refresh.append(refresh)
        if self.while_running:
            self.while_running()  # what the program does before it exits
        return True, 0

    def modDataChanged(self, mod):
        self.data_changed.append(mod)

    def refresh(self, save_changes=True):
        self.refreshed.append(save_changes)
        for name in self.on_disk_after_refresh:
            if name not in self._mod_list.names:
                self._mod_list.names.append(name)

    def onUserInterfaceInitialized(self, cb):
        self.ui_callbacks.append(cb)
        return True

    def onProfileChanged(self, cb):
        self.profile_callbacks.append(cb)
        return True

    def onAboutToRun(self, cb):
        self.about_to_run_callbacks.append(cb)
        return True

    def onFinishedRun(self, cb):
        self.finished_callbacks.append(cb)
        return True

    def onNextRefresh(self, cb, immediate_if_possible=True):
        if self.next_refresh_raises:
            raise RuntimeError("no refresh hook")
        self.next_refresh_callbacks.append(cb)
        return True

    def run_next_refresh(self):
        """MO2 finishing a mod list refresh: runs and clears the one-shot callbacks."""
        callbacks, self.next_refresh_callbacks = self.next_refresh_callbacks, []
        for cb in callbacks:
            cb()


class FakeEntry:
    """A file or folder of a fake archive tree.  Folders behave like ``mobase.IFileTree``:
    iterable, with ``find``, ``move``, ``detach`` and ``parent``.  ``moves`` records the
    ``move`` calls made on that folder."""

    def __init__(self, name: str, is_dir: bool = False, children=()):
        self._name, self._is_dir, self._parent = name, is_dir, None
        self.children: list[FakeEntry] = []
        self.moves: list[tuple[str, str]] = []
        for child in children:
            self._adopt(child)

    def _adopt(self, child):
        child._parent = self
        self.children.append(child)

    def name(self):
        return self._name

    def isDir(self):
        return self._is_dir

    def parent(self):
        return self._parent

    def __iter__(self):
        return iter(list(self.children))

    def find(self, path):
        """Like IFileTree.find: nested ``/`` paths, case-insensitive, None when missing."""
        node = self
        for part in (p for p in path.replace("\\", "/").split("/") if p):
            node = next((c for c in node.children if c.name().casefold() == part.casefold()), None)
            if node is None:
                return None
        return node

    def detach(self):
        if self._parent is not None:
            self._parent.children.remove(self)
            self._parent = None

    def _ensure_dir(self, path):
        node = self
        for part in (p for p in path.split("/") if p):
            nxt = next((c for c in node.children if c.name() == part and c.isDir()), None)
            if nxt is None:
                nxt = FakeEntry(part, True)
                node._adopt(nxt)
            node = nxt
        return node

    def move(self, entry, target):
        """Like IFileTree.move with its default policy (FAIL_IF_EXISTS): moving onto an
        existing name changes nothing and returns False; missing folders are created."""
        assert target.endswith("/"), "the plugin always moves into a directory"
        self.moves.append((entry.name(), target))
        dest = self._ensure_dir(target)
        taken = next((c for c in dest.children if c.name().casefold() == entry.name().casefold()), None)
        if taken is entry:
            return True
        if taken is not None:
            return False
        entry.detach()
        dest._adopt(entry)
        return True

    def files(self, prefix=""):
        """Sorted posix paths of every file below this folder."""
        out = []
        for child in self.children:
            path = f"{prefix}{child.name()}"
            out += child.files(path + "/") if child.isDir() else [path]
        return sorted(out)


class FakeTree(FakeEntry):
    """The root of a fake archive: a nameless folder with no parent."""

    def __init__(self, *entries: FakeEntry):
        super().__init__("", True, entries)
