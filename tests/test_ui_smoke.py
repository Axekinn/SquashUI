"""Builds the real window off-screen. Skipped when PySide6 isn't installed (CI installs it)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

try:
    import PySide6  # noqa: F401
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if sys.platform != "win32":  # Windows' offscreen plugin has no fonts and can crash; the normal one works without showing windows
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from tests import helpers  # noqa: E402,F401


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class WindowSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def make_window(self):
        import squashui.tools as tools
        from squashui.ui.main_window import SquashUIWindow

        with mock.patch.object(tools.ToolManager, "run", lambda self: None, create=True), \
                mock.patch("threading.Thread.start", lambda self: None):  # no network, no background workers
            return SquashUIWindow()

    def test_window_builds_with_every_page(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIn("SquashUI", win.windowTitle())
        self.assertGreater(win.stack.count(), 5)

    def test_run_buttons_start_in_the_right_state(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertTrue(win.start_btn.isEnabled())
        self.assertFalse(win.pause_btn.isEnabled())
        self.assertFalse(win.stop_btn.isEnabled())

    def test_converter_pages_are_compact_and_list_pages_are_not(self):
        win = self.make_window()
        self.addCleanup(win.close)
        for mode in win.MODES:
            with self.subTest(page=mode):
                self.assertTrue(win.pages[mode].property("compact"))
        self.assertFalse(win.pages["dl"].property("compact"))  # lists fill the space instead

    def test_expander_puts_its_content_in_the_foldable_body(self):
        from PySide6.QtWidgets import QLabel

        from squashui.ui.widgets import Expander

        card, label = Expander("Options", expanded=True), QLabel("inside")
        card.add(label)
        self.assertIs(label.parent(), card.body)
        card.header.setChecked(False)
        self.assertTrue(card.body.isHidden())
        card.header.setChecked(True)
        self.assertFalse(card.body.isHidden())

    def test_stack_pins_compact_pages_to_their_content(self):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from squashui.ui.widgets import SizedStack

        def page(compact: bool) -> QWidget:
            w = QWidget()
            QVBoxLayout(w).addWidget(QLabel("row"))
            w.setProperty("compact", compact)
            return w

        stack = SizedStack()
        tight, loose = page(True), page(False)
        stack.addWidget(tight)
        stack.addWidget(loose)
        stack.setCurrentWidget(tight)
        self.assertEqual(stack.minimumHeight(), stack.maximumHeight())
        self.assertLess(stack.maximumHeight(), SizedStack._MAX)
        stack.setCurrentWidget(loose)
        self.assertEqual((stack.minimumHeight(), stack.maximumHeight()), (0, SizedStack._MAX))

    def test_themes_compute_without_a_display(self):
        from squashui.ui.theme import T

        T.refresh()
        self.assertTrue(T.accent.isValid())

    def test_icon_is_a_real_image(self):
        from squashui.ui.icon import app_icon_pixmap

        pix = app_icon_pixmap()
        self.assertFalse(pix.isNull())
        self.assertGreaterEqual(pix.width(), 128)


if __name__ == "__main__":
    unittest.main()
