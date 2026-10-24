"""Application start-up."""
from __future__ import annotations

import contextlib
import ctypes
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QStyleFactory

from .constants import APP_NAME, APP_VERSION
from . import magnet
from .migrate import migrate_legacy_data
from .ui.main_window import SquashUIWindow
from .ui.theme import T


def main() -> int:
    migrate_legacy_data()
    if sys.platform == "win32":
        with contextlib.suppress(Exception):  # a taskbar identity of its own, not "python"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.{APP_VERSION}")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    links = magnet.links_from_args(sys.argv[1:])  # a magnet: link opened from a browser or another program
    if links and magnet.send_to_running(links):
        return 0  # an open SquashUI took it
    styles = {name.lower() for name in QStyleFactory.keys()}
    app.setStyle("windows11" if "windows11" in styles else "fusion")  # Qt 6.7+ ships the Windows 11 style
    families = set(QFontDatabase.families())
    for family in ("Segoe UI Variable Text", "Segoe UI"):  # Windows 11's UI font
        if family in families:
            font = QFont(family)
            font.setPointSizeF(9.0)
            app.setFont(font)
            break
    T.refresh()
    window = SquashUIWindow()
    window.show()
    magnet.serve(window)
    if links:
        QTimer.singleShot(0, lambda: window.open_magnets(links))
    return app.exec()
