"""Fluent colours derived from the live Qt palette."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


AlignL, AlignR, AlignC, AlignV = (Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight,
                                  Qt.AlignmentFlag.AlignCenter, Qt.AlignmentFlag.AlignVCenter)


# --- Theme: Fluent colours derived from the live palette ----------------------------------------
def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """a moved t of the way towards b."""
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


class Theme:
    """Surface, text and accent colours that follow Windows' light/dark setting and the user's accent colour.

    Everything is computed from QApplication.palette(), the same palette the native controls are drawn with, so
    the custom parts (navigation pane, cards) can never disagree with them.
    """

    def __init__(self) -> None:
        self.refresh()  # safe before QApplication exists: falls back to a dark default until main() refreshes it

    def refresh(self) -> None:
        if QApplication.instance() is None:
            win, fg, highlight = QColor("#202020"), QColor("#ffffff"), QColor("#4cc2ff")
        else:
            pal = QApplication.palette()
            win, fg = pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.WindowText)
            highlight = pal.color(QPalette.ColorRole.Highlight)
        white, black = QColor("#ffffff"), QColor("#000000")
        self.dark = win.lightness() < 128
        self.bg, self.fg = win, fg
        self.fg2, self.fg3 = _mix(fg, win, 0.30), _mix(fg, win, 0.52)
        self.card = _mix(win, white, 0.045) if self.dark else (white if win.lightness() < 250 else _mix(win, black, 0.025))
        self.border = _mix(win, fg, 0.15)
        self.hover, self.select = _mix(win, fg, 0.06), _mix(win, fg, 0.10)
        self.accent = QColor(highlight)
        luma = 0.299 * self.accent.red() + 0.587 * self.accent.green() + 0.114 * self.accent.blue()
        self.accent_fg = black if luma > 150 else white
        self.accent_hover = _mix(self.accent, white, 0.12) if self.dark else _mix(self.accent, black, 0.10)
        self.accent_press = _mix(self.accent, white, 0.25) if self.dark else _mix(self.accent, black, 0.22)
        if self.dark:
            self.ok, self.warn, self.bad = QColor("#6ccb5f"), QColor("#fce100"), QColor("#ff99a4")
            self.group_org, self.group_dl = QColor("#b39dfa"), QColor("#ffa45c")
        else:
            self.ok, self.warn, self.bad = QColor("#0f7b0f"), QColor("#9d5d00"), QColor("#c42b1c")
            self.group_org, self.group_dl = QColor("#6b4fc2"), QColor("#b4530b")

    def qss(self) -> str:
        c = lambda col: col.name()  # noqa: E731
        return f"""
QFrame#card {{ background: {c(self.card)}; border: 1px solid {c(self.border)}; border-radius: 8px; }}
QLabel#cardTitle {{ font-weight: 600; }}
QLabel#hint {{ color: {c(self.fg3)}; }}
QLabel#notice {{ color: {c(self.warn)}; font-weight: 600; }}
QLabel#muted {{ color: {c(self.fg2)}; }}
QLabel#pageTitle {{ font-size: 21pt; font-weight: 600; }}
QLabel#phase {{ font-size: 11pt; font-weight: 600; }}
QLabel#brandName {{ font-size: 14pt; font-weight: 600; }}
QLabel#dialogHead {{ font-size: 16pt; font-weight: 600; }}
QWidget#sideFooter {{ border-top: 1px solid {c(self.border)}; }}
QLabel#mono {{ font-family: "Cascadia Mono", Consolas, monospace; }}
QWidget#sidebar {{ background: transparent; }}
QWidget#commandBar {{ background: {c(self.card)}; border-top: 1px solid {c(self.border)}; }}
QPushButton[accent="true"] {{ background: {c(self.accent)}; color: {c(self.accent_fg)};
    border: 1px solid {c(self.accent)}; border-radius: 5px; padding: 6px 18px; font-weight: 600; }}
QPushButton[accent="true"]:hover {{ background: {c(self.accent_hover)}; border-color: {c(self.accent_hover)}; }}
QPushButton[accent="true"]:pressed {{ background: {c(self.accent_press)}; border-color: {c(self.accent_press)}; }}
QPushButton[accent="true"]:disabled {{ background: {c(self.select)}; color: {c(self.fg3)}; border-color: {c(self.select)}; }}
QPlainTextEdit#log {{ background: {c(self.card)}; border: 1px solid {c(self.border)}; border-radius: 8px;
    padding: 6px; font-family: "Cascadia Mono", Consolas, monospace; font-size: 9pt;
    selection-background-color: {c(self.accent)}; selection-color: {c(self.accent_fg)}; }}
QToolButton#expanderHeader {{ border: none; border-radius: 7px; background: transparent; padding: 11px 14px;
    text-align: left; font-weight: 600; }}
QToolButton#expanderHeader:hover {{ background: {c(self.hover)}; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:vertical:hover {{ background: {c(self.border)}; margin: 4px 0; border-radius: 1px; }}
"""


T = Theme()
