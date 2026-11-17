"""The few Qt dialogs the SDK and usvfs managers share."""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QThread, QTimer, qWarning
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog


def when_running(work, window=None, *, delay_ms: int = 500, give_up_s: float = 1200.0) -> None:
    """Run ``work`` once MO2's main event loop is running and its main window is up, and not before.

    MO2 calls a plugin's start-up callbacks from inside the main window's constructor, before the window
    is shown and before ``QApplication.exec()``.  A dialog opened there runs in a nested loop, and anything
    that asks MO2 to quit (``qApp->exit()``) is lost, because ``exec()`` clears that request when it starts:
    MO2 then sits there with its wait cursor on and never closes.  So prompts, upgrades and restarts wait
    here for the real loop (loop level 1: no nested dialog is open either) and a visible window.

    ``window`` is the main window, a callable returning it, or None.  ``work`` runs at most once; if the
    loop is not up after ``give_up_s`` it is dropped and a warning logged.
    """
    deadline = time.monotonic() + give_up_s

    def step() -> None:
        target = window() if callable(window) else window
        visible = getattr(target, "isVisible", None)
        if QThread.currentThread().loopLevel() != 1 or (visible is not None and not visible()):
            if time.monotonic() < deadline:
                QTimer.singleShot(delay_ms, step)
            else:
                qWarning("Borderlands: MO2's main window did not come up, so a start-up step was skipped.")
            return
        try:
            work()
        except Exception as err:  # a failing slot would take MO2 down with it
            qWarning(f"Borderlands: a start-up step failed: {err}")

    QTimer.singleShot(0, step)


def say(parent, kind: str, title: str, text: str) -> None:
    """A message box: ``kind`` is ``information``, ``warning`` or ``critical``."""
    getattr(QMessageBox, kind)(parent, title, text)


def ask(parent, title: str, text: str, buttons: list[str], default: int = 0) -> str:
    """A question with the given buttons (the first is the main one, the last is the way out)."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    roles = QMessageBox.ButtonRole
    made = [box.addButton(label, roles.AcceptRole if i == 0 else roles.RejectRole if i == len(buttons) - 1
                          else roles.ActionRole) for i, label in enumerate(buttons)]
    box.setDefaultButton(made[default])
    box.setEscapeButton(made[-1])
    box.exec()
    clicked = box.clickedButton()
    return buttons[made.index(clicked)] if clicked in made else buttons[-1]


def busy(work):
    """Run ``work`` with the wait cursor on."""
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        return work()
    finally:
        QApplication.restoreOverrideCursor()


class Progress:
    """A modal progress dialog that keeps the window responsive and can be cancelled."""

    def __init__(self, parent, title: str, label: str):
        self._dialog = QProgressDialog(label, "Cancel", 0, 100, parent)
        self._dialog.setWindowTitle(title)
        self._dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._dialog.setMinimumDuration(0)
        self._dialog.setAutoClose(False)
        self._dialog.setAutoReset(False)
        self._dialog.setValue(0)

    def update(self, done: int, total: int) -> bool:
        """For ``sdk.extract`` / ``netio.download``: False means the user cancelled."""
        self._dialog.setValue(int(done * 100 / total) if total else 0)
        QApplication.processEvents()
        return not self._dialog.wasCanceled()

    def close(self) -> None:
        self._dialog.close()
