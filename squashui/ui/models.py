"""Table model and filter proxy for the download list."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from ..dlfiles import compact_plan
from ..models import fmt_size
from .theme import T

if TYPE_CHECKING:
    from ..downloads import RemoteFile


SOURCE_TIPS = {
    "TorBox": "Cached on TorBox: downloads over HTTPS from TorBox. No swarm and no VPN needed.",
    "Torrent": "Regular torrent: downloads from the swarm, only through your VPN adapter.",
    "Direct": "Downloads straight from the website.",
}


FINISHED = ("Done", "Already downloaded")  # statuses of a row whose file is complete


# --- Download list model --------------------------------------------------------------------------
class DlModel(QAbstractTableModel):
    """The Download tab's file list: name, size, status and speed per file, plus which are ticked.

    A running download addresses rows by number, so a row taken out of the list while one is going is only marked in
    `removed` (the page hides it); remove_rows() deletes the marked rows for real once nothing is running."""

    HEAD = ("", "File", "Size", "Source", "Status", "Speed")

    def __init__(self) -> None:
        super().__init__()
        self.files: list[RemoteFile] = []
        self.checked: set[int] = set()
        self.removed: set[int] = set()
        self.status: list[str] = []
        self.speed: list[str] = []
        self.source: list[str] = []
        self._row_of: dict[int, int] = {}

    def set_files(self, files: list[RemoteFile]) -> None:
        self.beginResetModel()
        self.files = list(files)  # a copy: the thread that listed them keeps its own, and rows can leave this one
        self.removed = set()
        self.status = [""] * len(files)
        self.speed = [""] * len(files)
        self.source = [f.source or "Direct" for f in files]
        self._row_of = {id(f): i for i, f in enumerate(self.files)}
        self.endResetModel()

    def row_of(self, rf: RemoteFile) -> int | None:
        """Where this file is in the list now (row numbers move when rows are removed; the file itself doesn't)."""
        return self._row_of.get(id(rf))

    def is_finished(self, i: int) -> bool:
        return 0 <= i < len(self.status) and self.status[i] in FINISHED

    def remove_rows(self, rows: set[int]) -> list[int | None]:
        """Take these rows out for real. Returns old row number -> new row number (None for the removed ones)."""
        plan = compact_plan(len(self.files), rows)
        gone = sorted(i for i in rows if 0 <= i < len(self.files))
        ranges: list[list[int]] = []
        for i in gone:  # runs of neighbouring rows go in one step
            if ranges and i == ranges[-1][1] + 1:
                ranges[-1][1] = i
            else:
                ranges.append([i, i])
        for first, last in reversed(ranges):  # last run first, so the earlier row numbers stay valid meanwhile
            self.beginRemoveRows(QModelIndex(), first, last)
            for column in (self.files, self.status, self.speed, self.source):
                del column[first:last + 1]
            self.endRemoveRows()
        self.checked = {plan[i] for i in self.checked if i < len(plan) and plan[i] is not None}
        self.removed = set()
        self._row_of = {id(f): i for i, f in enumerate(self.files)}
        return plan

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.files)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEAD)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEAD[section]
        return None

    def flags(self, index) -> Qt.ItemFlag:
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.files):
            return None
        i, col = index.row(), index.column()
        rf = self.files[i]
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return 2 if i in self.checked else 0
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 1:
                return rf.name
            if col == 2:
                return fmt_size(rf.size) if rf.size is not None else "..."
            if col == 3:
                return self.source[i]
            if col == 4:
                return self.status[i]
            if col == 5:
                return self.speed[i]
        elif role == Qt.ItemDataRole.ForegroundRole and col == 3 and self.source[i] == "TorBox":
            return T.ok
        elif role == Qt.ItemDataRole.UserRole:  # what clicking a header sorts by
            if col == 0:
                return int(i in self.checked)
            if col == 1:
                return rf.name.lower()
            if col == 2:
                return -1 if rf.size is None else rf.size
            return self.source[i] if col == 3 else self.status[i] if col == 4 else self.speed[i]
        elif role == Qt.ItemDataRole.ToolTipRole:
            if col == 1:
                return rf.name
            if col == 3:
                return SOURCE_TIPS.get(self.source[i])
            if col == 4 and self.status[i]:
                return self.status[i]  # a long failure reason doesn't fit the column
        return None

    def _touch(self, i: int, col: int) -> None:
        if 0 <= i < len(self.files):
            ix = self.index(i, col)
            self.dataChanged.emit(ix, ix)

    def set_status(self, i: int, text: str) -> None:
        if 0 <= i < len(self.files):
            self.status[i] = text
            self._touch(i, 4)

    def set_speed(self, i: int, text: str) -> None:
        if 0 <= i < len(self.files):
            self.speed[i] = text
            self._touch(i, 5)

    def set_source(self, i: int, text: str) -> None:
        if 0 <= i < len(self.files) and self.source[i] != text:
            self.source[i] = text
            self._touch(i, 3)

    def size_changed(self, i: int) -> None:
        self._touch(i, 2)

    def toggle(self, rows: list[int]) -> None:
        for i in rows:
            self.checked ^= {i}
            self._touch(i, 0)

    def set_checked(self, i: int, on: bool) -> None:
        if (i in self.checked) != on:
            self.toggle([i])

    def refresh_checks(self) -> None:
        if self.files:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.files) - 1, 0))


class DlProxy(QSortFilterProxyModel):
    """Hides rows the filter / "archive.org extras" setting rules out, and sorts by column."""

    def __init__(self) -> None:
        super().__init__()
        self.visible: set[int] | None = None
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:
        return self.visible is None or source_row in self.visible

    def set_visible(self, rows: set[int]) -> None:
        self.visible = rows
        self.invalidateFilter()
