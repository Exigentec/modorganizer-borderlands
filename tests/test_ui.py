"""ui.when_running: start-up work waits for MO2's real event loop and a visible main window."""

import unittest
from unittest import mock

from tests import fakes

fakes.install()

from borderlands import ui  # noqa: E402


class WhenRunning(unittest.TestCase):
    def setUp(self):
        fakes.LOG.clear()
        fakes.QTimer.queue = []
        self.addCleanup(setattr, fakes.QTimer, "queue", None)
        self.addCleanup(fakes.QThread.level.__setitem__, 0, 1)
        fakes.QThread.level[0] = 1
        self.ran = []

    def fire(self):
        fakes.QTimer.queue.pop(0)()

    def test_it_runs_once_in_the_main_loop_with_a_visible_window(self):
        window = mock.Mock()
        ui.when_running(lambda: self.ran.append(1), window)
        self.assertEqual(self.ran, [])                                          # never synchronously
        self.fire()
        self.assertEqual(self.ran, [1])
        self.assertEqual(fakes.QTimer.queue, [])                                # and no more timers

    def test_it_waits_while_the_loop_is_not_running_or_a_dialog_is_open(self):
        for level in (0, 2):
            fakes.QThread.level[0] = level
            ui.when_running(lambda: self.ran.append(level))
            self.fire()
            self.fire()
            self.assertEqual(self.ran, [])
            fakes.QThread.level[0] = 1
            self.fire()
            self.assertEqual(self.ran, [level])
            self.ran.clear()

    def test_it_waits_for_the_window_to_be_shown(self):
        window = mock.Mock()
        window.isVisible.return_value = False
        ui.when_running(lambda: self.ran.append(1), lambda: window)
        self.fire()
        self.assertEqual(self.ran, [])
        window.isVisible.return_value = True
        self.fire()
        self.assertEqual(self.ran, [1])

    def test_no_window_to_check_is_fine(self):
        ui.when_running(lambda: self.ran.append(1), object())
        self.fire()
        self.assertEqual(self.ran, [1])

    def test_a_failure_is_logged_and_never_escapes_into_qt(self):
        def boom():
            raise RuntimeError("kaput")

        ui.when_running(boom)
        self.fire()
        self.assertTrue([m for _, m in fakes.LOG if "kaput" in m])

    def test_it_gives_up_with_a_warning_if_the_loop_never_comes_up(self):
        fakes.QThread.level[0] = 0
        ui.when_running(lambda: self.ran.append(1), give_up_s=0)
        self.fire()
        self.assertEqual(self.ran, [])
        self.assertEqual(fakes.QTimer.queue, [])
        self.assertTrue([m for _, m in fakes.LOG if "did not come up" in m])


if __name__ == "__main__":
    unittest.main()
