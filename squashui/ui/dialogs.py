"""Dialogs: logins, torrent privacy, end-of-run summary."""
from __future__ import annotations

import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from ..downloads import CUSTOM_KINDS, CUSTOM_SLOTS, login_for, LOGIN_SITES, LOGINS, save_logins, SKIPPED_LOGINS
from ..models import STAGE_TITLES
from ..torrents import list_adapters, load_torrent_settings, save_torrent_settings
from .theme import T
from .widgets import accent_button, Card, hint


# --- Dialogs ------------------------------------------------------------------------------------
def _button_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch(1)
    for b in buttons:
        row.addWidget(b)
    return row


class LoginsDialog(QDialog):
    """Sign-in details for archive.org, 1fichier and any other host. `needed` = sites in this job that have no
    login yet: it then also offers 'Continue without logging in'. `ok` is False only when the person cancels."""

    def __init__(self, parent: QWidget, needed: list[str] | None, remember: bool):
        super().__init__(parent)
        self.setWindowTitle("Logins")
        self.setModal(True)
        self.needed = needed or []
        self.ok = False
        self.remember_value = remember
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        if needed:
            names = " and ".join(LOGIN_SITES[k][0] for k in needed)
            intro = QLabel(f"This job includes {names} files. Sign in below if they need an account, "
                           "or continue without logging in.")
            intro.setWordWrap(True)
            lay.addWidget(intro)
        self.fields: dict[tuple[str, str], QLineEdit] = {}
        for key, (name, _hosts, fields, where, required) in LOGIN_SITES.items():
            box = QGroupBox(name + ("  (needed for these links)" if required else "  (optional)"))
            grid = QGridLayout(box)
            grid.setColumnStretch(1, 1)
            for row, (fld, label, hidden) in enumerate(fields):
                edit = QLineEdit((LOGINS.get(key) or {}).get(fld, ""))
                if hidden:
                    edit.setEchoMode(QLineEdit.EchoMode.Password)
                edit.setMinimumWidth(300)
                self.fields[(key, fld)] = edit
                grid.addWidget(QLabel(label), row, 0)
                grid.addWidget(edit, row, 1)
            get_key = QPushButton("Get key...")
            get_key.clicked.connect(lambda _checked=False, u=where: webbrowser.open(u))
            grid.addWidget(get_key, 0, 2)
            lay.addWidget(box)

        other = QGroupBox("Other sites (any file host)")
        grid = QGridLayout(other)
        for col, head in enumerate(("Host", "Type", "User", "Password / token / cookie")):
            grid.addWidget(QLabel(head), 0, col)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(3, 3)
        self.custom: list[tuple[QLineEdit, QComboBox, QLineEdit, QLineEdit]] = []
        saved = LOGINS.get("custom", [])
        for row in range(CUSTOM_SLOTS):
            cur = saved[row] if row < len(saved) else {}
            host, user, secret = QLineEdit(cur.get("host", "")), QLineEdit(cur.get("user", "")), QLineEdit(cur.get("secret", ""))
            secret.setEchoMode(QLineEdit.EchoMode.Password)
            kind = QComboBox()
            kind.addItems(CUSTOM_KINDS)
            if cur.get("kind") in CUSTOM_KINDS:
                kind.setCurrentText(cur["kind"])
            for col, w in enumerate((host, kind, user, secret)):
                grid.addWidget(w, row + 1, col)
            self.custom.append((host, kind, user, secret))
        lay.addWidget(other)

        self.remember = QCheckBox("Remember on this computer (stored as plain text next to this app)")
        self.remember.setChecked(remember)
        lay.addWidget(self.remember)

        save = accent_button("Save and continue" if needed else "Save")
        save.setMinimumHeight(32)
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons = [save]
        if needed:
            skip = QPushButton("Continue without logging in")
            skip.clicked.connect(self._skip)
            buttons.append(skip)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.append(cancel)
        lay.addLayout(_button_row(*buttons))

    def _save(self) -> None:
        for key, (_n, _h, fields, _w, _r) in LOGIN_SITES.items():
            vals = {f: self.fields[(key, f)].text().strip() for f, _l, _hid in fields}
            if any(vals.values()):
                LOGINS[key] = vals
            else:
                LOGINS.pop(key, None)
        rows = []
        for host_e, kind_e, user_e, secret_e in self.custom:
            host = host_e.text().strip().lower()
            host = (urllib.parse.urlsplit(host).hostname or host) if "://" in host else host.strip("/")
            if host and secret_e.text().strip():
                rows.append({"host": host, "kind": kind_e.currentText(), "user": user_e.text().strip(),
                             "secret": secret_e.text().strip()})
        LOGINS["custom"] = rows
        self.remember_value = self.remember.isChecked()
        save_logins(self.remember_value)
        SKIPPED_LOGINS.update(k for k in self.needed if login_for(f"https://{LOGIN_SITES[k][1][0]}/") is None)
        self.ok = True
        self.accept()

    def _skip(self) -> None:
        SKIPPED_LOGINS.update(self.needed)
        self.ok = True
        self.accept()


