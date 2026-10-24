"""Reusable widgets: cards, path rows, navigation, tables."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSizePolicy,
    QStackedWidget, QTableView, QToolButton, QTreeWidget,
    QVBoxLayout, QWidget,
)

from ..constants import APP_DIR
from ..system import resolve_tool_path
from .icon import app_icon_pixmap
from .theme import AlignL, AlignR, AlignV, T


# --- Small building blocks ------------------------------------------------------------------------
def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return label


def accent_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("accent", True)
    button.setMinimumHeight(38)
    return button


class Card(QFrame):
    """A Fluent card: a rounded, outlined surface with an optional semibold title."""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 13, 16, 14)
        self.lay.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            self.lay.addWidget(label)

    def add(self, item) -> None:
        if isinstance(item, QWidget):
            self.lay.addWidget(item)
        else:
            self.lay.addLayout(item)


class Expander(Card):
    """A card whose body folds away behind a header, like the expanders in the Windows 11 Settings app."""

    def __init__(self, title: str, expanded: bool = False):
        super().__init__("")
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.header = QToolButton()
        self.header.setObjectName("expanderHeader")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.toggled.connect(self._toggled)
        self.lay.addWidget(self.header)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(16, 2, 16, 14)
        self.body_lay.setSpacing(8)
        self.lay.addWidget(self.body)
        self._toggled(expanded)

    def add(self, item) -> None:
        """Same as Card.add, but into the foldable body (so Expander and Card are interchangeable)."""
        if isinstance(item, QWidget):
            self.body_lay.addWidget(item)
        else:
            self.body_lay.addLayout(item)

    def _toggled(self, open_: bool) -> None:
        self.header.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)
        self.body.setVisible(open_)
        self.updateGeometry()  # the card, and the page holding it, shrink or grow to match
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, SizedStack):
            parent = parent.parentWidget()
        if parent is not None:  # whatever sits under the page (the Performance card) follows
            QTimer.singleShot(0, parent.refit)


class PathRow(QWidget):
    """label + text box + Browse... button, for a folder or a program."""

    def __init__(self, label: str, folder: bool = True, label_width: int = 112, placeholder: str = ""):
        super().__init__()
        self.folder = folder
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.label = QLabel(label)
        self.label.setFixedWidth(label_width)
        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(True)
        self.edit.setPlaceholderText(placeholder)
        self.button = QPushButton("Browse...")
        self.button.clicked.connect(self._browse)
        lay.addWidget(self.label)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.button)

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:
        self.edit.setText(text)

    def _start_dir(self) -> str:
        current = resolve_tool_path(self.edit.text().strip(" \"'"))
        if current:
            path = Path(current)
            folder = path if path.is_dir() else path.parent
            if folder.is_dir():
                return str(folder)
        return str(APP_DIR)

    def _browse(self) -> None:
        if self.folder:
            picked = QFileDialog.getExistingDirectory(self.window(), "Select directory", self._start_dir())
        else:
            flt = "Programs (*.exe);;All files (*)" if sys.platform == "win32" else "All files (*)"
            picked, _ = QFileDialog.getOpenFileName(self.window(), "Select executable", self._start_dir(), flt)
        if picked:
            self.edit.setText(picked)


class ToolRow(QWidget):
    """A PathRow for a conversion tool, with its live status line underneath."""

    def __init__(self, label: str, label_width: int):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.path = PathRow(label, folder=False, label_width=label_width)
        self.status = hint("")
        lay.addWidget(self.path)
        lay.addWidget(self.status)


class NavButton(QAbstractButton):
    """A navigation-pane item: rounded hover/selected fill, accent indicator pill, status dot and badge."""

    def __init__(self, key: str, text: str, role: str, parent: QWidget | None = None):
        super().__init__(parent)
        # role: the Theme colour of the dot (its group's colour); state: "ok" / "warn" / "bad" overrides it for
        # conversion tools, whose dot shows whether the tool is ready
        self.key, self.label, self.role, self.state, self.badge, self.selected = key, text, role, None, "", False
        self.setFixedHeight(36)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(200, 36)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        hovered = self.underMouse() and self.isEnabled()
        if self.selected or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.select if self.selected else T.hover)
            p.drawRoundedRect(r, 5, 5)
        if self.selected:  # the accent pill on the left edge
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.accent)
            p.drawRoundedRect(QRectF(r.left(), r.center().y() - 8, 3, 16), 1.5, 1.5)
        if self.hasFocus():
            p.setPen(QPen(T.fg, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 4, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(getattr(T, self.state or self.role))
        p.drawEllipse(QRectF(r.left() + 14, r.center().y() - 4, 8, 8))
        p.setFont(self.font())
        fm = p.fontMetrics()
        badge_w = fm.horizontalAdvance(self.badge) + 12 if self.badge else 0
        text_w = max(20, r.width() - 36 - 10 - badge_w)
        p.setPen(T.fg)
        p.drawText(QRectF(r.left() + 34, r.top(), text_w, r.height()), AlignL | AlignV,
                   fm.elidedText(self.label, Qt.TextElideMode.ElideRight, int(text_w)))
        if self.badge:
            p.setPen(T.fg3)
            p.drawText(QRectF(r.right() - badge_w - 4, r.top(), badge_w, r.height()), AlignR | AlignV, self.badge)


class NavHeader(QAbstractButton):
    """A collapsible group heading in the navigation pane."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.label, self.open = text, True
        self.setFixedHeight(32)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 0, -2, 0)
        if self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.hover)
            p.drawRoundedRect(r.adjusted(0, 3, 0, -3), 5, 5)
        font = self.font()
        font.setBold(True)
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)
        p.setFont(font)
        p.setPen(T.fg3)
        p.drawText(QRectF(r.left() + 14, r.top(), r.width() - 40, r.height()), AlignL | AlignV, self.label)
        # chevron
        cx, cy = r.right() - 16, r.center().y()
        p.setPen(QPen(T.fg3, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if self.open:
            p.drawPolyline(QPolygonF([QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2)]))
        else:
            p.drawPolyline(QPolygonF([QPointF(cx - 2, cy - 4), QPointF(cx + 2, cy), QPointF(cx - 2, cy + 4)]))

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class GearButton(QAbstractButton):
    """The Settings entry at the bottom of the navigation pane: a drawn gear and a label."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.label = text
        self.setFixedHeight(40)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if text:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:  # icon only
            self.setFixedWidth(44)
        self.setToolTip("Settings: tool paths, downloads, logins and torrent privacy")

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self.underMouse() or self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.select if self.isDown() else T.hover)
            p.drawRoundedRect(r, 5, 5)
        if self.hasFocus():
            p.setPen(QPen(T.fg, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 4, 4)
        # the gear: eight teeth around a ring
        cx, cy = (r.center().x() if not self.label else r.left() + 22), r.center().y()
        p.save()
        p.translate(cx, cy)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(T.fg, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        for _ in range(8):
            p.drawLine(QPointF(0, -6.2), QPointF(0, -9.2))
            p.rotate(45)
        p.setPen(QPen(T.fg, 1.8))
        p.drawEllipse(QPointF(0, 0), 5.6, 5.6)
        p.drawEllipse(QPointF(0, 0), 2.1, 2.1)
        p.restore()
        if not self.label:
            return
        p.setFont(self.font())
        p.setPen(T.fg)
        p.drawText(QRectF(r.left() + 42, r.top(), r.width() - 46, r.height()), AlignL | AlignV, self.label)


class Logo(QWidget):
    """The app icon at sidebar size."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(36, 36)
        self._pix = app_icon_pixmap()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        p.drawPixmap(self.rect(), self._pix)


class ToggleTable(QTableView):
    """A table where Space asks for every selected row to be ticked / unticked, and Delete asks to remove them."""

    rowsToggled = Signal(list)  # view rows
    deletePressed = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if rows := sorted({ix.row() for ix in self.selectionModel().selectedRows()}):
                self.rowsToggled.emit(rows)
            return
        if event.key() == Qt.Key.Key_Delete and not event.isAutoRepeat():
            self.deletePressed.emit()
            return
        super().keyPressEvent(event)


class CheckTree(QTreeWidget):
    """A tree where Space asks for the current row to be ticked / unticked."""

    spacePressed = Signal(object)  # the QTreeWidgetItem

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if (item := self.currentItem()) is not None:
                self.spacePressed.emit(item)
            return
        super().keyPressEvent(event)


class SizedStack(QStackedWidget):
    """A stack that is only as tall as the page showing (a plain one is as tall as its tallest page).

    A page marked compact (property "compact") also gets its height pinned to its content, so whatever sits below
    the stack follows the cards up and down as they fold. Pages that hold a list meant to fill the space
    (Download, Organize) are left to grow."""

    _MAX = 16777215  # QWIDGETSIZE_MAX

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _i: self._fit())

    def setCurrentWidget(self, widget: QWidget) -> None:
        super().setCurrentWidget(widget)
        self._fit()

    def _fit(self) -> None:
        current = self.currentWidget()
        for i in range(self.count()):
            page = self.widget(i)
            page.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Preferred if page is current else QSizePolicy.Policy.Ignored)
        self.refit()

    def refit(self) -> None:
        """Re-measure the visible page and size the stack to it."""
        page = self.currentWidget()
        lay = page.layout() if page is not None else None
        if lay is None or not page.property("compact"):
            self.setMinimumHeight(0)
            self.setMaximumHeight(self._MAX)
            self.updateGeometry()
            return
        lay.activate()
        width = max(self.width(), 1)
        need = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        need = max(need, lay.totalMinimumSize().height())
        if self.minimumHeight() != need or self.maximumHeight() != need:
            self.setFixedHeight(need)
        self.updateGeometry()

    def event(self, e) -> bool:
        done = super().event(e)
        if e.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Resize):
            self.refit()
        return done

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()
