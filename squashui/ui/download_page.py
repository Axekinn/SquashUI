"""Download page: link list, torrent/login dialogs, the download run and its saved session."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from .. import magnet
from ..dlfiles import (
    compact_plan, delete_files, explorer_select_command, files_on_disk, magnet_for,
    nearest_folder, per_item_for, wait_until,
)
from ..downloads import DL_MAX_PARALLEL, DownloadRunner, DownloadSummary, logins_missing, RemoteFile, SESSION_FILE
from ..links import probe_sizes, resolve_links
from ..models import fmt_duration, fmt_size
from ..torbox import torbox_key
from ..torrent_runners import TorBoxRunner, TorrentRunner
from ..torrents import ENGINE, import_libtorrent, is_torrent_token, load_torrent_settings, NEEDS_LIBTORRENT
from .dialogs import LoginsDialog, TorrentDialog
from .state import LABEL_W
from .widgets import Card, Expander, hint, PathRow, ToggleTable


class DownloadMixin:
    """Download page: link list, torrent/login dialogs, the download run and its saved session.

    Methods only; the state lives on SquashUIWindow, which mixes this in."""

    @property
    def dl_files(self) -> list[RemoteFile]:
        return self.dl_model.files

    @property
    def dl_checked(self) -> set[int]:
        return self.dl_model.checked

    @dl_checked.setter
    def dl_checked(self, value: set[int]) -> None:
        self.dl_model.checked = value

    def _build_dl_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        links = Expander("Links", expanded=True)
        links.add(hint("Paste one or more archive.org item links (or a link to a plain file listing), one per line. "
                       "Magnet links and .torrent files work too - drop them anywhere on this page."))
        self.dl_url_text = QPlainTextEdit()
        self.dl_url_text.setFixedHeight(72)
        self.dl_url_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.dl_url_text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.dl_url_text.setPlaceholderText("https://archive.org/details/...")
        self.dl_url_text.setAcceptDrops(False)  # a drop falls through to the window, which understands torrents
        links.add(self.dl_url_text)
        row = QHBoxLayout()
        self.dl_fetch_btn = QPushButton("Fetch file list")
        self.dl_fetch_btn.clicked.connect(self._dl_fetch)
        self.dl_clear_btn = QPushButton("Clear")
        self.dl_clear_btn.setToolTip("Empty the Links box")
        self.dl_clear_btn.clicked.connect(self.dl_url_text.clear)
        self.dl_status = hint("")
        row.addWidget(self.dl_fetch_btn)
        row.addWidget(self.dl_clear_btn)
        row.addSpacing(6)
        row.addWidget(self.dl_status, 1)
        links.add(row)
        lay.addWidget(links)

        self.dl_dest = PathRow("Save to", label_width=LABEL_W)
        self.dl_dest.edit.setAcceptDrops(False)
        self.dl_dest.edit.textChanged.connect(lambda *_: self._dl_save_soon())
        dest = Expander("Save location", expanded=True)
        dest.add(self.dl_dest)
        lay.addWidget(dest)

        files = Card("Files  (click a row to tick or untick it, right-click for more)")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.dl_filter = QLineEdit()
        self.dl_filter.setPlaceholderText("Filter files")
        self.dl_filter.setClearButtonEnabled(True)
        self.dl_filter.setAcceptDrops(False)
        self.dl_filter.setMaximumWidth(260)
        self.dl_filter.textChanged.connect(lambda *_: self._dl_filter_timer.start())
        select = QPushButton("Select all")
        select.clicked.connect(lambda: self._dl_select(True))
        clear = QPushButton("Select none")
        clear.clicked.connect(lambda: self._dl_select(False))
        remove = QPushButton("Remove selected")
        remove.setToolTip("Remove the ticked files from the list")
        remove.clicked.connect(lambda: self._dl_delete(sorted(self.dl_checked)))
        self.dl_extras = QCheckBox("Show archive.org extras")
        self.dl_extras.toggled.connect(lambda *_: (self._dl_apply_filter(), self._dl_save_soon()))
        for w in (self.dl_filter, select, clear, remove, self.dl_extras):
            bar.addWidget(w)
        bar.addStretch(1)
        files.add(bar)

        table = self.dl_table = ToggleTable()
        table.setModel(self.dl_proxy)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        table.setMinimumHeight(190)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setSortingEnabled(True)
        table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # until a header is clicked the list keeps its own order
        hdr = table.horizontalHeader()
        hdr.setStretchLastSection(True)  # the last column soaks up slack, so dragging a border never moves the one before it
        hdr.setHighlightSections(False)
        hdr.setMinimumSectionSize(40)
        for col in range(1, 6):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        for col, width in ((0, 40), (1, 360), (2, 92), (3, 80), (4, 230), (5, 96)):
            table.setColumnWidth(col, width)
        hdr.sectionResized.connect(lambda *_: self._dl_save_soon())
        table.clicked.connect(self._dl_clicked)
        table.rowsToggled.connect(self._dl_rows_toggled)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._dl_context_menu)
        table.deletePressed.connect(lambda: self._dl_delete())
        files.add(table)
        self.dl_sel_label = hint("Paste a link above and press Fetch file list.")
        files.add(self.dl_sel_label)
        lay.addWidget(files)

        return page

    def _logins_dialog(self, needed: list[str] | None = None) -> bool:
        """Modal sign-in window. Returns False only when the person cancels."""
        dlg = LoginsDialog(self, needed, self.dl_remember)
        dlg.exec()
        if dlg.ok:
            self.dl_remember = dlg.remember_value
        return dlg.ok

    def _torrent_dialog(self) -> bool:
        dlg = TorrentDialog(self)
        dlg.exec()
        return dlg.ok

    def _torrent_ready(self, network: bool) -> bool:
        """libtorrent installed and, for anything that touches the swarm, the privacy settings chosen."""
        if import_libtorrent() is None:
            self._info("Torrent support", NEEDS_LIBTORRENT)
            return False
        cfg = load_torrent_settings()
        if network and cfg["require_vpn"] and not cfg["adapter"]:
            return self._torrent_dialog()
        return True

    def _dl_login_check(self, urls: list[str], required_only: bool = False) -> bool:
        """Ask for logins the job involves but doesn't have yet. False = the person cancelled."""
        if missing := logins_missing(urls, required_only):
            return self._logins_dialog(missing)
        return True

    def _dl_add_links(self, links: list[str]) -> None:
        have = set(self.dl_url_text.toPlainText().split())
        if fresh := [link for link in links if link not in have]:
            current = self.dl_url_text.toPlainText().strip()
            self.dl_url_text.setPlainText((current + "\n" if current else "") + "\n".join(fresh))
        if not isinstance(self.runner, DownloadRunner):
            self._dl_fetch()

    def open_magnets(self, links: list[str]) -> None:
        """Magnet links handed over by Windows (or by a second launch): bring the window up and add them."""
        if not (self.is_processing or self.org_busy):
            self._show_page("dl")
        magnet.bring_to_front(self)  # first: fetching can open a dialog that would otherwise sit behind other windows
        self._dl_add_links(links)

    def _dl_open_torrents(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Open .torrent file(s)", "", "Torrent files (*.torrent)")
        if paths:
            self._dl_add_links([Path(p).resolve().as_uri() for p in paths])

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if self.current_page == "dl" and (mime.hasUrls() or mime.hasText()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        mime, links = event.mimeData(), []
        for url in mime.urls():
            if url.isLocalFile():
                local = url.toLocalFile()
                if local.lower().endswith(".torrent") and Path(local).is_file():
                    links.append(Path(local).resolve().as_uri())
            elif url.scheme().lower() == "magnet" and url.toString() not in links:
                links.append(url.toString())
        if mime.hasText():
            links += [piece for piece in mime.text().split() if piece.lower().startswith("magnet:") and piece not in links]
        if links:
            self._dl_add_links(links)
        else:
            self.dl_status.setText("Drop .torrent files or magnet links here.")
        event.acceptProposedAction()

    def _dl_fetch(self) -> None:
        text = self.dl_url_text.toPlainText().strip()
        if not text:
            self.dl_status.setText("Paste a link first.")
            return
        tokens = text.split()
        if not self._dl_login_check([t for t in tokens if not is_torrent_token(t)], required_only=True):
            return
        # With a TorBox key a cached torrent is listed from TorBox, so the swarm (and the VPN it needs) is only
        # involved when it isn't cached; list_torrent reports a missing VPN / libtorrent itself in that case.
        if any(is_torrent_token(t) for t in tokens) and not torbox_key() and not self._torrent_ready(
                network=any(t.lower().strip("\"'").startswith("magnet:") for t in tokens)):
            return
        self._dl_token += 1
        self.dl_fetch_btn.setEnabled(False)
        self.dl_status.setText("Fetching file list...")
        threading.Thread(target=self._dl_resolve, args=(self._dl_token, text), daemon=True).start()

    def _dl_resolve(self, token: int, text: str) -> None:  # worker thread: no Qt in here
        ENGINE.note = lambda msg: self.ui_q.put(("dl_note", token, msg))
        try:
            files, errors = resolve_links(text)
        except Exception as exc:
            files, errors = [], [f"Unexpected error: {exc}"]
        self.ui_q.put(("dl_files", token, files, errors))
        if files:  # listings without sizes get them filled in as the answers arrive
            # the file itself, not its row number: rows can be removed from the list while sizes are still arriving
            probe_sizes(files, lambda i, n: self.ui_q.put(("dl_size", token, files[i], n)))

    def _dl_populate(self, files: list[RemoteFile], errors: list[str]) -> None:
        if not isinstance(self.runner, DownloadRunner):
            self.dl_fetch_btn.setEnabled(True)
        self.dl_model.set_files(files)
        self._dl_cleared, self._dl_next = False, []
        pre = {i for i, rf in enumerate(files) if rf.preselect}
        self.dl_checked = pre or {i for i, rf in enumerate(files) if not rf.extra}
        note = f"{len(files)} file(s) found"
        if extras := sum(rf.extra for rf in files):
            note += f" ({extras} archive.org extras hidden)"
        if errors:
            note = errors[0] if not files else note + f" - {len(errors)} link(s) had problems: {errors[0]}"
        self.dl_status.setText(note)
        self.dl_status.setToolTip("\n".join(errors))
        self._dl_apply_filter()

    def _dl_visible(self) -> list[int]:
        terms = self.dl_filter.text().lower().split()
        extras = self.dl_extras.isChecked()
        gone = self.dl_model.removed
        return [i for i, rf in enumerate(self.dl_files)
                if i not in gone and (extras or not rf.extra) and all(t in rf.name.lower() for t in terms)]

    def _dl_apply_filter(self) -> None:
        self._dl_filter_timer.stop()
        if not self.dl_extras.isChecked():  # hidden files can't stay ticked
            self.dl_checked = self.dl_checked - {i for i, rf in enumerate(self.dl_files) if rf.extra}
        self.dl_proxy.set_visible(set(self._dl_visible()))
        self.dl_model.refresh_checks()
        self._dl_update_sel()

    def _dl_update_sel(self) -> None:
        shown = len(self.dl_files) - len(self.dl_model.removed)
        if shown <= 0:
            if self._dl_cleared:  # the person emptied the list (at start-up an empty list just keeps its first hint)
                self.dl_sel_label.setText("The list is empty. Paste a link above and press Fetch file list.")
                self._dl_save_soon()
            return
        picks = [self.dl_files[i] for i in self.dl_checked if i < len(self.dl_files)]
        self._dl_save_soon()
        total = sum(rf.size or 0 for rf in picks)
        more = "+" if any(rf.size is None for rf in picks) else ""
        self.dl_sel_label.setText(f"{len(picks)} of {shown} file(s) selected ({fmt_size(total)}{more})")

    def _dl_toggle(self, indices: list[int]) -> None:
        self.dl_model.toggle(indices)
        self._dl_update_sel()

    def _dl_select(self, on: bool) -> None:
        if isinstance(self.runner, DownloadRunner):
            return
        self._dl_toggle([i for i in self._dl_visible() if (i in self.dl_checked) != on])

    def _dl_clicked(self, index) -> None:
        if isinstance(self.runner, DownloadRunner) or not index.isValid():
            return
        if QApplication.keyboardModifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return  # extending the selection, not ticking
        self._dl_toggle([self.dl_proxy.mapToSource(index).row()])

    def _dl_rows_toggled(self, rows: list[int]) -> None:
        if isinstance(self.runner, DownloadRunner):
            return
        self._dl_toggle([self.dl_proxy.mapToSource(self.dl_proxy.index(r, 0)).row() for r in rows])

    # ---- right-click menu: open, start, stop, delete -------------------------------------------------------
    def _dl_runner(self) -> DownloadRunner | None:
        return self.runner if isinstance(self.runner, DownloadRunner) else None

    def _dl_is_live(self, row: int) -> bool:
        """Waiting for, or taking part in, the current run (or lined up behind it)."""
        runner = self._dl_runner()
        return row in self._dl_next or (runner is not None and runner.is_live(row))

    def _dl_selected_rows(self) -> list[int]:
        """The highlighted rows as list positions, top to bottom as shown."""
        picked = sorted(self.dl_table.selectionModel().selectedRows(), key=lambda ix: ix.row())
        return [self.dl_proxy.mapToSource(ix).row() for ix in picked]

    def _dl_dest_path(self) -> Path | None:
        raw = self._clean(self.dl_dest.text())
        return Path(raw) if raw else None

    def _dl_per_item(self, rf: RemoteFile) -> bool:
        return per_item_for(rf, self.dl_folders.isChecked())

    @staticmethod
    def _dl_link(rf: RemoteFile) -> str | None:
        """What to copy for 'Copy link': the web address, or a magnet link for a file inside a torrent."""
        return magnet_for(rf) if rf.url.startswith("torrent:") else rf.url

    def _dl_context_menu(self, pos) -> None:
        table = self.dl_table
        ix = table.indexAt(pos)
        if ix.isValid() and not table.selectionModel().isRowSelected(ix.row(), QModelIndex()):
            table.selectRow(ix.row())  # right-clicking outside the selection acts on that row alone
        rows = self._dl_selected_rows()
        running = self._dl_runner() is not None
        finished = [i for i in self._dl_visible() if self.dl_model.is_finished(i)]
        menu = QMenu(table)

        def add(label: str, handler: Callable[[], None], enabled: bool = True) -> None:
            act = menu.addAction(label)
            act.setEnabled(enabled)
            act.triggered.connect(lambda _checked=False: handler())

        if rows:
            many = len(rows) > 1
            blocked = self.org_busy or (self.is_processing and not running)  # a conversion owns the run controls
            ticked = [i for i in rows if i in self.dl_checked]
            has_links = any(self._dl_link(self.dl_files[i]) for i in rows)
            add("Open folder", lambda: self._dl_open_folder(rows))
            menu.addSeparator()
            add("Start", lambda: self._dl_start_rows(rows), not blocked and any(not self._dl_is_live(i) for i in rows))
            add("Stop", lambda: self._dl_stop_rows(rows), any(self._dl_is_live(i) for i in rows))
            menu.addSeparator()
            add("Tick", lambda: self._dl_set_ticks(rows, True), not running and len(ticked) < len(rows))
            add("Untick", lambda: self._dl_set_ticks(rows, False), not running and bool(ticked))
            menu.addSeparator()
            add("Copy names" if many else "Copy name", lambda: self._dl_copy(rows, False))
            add("Copy links" if many else "Copy link", lambda: self._dl_copy(rows, True), has_links)
            menu.addSeparator()
            add("Delete...\tDel", lambda: self._dl_delete(rows))
        if finished:
            if rows:
                menu.addSeparator()
            add(f"Remove {len(finished)} finished from the list", lambda: self._dl_remove_finished(finished))
        if menu.isEmpty():
            return
        menu.exec(table.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _dl_set_ticks(self, rows: list[int], on: bool) -> None:
        if self._dl_runner() is None:  # ticks are frozen while a run is going, as when clicking
            self._dl_toggle([i for i in rows if (i in self.dl_checked) != on])

    def _dl_copy(self, rows: list[int], links: bool) -> None:
        if links:
            found = [link for i in rows if (link := self._dl_link(self.dl_files[i]))]
            text = "\n".join(dict.fromkeys(found))  # many files of one torrent share a single magnet link
        else:
            text = "\n".join(self.dl_files[i].name for i in rows)
        QApplication.clipboard().setText(text)
        self.dl_status.setText(f"Copied {'link(s)' if links else 'name(s)'} to the clipboard.")

    def _dl_open_folder(self, rows: list[int]) -> None:
        dest = self._dl_dest_path()
        if dest is None:
            self.dl_status.setText("Choose a Save to folder first.")
            return
        for i in rows:  # the first selected file that exists is shown in its folder, highlighted where Explorer allows
            rf = self.dl_files[i]
            if found := files_on_disk(rf, dest, self._dl_per_item(rf)):
                if cmd := explorer_select_command(found[0]):
                    with contextlib.suppress(OSError):
                        subprocess.Popen(cmd)
                        return
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(found[0].parent)))
                return
        rf = self.dl_files[rows[0]]  # nothing downloaded yet: the folder it will go to, or the nearest one that exists
        folder = nearest_folder(rf, dest, self._dl_per_item(rf))
        if folder is None:
            self.dl_status.setText(f"Nothing has been saved to {dest} yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _dl_start_rows(self, rows: list[int]) -> None:
        """Start just these files: as a new run when idle, or added to the run that is going."""
        rows = [i for i in rows if i < len(self.dl_files) and i not in self.dl_model.removed]
        runner = self._dl_runner()
        if runner is None:
            if rows and not (self.is_processing or self.org_busy):
                for i in rows:
                    self.dl_model.set_checked(i, True)
                self._dl_update_sel()
                self._start_download(only=rows)
            return
        fresh = [i for i in rows if not self._dl_is_live(i)]
        if not fresh:
            return
        want_torrent = isinstance(runner, TorrentRunner)
        fits = [i for i in fresh if self.dl_files[i].url.startswith("torrent:") == want_torrent]
        later = [i for i in fresh if i not in fits]  # the other kind of download: can't share this run
        if fits and not want_torrent and not self._dl_login_check([self.dl_files[i].url for i in fits]):
            return
        for i in fresh:  # ticked, so the list shows what is in the batch
            self.dl_model.set_checked(i, True)
        self._dl_update_sel()
        taken = runner.add([(i, self.dl_files[i]) for i in fits]) if fits else []
        if taken is None:  # a torrent session can't take files mid-run, and a run that just ended can't either
            later, taken = later + fits, []
        for i in taken:
            self.dl_model.set_status(i, "Queued")
        self._dl_defer(later)
        if taken:
            self._refresh_dl(True)

    def _dl_defer(self, rows: list[int]) -> None:
        """Line files up to start as soon as the current run is over."""
        for i in rows:
            if i not in self._dl_next:
                self._dl_next.append(i)
                self.dl_model.set_status(i, "Queued - starts after this run")

    def _dl_clear_next(self) -> None:
        for i in self._dl_next:
            self.dl_model.set_status(i, "Stopped")
        self._dl_next = []

    def _dl_stop_rows(self, rows: list[int]) -> None:
        """Stop just these files. Whatever was downloaded of them is kept, so Start picks up where they stopped."""
        for i in [i for i in rows if i in self._dl_next]:
            self._dl_next.remove(i)
            self.dl_model.set_status(i, "Stopped")
        if (runner := self._dl_runner()) is not None:
            runner.cancel([i for i in rows if runner.is_live(i)])  # the runner reports each row as it lets go
        self._dl_refresh_live()

    def _dl_refresh_live(self) -> None:
        if self._dl_runner() is not None:
            self._refresh_dl(True)

    def _dl_run_next(self) -> None:
        """The run that was in the way is over: start what was lined up behind it."""
        rows = [i for i in self._dl_next if i < len(self.dl_files) and i not in self.dl_model.removed]
        self._dl_next = rows
        if not rows or self.is_processing:  # still busy: they stay lined up and are tried again when that run ends
            return
        torrent = self.dl_files[rows[0]].url.startswith("torrent:")
        now = [i for i in rows if self.dl_files[i].url.startswith("torrent:") == torrent]
        self._dl_next = [i for i in rows if i not in now]  # the other kind follows when this run is over
        self._start_download(only=now)
        if not self.is_processing:  # it didn't start (a message says why): don't leave anything waiting for nothing
            for i in rows:
                self.dl_model.set_status(i, "")
            self._dl_next = []

    def _dl_after_run(self) -> bool:
        """A download run just ended: now that no row number is in use, drop the rows removed during it, then start
        whatever was lined up. True if another run follows."""
        self._dl_compact()
        if not self._dl_next:
            return False
        QTimer.singleShot(0, self._dl_run_next)
        return True

    # ---- taking rows out of the list ---------------------------------------------------------------------
    def _dl_compact(self) -> None:
        """Really remove the rows that were only hidden. Only while no run is using row numbers."""
        gone = set(self.dl_model.removed)
        if not gone or self._dl_runner() is not None:
            return
        plan = self.dl_model.remove_rows(gone)
        self._dl_epoch += 1  # a start-up disk scan still running has stale row numbers
        self._dl_next = [plan[i] for i in self._dl_next if i < len(plan) and plan[i] is not None]
        self._dl_apply_filter()

    def _dl_remove_finished(self, rows: list[int]) -> None:
        self._dl_remove(rows, delete_from_disk=False)

    def _dl_delete(self, rows: list[int] | None = None) -> None:
        """Delete / right-click > Delete...: remove files from the list, and ask whether to delete them from disk too."""
        rows = [i for i in (self._dl_selected_rows() if rows is None else rows)
                if i < len(self.dl_files) and i not in self.dl_model.removed]
        if not rows:
            return
        dest = self._dl_dest_path()
        on_disk: list[Path] | None = None  # None = too many rows to look at, so assume there may be something
        if dest is not None and len(rows) <= 500:
            on_disk = [p for i in rows for p in files_on_disk(self.dl_files[i], dest, self._dl_per_item(self.dl_files[i]))]
        elif dest is None:
            on_disk = []
        live = any(self._dl_is_live(i) for i in rows)

        name = self.dl_files[rows[0]].name.rsplit("/", 1)[-1]
        what = f'"{name}"' if len(rows) == 1 else f"{len(rows)} files"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Delete downloads")
        box.setText(f"Remove {what} from the list?")
        remove_btn = box.addButton("Remove from list", QMessageBox.ButtonRole.AcceptRole)
        delete_btn = None
        if on_disk is None:
            detail = "'Delete files too' also deletes anything already downloaded of them from disk."
        elif on_disk:
            size = 0
            for path in on_disk:
                with contextlib.suppress(OSError):
                    size += path.stat().st_size
            detail = (f"{len(on_disk)} file(s) on disk ({fmt_size(size)}), partial downloads included. "
                      "'Delete files too' removes them permanently (not to the Recycle Bin).")
        else:
            detail = "None of them have anything on disk yet."
        if on_disk is None or on_disk:
            delete_btn = box.addButton("Delete files too", QMessageBox.ButtonRole.DestructiveRole)
        if live:
            detail += "\n\nDownloads in progress are stopped first."
        box.setInformativeText(detail)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(remove_btn)  # the safe choice is the one Enter picks
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None or clicked is cancel_btn:
            return
        self._dl_remove(rows, delete_from_disk=clicked is delete_btn)

    def _dl_remove(self, rows: list[int], delete_from_disk: bool) -> None:
        dest = self._dl_dest_path()
        runner = self._dl_runner()
        live = [i for i in rows if self._dl_is_live(i)]
        doomed = [(self.dl_files[i], self._dl_per_item(self.dl_files[i])) for i in rows]  # by file: rows renumber below
        if live:
            self._dl_stop_rows(live)
        self.dl_model.removed |= set(rows)  # hidden at once; really removed as soon as no run is using row numbers
        self.dl_checked = self.dl_checked - set(rows)
        self._dl_next = [i for i in self._dl_next if i not in self.dl_model.removed]
        self._dl_cleared = len(self.dl_files) == len(self.dl_model.removed)
        if delete_from_disk and dest is not None:
            # A download that is only now letting go of its file can't be deleted until it has: the worker waits for that.
            busy = (lambda: any(runner.is_busy(i) for i in live)) if runner is not None and live else None
            threading.Thread(target=self._dl_delete_worker, args=(doomed, dest, busy), daemon=True).start()
        if runner is None:
            self._dl_compact()
        else:
            self._dl_apply_filter()
        self.dl_status.setText(f"Removed {len(rows)} file(s) from the list"
                               + (", deleting them from disk..." if delete_from_disk else ". Files on disk were kept."))
        self._dl_save_soon()

    def _dl_delete_worker(self, doomed: list[tuple[RemoteFile, bool]], dest: Path,
                          busy: Callable[[], bool] | None) -> None:  # worker thread: no Qt in here
        if busy is not None:
            wait_until(lambda: not busy(), 30)
        paths = [p for rf, per_item in doomed for p in files_on_disk(rf, dest, per_item)]  # looked up once nothing writes
        deleted, problems = delete_files(paths, dest, patience=30 if busy is not None else 3)
        self.ui_q.put(("dl_deleted", deleted, problems))

    def _dl_deleted(self, count: int, problems: list[str]) -> None:
        self.dl_status.setText(f"Deleted {count} file(s) from disk." + (f" {len(problems)} couldn't be deleted." if problems else ""))
        if problems:
            lines = [f"  - {p}" for p in problems[:12]] + ([f"  ...and {len(problems) - 12} more"] if len(problems) > 12 else [])
            self._warn("Some files couldn't be deleted", "They are no longer in the list, but are still on disk:\n\n"
                       + "\n".join(lines))

    def _start_download(self, only: list[int] | None = None) -> None:
        if self.is_processing:
            return
        picks = sorted(i for i in (self.dl_checked if only is None else only)
                       if i < len(self.dl_files) and i not in self.dl_model.removed)
        if not picks:
            self._info("Nothing selected", "Fetch a file list and tick at least one file first.")
            return
        dest_raw = self._clean(self.dl_dest.text())
        if not dest_raw:
            self._error("Error", "Choose a folder to save the downloads in.")
            return
        dest = Path(dest_raw)
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._error("Error", f"Can't use that folder:\n\n{exc}")
            return
        threads = min(DL_MAX_PARALLEL, max(1, self.dl_threads.value()))
        is_torrent = [self.dl_files[i].url.startswith("torrent:") for i in picks]
        if any(is_torrent) and not all(is_torrent):
            self._error("Mixed selection", "Torrent files and web files can't be downloaded in the same run. "
                                           "Untick one kind and start again.")
            return
        jobs = [(i, self.dl_files[i]) for i in picks]
        if all(is_torrent):
            if torbox_key():  # TorBox's cache is tried first; the swarm only gets what isn't cached
                runner = TorBoxRunner(jobs, dest, threads, self.ui_q.put, self.pause_event, load_torrent_settings())
            else:
                if not self._torrent_ready(network=True):
                    return
                runner = TorrentRunner(jobs, dest, self.ui_q.put, self.pause_event, load_torrent_settings())
        else:
            if not self._dl_login_check([self.dl_files[i].url for i in picks]):
                return
            runner = DownloadRunner(jobs, dest, threads, self.dl_verify.isChecked(),
                                    self.dl_folders.isChecked(), self.ui_q.put, self.pause_event)
        self._t0, self._paused_total, self._pause_t, self._eta_tick = time.monotonic(), 0.0, None, 0.0
        self._dl_samples.clear()
        self.is_processing, self.is_paused = True, False
        self.pause_event.set()
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setText("Pause")
        self.dl_fetch_btn.setEnabled(False)
        self.lbl_phase.setText("Phase: Downloading")
        info = f"DOWNLOAD  |  {len(picks)} file(s)  ->  {dest}"
        self.lbl_run_info.setText(info if len(info) <= 80 else info[:38] + "..." + info[-39:])
        self.lbl_eta.setText("")
        self._set_bar(0, max(1, runner.bytes_total))
        self.lbl_total_prog.setText(f"Download Progress: 0 / {runner.files_total}")
        for i in picks:
            self.dl_model.set_status(i, "Queued")
        self.runner = runner
        self._set_settings_visible(False)  # the log gets the window while it runs
        threading.Thread(target=runner.run, daemon=True).start()

    def _dl_threads_changed(self, n: int) -> None:
        """The spinbox applies live: a running download picks up the new limit straight away."""
        self._dl_save_soon()
        if isinstance(self.runner, DownloadRunner):
            self.runner.set_threads(n)

    def _refresh_dl(self, force: bool = False) -> None:
        r = self.runner
        if not isinstance(r, DownloadRunner) or self._t0 is None:
            return
        now = time.monotonic()
        if not force and now - self._eta_tick < 0.5:
            return
        self._eta_tick = now
        done, total = r.bytes_done, r.bytes_total
        self._set_bar(min(done, total), max(1, total))
        self.lbl_total_prog.setText(f"Download Progress: {r.files_finished} / {r.files_total}")
        t, samples = self._active_secs(), self._dl_samples
        if not samples or t - samples[-1][0] >= 1.0:
            samples.append((t, done))
        while len(samples) > 2 and t - samples[0][0] > 20:  # speed = the last ~20 seconds
            samples.popleft()
        head = f"Elapsed: {fmt_duration(t)}  |  {fmt_size(done)} of {fmt_size(total)}"
        if self.is_paused:
            self.lbl_eta.setText(f"{head}  |  Paused")
            return
        t0, b0 = samples[0]
        speed = (done - b0) / (t - t0) if t > t0 else 0.0
        if speed <= 0:
            self.lbl_eta.setText(f"{head}  |  Starting...")
        else:
            left = f"  |  Est. remaining: ~{fmt_duration((total - done) / speed)}" if total > done else ""
            self.lbl_eta.setText(f"{head}  |  {fmt_size(int(speed))}/s{left}")

    def _show_dl_summary(self, sm: DownloadSummary) -> None:
        if not (sm.done or sm.skipped or sm.failures):
            return  # every file was stopped one by one: the Status column already says so
        parts = [f"{sm.done} downloaded ({fmt_size(sm.bytes_received)})"]
        if sm.skipped:
            parts.append(f"{sm.skipped} already present")
        if sm.stopped:
            parts.append(f"{sm.stopped} stopped")
        took = fmt_duration(sm.seconds)
        if not sm.failures:
            self._info("Downloads Complete", f"{', '.join(parts)} in {took}.")
            return
        lines = [f"  - {name}: {reason}" for _i, name, reason in sm.failures[:12]]
        if len(sm.failures) > 12:
            lines.append(f"  ...and {len(sm.failures) - 12} more (see the Status column)")
        text = (f"{', '.join(parts)}, {len(sm.failures)} failed, in {took}.\n\nFailed:\n" + "\n".join(lines)
                + "\n\nPartial downloads are kept, so a retry resumes where each one stopped.\n\n"
                  "Retry the failed files now?")
        retry, cancel = QMessageBox.StandardButton.Retry, QMessageBox.StandardButton.Cancel
        if QMessageBox.warning(self, "Downloads finished with errors", text, retry | cancel, retry) == retry:
            self._start_download(only=[i for i, _name, _why in sm.failures])

    def _dl_save_soon(self) -> None:
        self._dl_save_timer.start()  # restarts the 1.5 s countdown

    def _dl_save_session(self) -> None:
        """Write the file list, ticks, filter and options next to the app so a restart can pick up where this left off."""
        self._dl_save_timer.stop()
        gone, files = self.dl_model.removed, self.dl_files
        if len(files) == len(gone):  # nothing left to remember
            if self._dl_cleared:  # emptied on purpose: forget the old list too, or a restart would bring it back
                with contextlib.suppress(OSError):
                    SESSION_FILE.unlink(missing_ok=True)
            return  # otherwise never replace a saved list with an empty one
        plan = compact_plan(len(files), gone)
        try:
            data = {
                "version": 1,
                "urls": self.dl_url_text.toPlainText().strip(),
                "dest": self.dl_dest.text(),
                "threads": self.dl_threads.value(),
                "verify": self.dl_verify.isChecked(),
                "folders": self.dl_folders.isChecked(),
                "extras": self.dl_extras.isChecked(),
                "filter": self.dl_filter.text(),
                "was_running": isinstance(self.runner, DownloadRunner),
                "checked": sorted(plan[i] for i in self.dl_checked if i < len(plan) and plan[i] is not None),
                "cols_user": True,
                "col_widths": {name: self.dl_table.columnWidth(col) for name, col in self.DL_COLS.items()},
                "files": [[f.name, f.url, f.size, f.group, f.md5, f.extra, f.preselect, f.source]
                          for i, f in enumerate(files) if i not in gone],
            }
            tmp = SESSION_FILE.with_name(SESSION_FILE.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, SESSION_FILE)
        except (OSError, ValueError, TypeError):
            pass  # remembering the session is a convenience; never let it interrupt a download

    def _dl_restore_session(self) -> None:
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            files = [RemoteFile(str(r[0]), str(r[1]), None if r[2] is None else int(r[2]), str(r[3] or ""), r[4],
                                bool(r[5]), bool(r[6]), str(r[7]) if len(r) > 7 else "")  # older sessions: 7 fields
                     for r in data["files"]]
        except (OSError, ValueError, KeyError, TypeError):
            return
        if not files:
            return
        self.dl_url_text.setPlainText(str(data.get("urls", "")))
        self.dl_dest.setText(str(data.get("dest", "")))
        with contextlib.suppress(ValueError, TypeError):
            self.dl_threads.setValue(min(DL_MAX_PARALLEL, max(1, int(data.get("threads", 4)))))
        self.dl_verify.setChecked(bool(data.get("verify", True)))
        self.dl_folders.setChecked(bool(data.get("folders", False)))
        self.dl_extras.setChecked(bool(data.get("extras", False)))
        self.dl_filter.setText(str(data.get("filter", "")))
        if data.get("cols_user") and isinstance(data.get("col_widths"), dict):
            for name, width in data["col_widths"].items():
                if name in self.DL_COLS and isinstance(width, int):
                    self.dl_table.setColumnWidth(self.DL_COLS[name], max(50, min(2000, width)))
        self._dl_populate(files, [])
        self.dl_checked = {i for i in data.get("checked", []) if isinstance(i, int) and 0 <= i < len(files)}
        self._dl_apply_filter()
        self.dl_status.setText(f"Restored your last list: {len(files)} file(s), {len(self.dl_checked)} selected. "
                               "Press Start to continue - finished files are skipped and partial ones resume.")
        if data.get("was_running"):
            self._show_page("dl")
        dest, per_item = self._clean(self.dl_dest.text()), self.dl_folders.isChecked()
        if dest and (picks := sorted(self.dl_checked)):
            threading.Thread(target=self._dl_scan_disk, args=(files, picks, Path(dest), per_item, self._dl_epoch),
                             daemon=True).start()

    def _dl_scan_disk(self, files: list[RemoteFile], picks: list[int], dest: Path, per_item: bool, epoch: int) -> None:
        """Worker thread: mark restored rows that are already finished or partly downloaded."""
        probe = DownloadRunner([], dest, 1, False, per_item, lambda _e: None, threading.Event())
        for i in picks:
            rf = files[i]
            if rf.url.startswith("torrent:"):
                continue  # libtorrent checks what is already on disk itself when the download starts
            try:
                path = probe._target(rf)
                if path.exists() and (rf.size is None or path.stat().st_size == rf.size):
                    self.ui_q.put(("dl_scan_row", epoch, i, "Already downloaded"))
                else:
                    part = path.with_name(path.name + ".part")
                    if part.exists():
                        self.ui_q.put(("dl_scan_row", epoch, i, f"Partial: {fmt_size(part.stat().st_size)} (will resume)"))
            except (OSError, ValueError):
                continue