class TorrentDialog(QDialog):
    """Pick the VPN adapter torrent traffic is bound to."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Torrent privacy")
        self.setModal(True)
        self.ok = False
        self.cfg = load_torrent_settings()
        self.adapters: list[dict] = []
        self._scanning = False
        self._box: queue.Queue = queue.Queue(maxsize=1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        intro = QLabel("Everyone in a torrent swarm can see the IP address you connect from. To keep yours private, "
                       "torrent traffic is tied to your VPN's network adapter: if the VPN drops, torrenting stops "
                       "instead of falling back to your normal connection.")
        intro.setWordWrap(True)
        intro.setMinimumWidth(520)
        lay.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("VPN adapter:"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(380)
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.combo, 1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)
        lay.addWidget(hint("Connect your VPN first, then press Refresh and pick its adapter (usually marked [VPN])."))
        self.require = QCheckBox("Only allow torrents through this adapter (strongly recommended)")
        self.require.setChecked(bool(self.cfg["require_vpn"]))
        self.dht = QCheckBox("Use DHT to find peers (needed for magnet links without trackers)")
        self.dht.setChecked(bool(self.cfg["dht"]))
        lay.addWidget(self.require)
        lay.addWidget(self.dht)
        lay.addWidget(hint("Always on: anonymous mode (no client name, no address sent to trackers), UPnP / NAT-PMP / "
                           "local-network discovery off, IPv4 on the VPN only, and no seeding - each torrent is left "
                           "as soon as your files are done."))
        save = accent_button("Save")
        save.setMinimumHeight(32)
        save.setDefault(True)
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        lay.addLayout(_button_row(save, cancel))
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll)
        QTimer.singleShot(0, self._refresh)

    def _refresh(self) -> None:
        """Listing adapters starts PowerShell (a second or two), so it runs off the UI thread."""
        if self._scanning:
            return
        self._scanning = True
        self.refresh_btn.setEnabled(False)
        self.combo.clear()
        self.combo.addItem("Looking for network adapters...")
        threading.Thread(target=lambda: self._box.put(list_adapters()), daemon=True).start()
        self._poll_timer.start()

    def _poll(self) -> None:
        try:
            found = self._box.get_nowait()
        except queue.Empty:
            return
        self._poll_timer.stop()
        self._scanning = False
        self.refresh_btn.setEnabled(True)
        self.adapters = sorted(found, key=lambda a: (not a["vpn"], a["name"].lower()))
        self.combo.clear()
        for a in self.adapters:
            self.combo.addItem(f'{"[VPN]  " if a["vpn"] else ""}{a["name"]}  -  {a["ip"]}')
        idx = next((i for i, a in enumerate(self.adapters) if a["name"] == self.cfg["adapter"]),
                   0 if self.adapters and self.adapters[0]["vpn"] else -1)
        self.combo.setCurrentIndex(idx)

    def _save(self) -> None:
        idx = self.combo.currentIndex() if not self._scanning else -1
        chosen = self.adapters[idx] if 0 <= idx < len(self.adapters) else None
        yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        if self.require.isChecked():
            if not chosen:
                QMessageBox.critical(self, "Choose an adapter", "Connect your VPN, press Refresh and pick its adapter.")
                return
            if not chosen["vpn"] and QMessageBox.warning(
                    self, "Not a VPN?", f"'{chosen['name']}' doesn't look like a VPN adapter. If it's your normal "
                    "connection, peers will see your real IP address.\n\nUse it anyway?", yes | no, no) != yes:
                return
        elif QMessageBox.warning(
                self, "No VPN protection", "Without the VPN requirement, peers in the swarm can see your real IP "
                "address.\n\nContinue without it?", yes | no, no) != yes:
            return
        save_torrent_settings({"adapter": chosen["name"] if chosen else "", "require_vpn": self.require.isChecked(),
                               "dht": self.dht.isChecked()})
        self.ok = True
        self.accept()


class SummaryDialog(QDialog):
    """End-of-batch report when something failed: what went wrong, per stage, with a one-click retry."""

    def __init__(self, parent: QWidget, summary: BatchSummary, report: str, on_retry: Callable[[frozenset], None]):
        super().__init__(parent)
        self.setWindowTitle("Batch complete - some items failed")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(700, 540)
        failures = summary.failures
        failed_sources = {f.source for f in failures}
        retry_paths = frozenset(f.source for f in failures if f.retryable)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        head = QLabel(f"Batch complete: {summary.total - len(failed_sources)} of {summary.total} item(s) succeeded, "
                      f"{len(failed_sources)} failed.")
        head.setObjectName("phase")
        head.setWordWrap(True)
        lay.addWidget(head)
        note = QLabel("Your original files were NOT deleted for any item that failed - everything that errored is "
                      "still exactly where it was.")
        note.setWordWrap(True)
        lay.addWidget(note)
        if report:
            rep = QLabel(report)
            rep.setObjectName("mono")
            rep.setWordWrap(True)
            rep.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(rep)
        card = Card("What went wrong")
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setObjectName("log")
        card.add(details)
        lay.addWidget(card, 1)
        head_fmt, why_fmt = QTextCharFormat(), QTextCharFormat()
        head_fmt.setFontWeight(700)
        head_fmt.setForeground(T.accent)
        why_fmt.setForeground(T.fg3)
        plain = QTextCharFormat()
        cur = details.textCursor()
        for stage, title in STAGE_TITLES.items():
            items = [f for f in failures if f.stage == stage]
            if not items:
                continue
            cur.insertText(f"{title} ({len(items)})\n", head_fmt)
            for f in items:
                cur.insertText(f"  \u2022 {f.name}\n", plain)
                extra = "" if f.retryable else "  [retrying won't help - change a setting first]"
                cur.insertText(f"      {f.reason}{extra}\n", why_fmt)
        details.moveCursor(QTextCursor.MoveOperation.Start)
        retry = accent_button(f"Retry Failed ({len(retry_paths)})")
        retry.setMinimumHeight(32)
        retry.setEnabled(bool(retry_paths))
        retry.clicked.connect(lambda: (self.close(), on_retry(retry_paths)))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        lay.addLayout(_button_row(retry, close))
