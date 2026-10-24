"""Settings window and tool paths."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame,
    QHBoxLayout, QLabel, QListWidget, QPushButton,
    QRadioButton, QScrollArea, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import magnet
from ..constants import (
    CHD_COMPRESSION_CHOICES, CSO_SPEED_CHOICES, PKG_LAYOUTS, PKG_MISSING_HINT,
    PKG_TOOLS, PS3_OUTPUTS, RVZ_DEFAULT_LEVEL,
    RVZ_MAX_LEVEL, TOOL_MISSING_TEXT,
)
from ..downloads import DL_MAX_PARALLEL
from ..models import IO_MODES
from ..system import portable_path, resolve_dolphintool, resolve_tool_path
from .state import LABEL_W, TOOL_GROUPS, TOOLS
from .widgets import Card, Expander, hint, PathRow, ToolRow


class SettingsMixin:
    """Settings window and tool paths: where each external tool lives and whether it can be run.

    Methods only; the state lives on SquashUIWindow, which mixes this in."""

    def _make_settings_widgets(self) -> None:
        def spin(lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
            box = QSpinBox()
            box.setRange(lo, hi)
            box.setValue(value)
            box.setSuffix(suffix)
            box.setMinimumWidth(90)
            box.setKeyboardTracking(False)
            return box

        def combo(items, current: str | None = None) -> QComboBox:
            box = QComboBox()
            box.addItems(list(items))
            if current is not None:
                box.setCurrentText(current)
            box.setMinimumWidth(180)
            return box

        self.chd_compression = combo(CHD_COMPRESSION_CHOICES)
        self.cso_speed = combo(CSO_SPEED_CHOICES)
        self.rvz_scrub = QCheckBox("Scrub junk data (smaller files, but not bit-for-bit identical)")
        self.rvz_level = spin(1, RVZ_MAX_LEVEL, RVZ_DEFAULT_LEVEL)
        self.thread_budget = spin(1, self.sys_threads, self.sys_threads)  # low priority makes "all cores" safe
        self.io_mode = combo(IO_MODES)
        self.io_mode.setMinimumWidth(340)
        self.temp_dir = PathRow("Temp folder", label_width=150)
        self.ps3_keys = PathRow("Keys folder", label_width=LABEL_W)
        self.ps3_output = combo(PS3_OUTPUTS)
        self.pkg_layout = combo(PKG_LAYOUTS)
        self.pkg_merge = QCheckBox("Merge each game's updates into it")  # PS4 page: one patched folder per game
        self.pkg_zar = QCheckBox("Convert to .zar after extraction")  # PS4 page: pack each extraction into a .zar
        self.ps5_anyps5 = QRadioButton("AnyPS5  -  source/ + app0/, relinked into app.exe with libs/")
        self.ps5_kyty = QRadioButton("KytyPS5  -  the game files with eboot.bin at the root, packed into a .zar")
        self.ps5_anyps5.setChecked(True)

    def _build_perf_card(self) -> Expander:
        card = Expander("Performance && CPU budget")
        lay = card.body_lay

        def row(label: str, widget: QWidget, note: str = "") -> None:
            h = QHBoxLayout()
            h.setSpacing(10)
            lbl = QLabel(label)
            lbl.setFixedWidth(150)
            h.addWidget(lbl)
            h.addWidget(widget)
            if note:
                n = hint(note)
                h.addWidget(n, 1)
            else:
                h.addStretch(1)
            lay.addLayout(h)

        row("Total thread budget", self.thread_budget,
            f"of {self.sys_threads} system threads - dynamically split across tasks")
        row("Disk I/O mode", self.io_mode)
        lay.addWidget(self.temp_dir)
        lay.addWidget(hint("Optional. Extract archives to a RAM disk or SSD instead of the system temp folder. "
                           "It needs room for about two unpacked archives."))
        return card

    def _tool_text(self, key: str) -> str:
        rows = self._tool_rows[key]
        return rows[0].path.text().strip() if rows else ""

    def _open_settings(self, page: int = 0) -> None:
        self.settings_list.setCurrentRow(page)
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()

    def _tool_missing(self, text: str) -> None:
        """A needed tool isn't set up: say so, then open Settings on the Tools page."""
        self._error("Executable Not Found", text + "\n\nTool paths are in Settings (the gear at the bottom left).")
        self._open_settings(0)

    def _build_settings_dialog(self) -> None:
        dlg = self.settings_dlg = QDialog(self)  # created once and kept, so its fields hold their values
        dlg.setWindowTitle("Settings")
        dlg.resize(780, 660)
        dlg.setMinimumSize(640, 480)
        root = QHBoxLayout(dlg)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        nav = self.settings_list = QListWidget()
        nav.setFixedWidth(190)
        nav.setFrameShape(QFrame.Shape.NoFrame)
        nav.setSpacing(2)
        nav.addItems(["Tools", "Downloads & accounts"])
        stack = QStackedWidget()
        nav.currentRowChanged.connect(stack.setCurrentIndex)
        root.addWidget(nav)
        root.addWidget(stack, 1)

        def page(title: str) -> tuple[QVBoxLayout, QWidget]:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            inner = QWidget()
            lay = QVBoxLayout(inner)
            lay.setContentsMargins(24, 20, 24, 20)
            lay.setSpacing(10)
            head = QLabel(title)
            head.setObjectName("dialogHead")
            lay.addWidget(head)
            scroll.setWidget(inner)
            stack.addWidget(scroll)
            return lay, inner

        # -- Tools: every conversion tool once; the app finds and updates its own copies in ./tools
        lay, _ = page("Tools")
        lay.addWidget(hint("Each tool is found and kept up to date automatically in the tools folder next to this app. "
                           "Browse only if you want to use a different copy. The dots in the side panel show which "
                           "tools are ready."))
        for group, tools in TOOL_GROUPS.items():
            card = Card(group)
            for key, label in tools.items():
                row = ToolRow(label, 150)
                self._register_tool_row(key, row)
                card.add(row)
            lay.addWidget(card)
        lay.addStretch(1)

        # -- Downloads & accounts
        lay, _ = page("Downloads & accounts")
        card = Card("Downloading")
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(QLabel("Parallel downloads"))
        self.dl_threads = QSpinBox()
        self.dl_threads.setRange(1, DL_MAX_PARALLEL)
        self.dl_threads.setValue(4)
        self.dl_threads.setMinimumWidth(80)
        self.dl_threads.valueChanged.connect(self._dl_threads_changed)
        row.addWidget(self.dl_threads)
        row.addStretch(1)
        card.add(row)
        self.dl_verify = QCheckBox("Verify checksums")
        self.dl_verify.setChecked(True)
        self.dl_folders = QCheckBox("Folder per item")
        for box in (self.dl_verify, self.dl_folders):
            box.toggled.connect(lambda *_: self._dl_save_soon())
            card.add(box)
        lay.addWidget(card)

        def action_card(title: str, text: str, button: str, fn) -> None:
            c = Card(title)
            c.add(hint(text))
            b = QPushButton(button)
            b.clicked.connect(lambda _c=False: fn())
            h = QHBoxLayout()
            h.addWidget(b)
            h.addStretch(1)
            c.add(h)
            lay.addWidget(c)

        if sys.platform == "win32":
            card = Card("Magnet links")
            card.add(hint("Let magnet links from your browser and other programs open in SquashUI's Download page. "
                          "This is per-user and needs no administrator rights."))
            self.magnet_box = QCheckBox("Open magnet links with SquashUI")
            self.magnet_box.setChecked(magnet.is_registered())
            self.magnet_box.toggled.connect(self._magnet_toggled)
            card.add(self.magnet_box)
            self.magnet_status = hint("")
            card.add(self.magnet_status)
            b = QPushButton("Open Windows default apps...")
            b.clicked.connect(lambda _c=False: magnet.open_default_apps())
            h = QHBoxLayout()
            h.addWidget(b)
            h.addStretch(1)
            card.add(h)
            lay.addWidget(card)
            self._magnet_refresh()

        action_card("Logins", "Sign-in details for archive.org, 1fichier, TorBox and any other file host. With a "
                              "TorBox API key, torrents are looked up in TorBox's cache first: cached files download "
                              "straight from TorBox over HTTPS, and only what isn't cached falls back to torrenting.",
                    "Logins...", self._logins_dialog)
        action_card("Torrent privacy", "Used for torrents that aren't cached on TorBox (or for everyone not using "
                                       "TorBox): ties torrent traffic to your VPN adapter so your real IP address is "
                                       "never exposed to the swarm.", "Torrent privacy...", self._torrent_dialog)
        lay.addStretch(1)
        nav.setCurrentRow(0)

    def _magnet_refresh(self) -> None:
        if not magnet.is_registered():
            self.magnet_status.setText("")
        elif magnet.is_default():
            self.magnet_status.setText("Magnet links open in SquashUI.")
        else:
            self.magnet_status.setText("Registered, but Windows still uses another app for magnet links. Open Default "
                                       "apps below and choose SquashUI for MAGNET.")

    def _magnet_toggled(self, on: bool) -> None:
        try:
            (magnet.register if on else magnet.unregister)()
        except OSError as exc:
            self.magnet_box.blockSignals(True)
            self.magnet_box.setChecked(not on)
            self.magnet_box.blockSignals(False)
            self._error("Magnet links", f"Couldn't {'register' if on else 'remove'} the magnet handler: {exc}")
        self._magnet_refresh()

    def _register_tool_row(self, key: str, row: ToolRow) -> None:
        row.path.setText(self._auto[key])
        row.status.setText(f"Checking for {TOOLS[key][0]}...")
        if existing := self._tool_rows[key]:  # a second page showing the same tool starts in step with the first
            row.path.setText(existing[0].path.text())
            row.status.setText(existing[0].status.text())
        self._tool_rows[key].append(row)
        row.path.edit.textEdited.connect(lambda text, k=key: self._set_tool_text(k, text, user=True))
        row.path.edit.textChanged.connect(lambda *_: self._refresh_nav_dots())

    def _set_tool_text(self, key: str, text: str, user: bool = False) -> None:
        for row in self._tool_rows[key]:
            if row.path.edit.text() != text:
                row.path.setText(text)

    def _set_tool_status(self, key: str, text: str) -> None:
        for row in self._tool_rows[key]:
            row.status.setText(text)

    def _tool_path_found(self, key: str, path: str) -> None:
        if self._tool_text(key) in ("", self._auto[key]):  # never overwrite a manual choice
            self._set_tool_text(key, path)
        self._auto[key] = path
        if key == "ps3qdd":
            keys = Path(resolve_tool_path(path)).parent / "Keys"
            if keys.is_dir() and self.ps3_keys.text().strip() in ("", self._auto_ps3_keys):
                self._auto_ps3_keys = portable_path(keys)
                self.ps3_keys.setText(self._auto_ps3_keys)

    def _tool_ready(self, key: str) -> None:
        self._pending_tools.discard(key)
        self._refresh_nav_dots()
        if not self._pending_tools and not self.is_processing and not self.org_busy:
            self.start_btn.setEnabled(True)

    def _missing_tool(self, path: str, text: str) -> bool:
        """True (after telling the person) when `path` isn't a runnable tool."""
        if path and shutil.which(path):
            return False
        self._tool_missing(text)
        return True

    def _resolved_tools(self) -> dict[str, str]:
        """Every tool path as the engine should run it (./tools/... is anchored to the app folder)."""
        tools = {key: resolve_tool_path(self._clean(self._tool_text(key))) for key in TOOLS}
        tools["dolphin"] = resolve_dolphintool(self._clean(self._tool_text("dolphin")))  # also takes Dolphin.exe / its folder
        return tools

    def _tools_ok(self, mode: str, engine_mode: str, tools: dict[str, str], *, pkg_tool: str, relink: bool,
                  packs_zar: bool, ps3_keys: str) -> bool:
        """Check that the tools this page needs exist; on a miss, say so and open Settings."""
        if mode == "rvz":
            if not tools["dolphin"]:
                self._tool_missing(TOOL_MISSING_TEXT["rvz"])
                return False
        elif mode in ("chd", "cso", "xbox", "ps3"):
            key = {"chd": "chdman", "cso": "maxcso", "xbox": "xdvdfs", "ps3": "ps3qdd"}[mode]
            if self._missing_tool(tools[key], TOOL_MISSING_TEXT[mode]):
                return False
        if mode == "ps3" and ps3_keys and not Path(ps3_keys).is_dir():
            self._error("Error", f"The PS3 keys folder does not exist.\n\nPath checked: {ps3_keys}")
            return False
        if engine_mode == "pkg" and self._missing_tool(
                tools[pkg_tool], f"Could not locate the '{PKG_TOOLS[pkg_tool]}' executable.\n\n{PKG_MISSING_HINT[pkg_tool]}"):
            return False
        if relink and self._missing_tool(tools["relinker"], TOOL_MISSING_TEXT["relinker"]):
            return False
        return not (packs_zar and self._missing_tool(tools["zarchive"], TOOL_MISSING_TEXT["zar"]))
