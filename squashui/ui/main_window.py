"""The main window: layout, sidebar navigation and theming."""
from __future__ import annotations

import os
import queue
import shutil
import threading
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFrame,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QToolTip,
    QVBoxLayout, QWidget,
)

from ..constants import APP_NAME, APP_TAGLINE, APP_VERSION, RVZ_DEFAULT_LEVEL, RVZ_MAX_LEVEL
from ..downloads import DownloadRunner, load_logins
from ..organize import ORG_TOOLS
from ..system import resolve_tool_path
from ..tools import (
    AnyPS5Manager, ChdmanManager, DolphinToolManager, MaxCsoManager,
    PkgExtractorManager, PkgToAnyPs5Manager, Ps3QddManager,
    XdvdfsManager, ZArchiveManager,
)
from .download_page import DownloadMixin
from .icon import make_app_icon
from .models import DlModel, DlProxy
from .monitor import ResourceMonitor
from .nav import NAV_GROUPS, PAGE_INFO
from .organize_page import OrganizeMixin
from .run_panel import RunMixin
from .settings_page import SettingsMixin
from .state import _which_any, LABEL_W, TabUI, TOOLS
from .theme import T
from .widgets import accent_button, Card, Expander, GearButton, hint, Logo, NavButton, NavHeader, SizedStack

if TYPE_CHECKING:
    from ..batch import BatchRunner
    from ..models import Config


class SquashUIWindow(RunMixin, OrganizeMixin, DownloadMixin, SettingsMixin, QMainWindow):
    MODES = ("chd", "rvz", "cso", "pkg", "pkg5", "zar", "ps3", "xbox")  # pkg5: the PS5 page, which runs the "pkg" engine mode
    SIDEBAR_WIDTH = 268
    FOOTER_H = 64  # sidebar footer and command bar share this height so their top borders line up
    PAGE_MARGIN = 28

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION} - {APP_TAGLINE}")
        self.setWindowIcon(make_app_icon())
        self.resize(1240, 880)
        self.setMinimumSize(1000, 600)
        self.setAcceptDrops(True)  # .torrent files and magnet links dropped on the Download page

        self.is_processing = False
        self.is_paused = False
        self._stopping = False
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.runner: BatchRunner | DownloadRunner | None = None
        self._last_cfg: Config | None = None
        self._work = (0, 0)  # (bytes done, bytes to do) from the runner
        self._samples: deque[tuple[float, int]] = deque(maxlen=12)  # (active seconds, bytes done)
        self._t0: float | None = None
        self._paused_total, self._pause_t, self._eta_tick = 0.0, None, 0.0
        self._settings_visible = True
        self._theming = False
        self._qss = ""
        self.ui_q: queue.SimpleQueue = queue.SimpleQueue()
        self.sys_threads = os.cpu_count() or 4
        self._tasks: dict[int, list[int]] = {}  # log task id -> [first block, line count]

        # tool paths: one setting per tool, mirrored in every page that shows it
        self._tool_rows: dict[str, list[ToolRow]] = {key: [] for key in TOOLS}
        self._auto: dict[str, str] = {key: _which_any(names) for key, (_n, names) in TOOLS.items()}  # last value the app (not the user) put in the box
        self._auto_ps3_keys = ""
        self._pending_tools = set(TOOLS)

        self.current_page = "chd"
        self.tabs: dict[str, TabUI] = {}
        self._make_settings_widgets()
        self.nav_items: dict[str, NavButton] = {}
        self.nav_groups: dict[str, dict] = {}

        # Organisation pages share one folder; each tool keeps its own results and options
        self.org: dict[str, OrgState] = {}
        self.org_busy = False
        self._org_stop = threading.Event()
        self._org_sources: list[PathRow] = []
        self._org_recursive: list[QCheckBox] = []

        # Download page
        self.dl_remember = load_logins()  # logins are asked for when a download starts
        self._dl_token = 0  # bumped by every Fetch, so late replies for an old list are ignored
        self._dl_next: list[int] = []  # rows lined up to start when the current run ends
        self._dl_epoch = 0  # bumped whenever rows are renumbered, so a slow start-up disk scan can't mark the wrong ones
        self._dl_cleared = False  # the person emptied the list (so the saved copy goes too)
        self._dl_samples: deque[tuple[float, int]] = deque()
        self.dl_model, self.dl_proxy = DlModel(), DlProxy()
        self.dl_proxy.setSourceModel(self.dl_model)
        self._dl_filter_timer = QTimer(self)
        self._dl_filter_timer.setSingleShot(True)
        self._dl_filter_timer.setInterval(250)  # debounce: one rebuild after typing pauses
        self._dl_filter_timer.timeout.connect(self._dl_apply_filter)
        self._dl_save_timer = QTimer(self)
        self._dl_save_timer.setSingleShot(True)
        self._dl_save_timer.setInterval(1500)
        self._dl_save_timer.timeout.connect(self._dl_save_session)

        self._build_ui()
        self._apply_theme()
        self._dl_restore_session()
        self.start_btn.setEnabled(False)  # enabled once the tool checks have finished
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_events)
        self._poll.start(50)
        QApplication.styleHints().colorSchemeChanged.connect(lambda *_: QTimer.singleShot(60, self._apply_theme))
        for manager in (ChdmanManager, DolphinToolManager, MaxCsoManager, PkgExtractorManager,
                        PkgToAnyPs5Manager, ZArchiveManager, Ps3QddManager, XdvdfsManager, AnyPS5Manager):
            threading.Thread(target=manager(self.ui_q.put).run, daemon=True).start()

    def _apply_theme(self) -> None:
        if self._theming:
            return
        self._theming = True
        try:
            T.refresh()
            if (qss := T.qss()) != self._qss:
                self._qss = qss
                QApplication.instance().setStyleSheet(qss)
            for nav in self.nav_items.values():
                nav.update()
            if hasattr(self, "settings_nav"):
                self.settings_nav.update()
            for header in (g["header"] for g in self.nav_groups.values()):
                header.update()
            self.centralWidget().update()
        finally:
            self._theming = False

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ThemeChange) and not self._theming:
            QTimer.singleShot(0, self._apply_theme)
        super().changeEvent(event)

    def _info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        return QMessageBox.question(self, title, text, yes | no, yes) == yes

    @staticmethod
    def _clean(text: str) -> str:
        return text.strip(" \"'")

    def closeEvent(self, event) -> None:
        if self.org_busy and not self._ask(
                "Confirm Exit", "Games are still being moved into folders.\n\nExit anyway? (Use Undo Last Run "
                                "next time to reverse a half-finished run.)"):
            event.ignore()
            return
        if self.is_processing:
            if not self._ask(
                    "Confirm Exit",
                    "A batch is currently running!\n\nClosing now will instantly abort it. "
                    + ("Partly downloaded files are kept and resume next time. Are you sure?"
                       if isinstance(self.runner, DownloadRunner) else
                       "Active conversions are stopped and partial files deleted. Are you sure?")):
                event.ignore()
                return
            if self.runner:
                self.runner.shutdown()
        self._dl_save_session()
        event.accept()
        self.hide()
        QApplication.processEvents()
        os._exit(0)  # worker threads may still be inside a (non-interruptible) extraction

    def _build_ui(self) -> None:
        self._build_settings_dialog()  # tool paths and download options live here, not on each page
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        root.addWidget(content, 1)

        # header: page title and blurb, and the settings fold-away
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(self.PAGE_MARGIN, 22, self.PAGE_MARGIN, 10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_blurb = QLabel()
        self.page_blurb.setObjectName("muted")
        self.page_blurb.setWordWrap(True)
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_blurb)
        hl.addLayout(titles, 1)
        self.settings_btn = QPushButton("Hide options")
        self.settings_btn.setToolTip("Fold away this page's options to give the log more room")
        self.settings_btn.clicked.connect(lambda: self._set_settings_visible(not self._settings_visible))
        hl.addWidget(self.settings_btn, 0, Qt.AlignmentFlag.AlignTop)
        col.addWidget(header)

        # settings (top) and the live run panel (bottom) share the space through a splitter
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        self.stack = SizedStack()
        self.pages: dict[str, QWidget] = {mode: self._build_tab(mode) for mode in self.MODES}
        for tool in ORG_TOOLS.values():
            self.pages[tool.key] = self._build_org_page(tool)
        self.pages["dl"] = self._build_dl_page()
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.perf = self._build_perf_card()
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 10, 4)
        il.setSpacing(10)
        il.addWidget(self.stack)
        il.addWidget(self.perf)
        il.addStretch(1)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setWidget(inner)
        self.splitter.addWidget(self.settings_scroll)
        self.splitter.addWidget(self._build_run_panel())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([480, 320])
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(self.PAGE_MARGIN, 0, self.PAGE_MARGIN - 8, 14)
        wl.addWidget(self.splitter)
        col.addWidget(wrap, 1)

        # command bar
        bar = QWidget()
        bar.setObjectName("commandBar")
        bl = QHBoxLayout(bar)
        bar.setFixedHeight(self.FOOTER_H)
        bl.setContentsMargins(self.PAGE_MARGIN, 12, self.PAGE_MARGIN, 12)
        bl.setSpacing(10)
        self.start_btn = accent_button("Start Batch Conversion")
        self.start_btn.clicked.connect(self._start_processing)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setMinimumHeight(38)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setMinimumHeight(38)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_run)
        bl.addWidget(self.start_btn, 3)
        bl.addWidget(self.pause_btn, 1)
        bl.addWidget(self.stop_btn, 1)
        col.addWidget(bar)
        for seq, button in (("Ctrl+Return", self.start_btn), ("Ctrl+P", self.pause_btn), ("Ctrl+.", self.stop_btn)):
            QShortcut(QKeySequence(seq), self, activated=button.click)  # click() is a no-op while disabled
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self._open_settings())
        self.start_btn.setToolTip("Ctrl+Enter")
        self.pause_btn.setToolTip("Ctrl+P: finish active jobs, start no new ones")
        self.stop_btn.setToolTip("Ctrl+.: abort the run (finished items are kept)")

        self._refresh_nav_dots()
        self._show_page(self.current_page)

    def _set_settings_visible(self, visible: bool) -> None:
        if visible == self._settings_visible or (not visible and not self._log_shown):
            return  # pages without a log keep their options on screen
        self._settings_visible = visible
        self.settings_scroll.setVisible(visible)
        self.settings_btn.setText("Hide options" if visible else "Show options")

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(self.SIDEBAR_WIDTH)
        outer = QVBoxLayout(side)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(10, 20, 4, 0)
        lay.setSpacing(0)
        outer.addWidget(content, 1)

        brand = QHBoxLayout()
        brand.setContentsMargins(8, 0, 0, 12)
        brand.setSpacing(12)
        brand.addWidget(Logo())
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setObjectName("brandName")
        count = len([k for k in self.pages_order() if not k.startswith("act_")])
        sub = QLabel(f"{APP_VERSION}  |  {count} tools")
        sub.setObjectName("hint")
        names.addWidget(name)
        names.addWidget(sub)
        brand.addLayout(names)
        brand.addStretch(1)
        lay.addLayout(brand)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 6, 0)
        il.setSpacing(1)
        group_role = {"CONVERT": "accent", "ORGANISE": "group_org", "DOWNLOAD": "group_dl"}
        for group, items in NAV_GROUPS:
            header = NavHeader(group)
            body = QWidget()
            bl = QVBoxLayout(body)
            bl.setContentsMargins(0, 0, 0, 4)
            bl.setSpacing(1)
            self.nav_groups[group] = {"header": header, "body": body}
            header.clicked.connect(lambda _c=False, g=group: self._toggle_group(g))
            il.addWidget(header)
            il.addWidget(body)
            for key, text in items:
                if key.startswith("act_"):  # Logins / Torrent privacy now live in Settings
                    continue
                item = NavButton(key, text, group_role[group])
                item.clicked.connect(lambda _c=False, k=key: self._nav_click(k))
                self.nav_items[key] = item
                bl.addWidget(item)
        il.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        self.settings_nav = GearButton("")
        self.settings_nav.setToolTip("Settings: tool paths, downloads, logins and torrent privacy (Ctrl+,)")
        self.settings_nav.clicked.connect(lambda: self._open_settings())
        footer = QWidget()
        footer.setObjectName("sideFooter")
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        footer.setFixedHeight(self.FOOTER_H)
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(10, 0, 6, 0)
        bottom.setSpacing(2)
        bottom.addWidget(ResourceMonitor(), 1)
        bottom.addWidget(self.settings_nav)
        outer.addWidget(footer)
        self._refresh_nav_dots()
        return side

    @staticmethod
    def pages_order() -> list[str]:
        return [key for _group, items in NAV_GROUPS for key, _text in items]

    def _nav_click(self, key: str) -> None:
        if self.is_processing:  # the Start button belongs to the running batch until it ends
            QToolTip.showText(QCursor.pos(), "Locked while a run is active. Stop it or wait for it to finish.", self)
            return
        self._show_page(key)

    def _toggle_group(self, group: str) -> None:
        g = self.nav_groups[group]
        g["header"].open = not g["header"].open
        g["body"].setVisible(g["header"].open)
        g["header"].update()

    def _set_badge(self, key: str, count: int | str) -> None:
        self.nav_items[key].badge = str(count) if count else ""
        self.nav_items[key].update()

    def _refresh_nav_dots(self) -> None:
        """Green when a conversion tool is ready, amber while it is still being checked, red when it is missing."""
        if not self.nav_items:
            return
        for mode, tool in (("chd", "chdman"), ("rvz", "dolphin"), ("cso", "maxcso"), ("pkg", "shadplus"),
                           ("pkg5", "anyps5"), ("zar", "zarchive"), ("ps3", "ps3qdd"), ("xbox", "xdvdfs")):
            path = self._tool_text(tool)
            ready = bool(path) and (Path(resolve_tool_path(path)).is_file() or bool(shutil.which(path)))
            if mode in self.nav_items:
                self.nav_items[mode].state = "ok" if ready else "warn" if tool in self._pending_tools else "bad"
                self.nav_items[mode].update()

    def _show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        self.current_page = key
        self.stack.setCurrentWidget(self.pages[key])
        title, blurb = PAGE_INFO[key]
        self.page_title.setText(title)
        self.page_blurb.setText(blurb)
        for group, items in NAV_GROUPS:  # make sure the group holding this page is expanded
            if any(k == key for k, _t in items) and not self.nav_groups[group]["header"].open:
                self._toggle_group(group)
        for k, item in self.nav_items.items():
            item.selected = k == key
            item.update()
        self._refresh_start_label()

    def _current_mode(self) -> str:
        page = self.current_page
        return page if page in self.MODES or page == "dl" else "org"

    def _refresh_start_label(self) -> None:
        page, mode = self.current_page, self._current_mode()
        self.start_btn.setText(
            "Start Download" if mode == "dl" else ORG_TOOLS[page].apply_label if mode == "org"
            else f"Start {({'pkg': 'PS4 PKG', 'pkg5': 'PS5 PKG'}).get(mode, mode.upper())} Batch Conversion")
        if not self.is_processing and not self.org_busy:  # the idle label follows the page
            self.lbl_total_prog.setText("Download Progress: 0 / 0" if mode == "dl" else
                                        "Organize Progress: 0 / 0" if mode == "org" else "Batch Progress: 0 / 0")
        self.perf.setVisible(mode not in ("dl", "org"))
        self._apply_panel_mode(mode)

    def _build_tab(self, mode: str) -> QWidget:
        kind = mode.upper()
        is_pkg_page = mode in ("pkg", "pkg5")
        always_checked = is_pkg_page or mode == "ps3"  # these pages check every result, so the Verify box is fixed
        tv = self.tabs[mode] = TabUI(mode, always_checked)
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        dirs = Expander("Folders", expanded=True)
        dirs.add(tv.source)
        dirs.add(tv.dest)
        blank_note = ("Leave Destination blank to extract each PKG into a folder next to it." if is_pkg_page
                      else "Leave Destination blank to save each .zar next to its game folder." if mode == "zar"
                      else "Leave Destination blank to save each result next to its ISO." if mode == "ps3"
                      else f"Leave Destination blank to save {kind}s directly alongside their source files.")
        dirs.add(hint(blank_note))
        extra = {
            "pkg": "Only PS4 .pkg files (and archives containing them) are picked up; PS5, PS3 and Vita packages "
                   "are skipped.",
            "pkg5": "Only PS5 debug .pkg files (and archives containing them) are picked up. Retail or encrypted "
                    "packages can't be read: they are flagged and left untouched.",
            "zar": "Every PS4 game, update or DLC folder under the source (one with sce_sys/param.sfo or eboot.bin) "
                   "becomes its own .zar named after the folder. Other folders are skipped.",
            "xbox": "Only Xbox and Xbox 360 .iso discs (and archives containing them) are picked up; other .iso "
                    "files are skipped.",
            "ps3": "Only PS3 .iso discs (and archives containing them) are picked up. Each needs its Redump .dkey, "
                   "named like the ISO, next to it or in the Keys folder below.",
            "cso": "Only PSP .iso discs (and archives containing them) are picked up here; other .iso files are "
                   "skipped.",
        }.get(mode)
        if extra:
            dirs.add(hint(extra))
        lay.addWidget(dirs)

        opts = Expander("Workflow", expanded=True)
        if mode != "zar":  # folders are packed as they are
            opts.add(tv.extract)
        tv.verify.setText({
            "chd": "Verify each CHD right after conversion (runs in parallel)",
            "rvz": "Verify each RVZ right after conversion (SHA-1 compared with the source)",
            "cso": "Verify each CSO right after conversion (decompressed and compared with the source)",
            "pkg": "Check each extraction (title files present) - always on",
            "pkg5": "Check each extraction (app0/ or source/ written) - always on",
            "zar": "Verify each .zar after packing (unpacked again and every file compared with the folder; slow)",
            "ps3": "Check each result (PARAM.SFO readable, files complete) - always on",
            "xbox": "Verify each XISO right after conversion (content checksum compared with the source ISO)",
        }[mode])
        tv.delete.setText("Delete the original folders after they are packed successfully" if mode == "zar"
                          else "Delete original files after successful conversion/verification")
        opts.add(tv.verify)
        opts.add(tv.delete)
        lay.addWidget(opts)

        def form(card: Card, label: str, widget: QWidget, note: str = "", label_w: int = LABEL_W) -> None:
            h = QHBoxLayout()
            h.setSpacing(10)
            lbl = QLabel(label)
            lbl.setFixedWidth(label_w)
            h.addWidget(lbl)
            h.addWidget(widget)
            if note:
                h.addWidget(hint(note), 1)
            else:
                h.addStretch(1)
            card.add(h)

        if mode == "chd":
            card = Expander("CHD settings", expanded=True)
            form(card, "Compression", self.chd_compression,
                 "Default = smallest, slowest  |  Fast = quicker  |  Fastest = quickest, little saving  |  "
                 "None = no compression")
            lay.addWidget(card)
        if mode == "cso":
            card = Expander("CSO settings", expanded=True)
            form(card, "Compression", self.cso_speed, "Default = smallest, slowest  |  Fast = much quicker, a little larger")
            lay.addWidget(card)
        if mode == "rvz":
            card = Expander("RVZ settings", expanded=True)
            card.add(self.rvz_scrub)
            card.add(hint("With scrubbing on, verification only checks the RVZ reads back cleanly."))
            form(card, "Compression level", self.rvz_level,
                 f"zstd 1-{RVZ_MAX_LEVEL}, higher = smaller but slower; default {RVZ_DEFAULT_LEVEL}")
            lay.addWidget(card)
        if mode == "ps3":
            card = Expander("PS3 settings", expanded=True)
            card.add(self.ps3_keys)
            form(card, "Output", self.ps3_output)
            card.add(hint("Keys: a .dkey beside the ISO wins, else one with the same name in the Keys folder (PS3QDD's "
                          "own Keys folder is used automatically).  Results are named 'Game Name - Title ID' from "
                          "PARAM.SFO.  A loose folder needs room for the decrypted ISO as well while it is unpacked "
                          "(a temp folder from Performance is used if set)."))
            lay.addWidget(card)
        if mode == "pkg":
            card = Expander("PS4 PKG settings", expanded=True)
            form(card, "Folder layout", self.pkg_layout)
            card.add(hint("One folder per PKG: named after each file.  |  shadPS4 layout: a game in CUSAxxxxx, its "
                          "update in CUSAxxxxx-UPDATE, DLC in addcont/CUSAxxxxx/<label>."))
            card.add(self.pkg_merge)
            card.add(hint("A game and the updates for it in the source folder (same title ID) become one patched game "
                          "folder: each update is laid over the game in version order and the game's param.sfo takes the "
                          "newest version. The update .pkg files are used up (deleted with the game if you ticked that); "
                          "an update with no game in the folder is extracted on its own."))
            card.add(self.pkg_zar)
            card.add(hint("Each extraction is checked, packed into one <name>.zar with ZArchive (shadPS4 0.17+ reads "
                          "it directly) and the extracted folder is removed."))
            lay.addWidget(card)
        if mode == "pkg5":
            card = Expander("PS5 PKG settings", expanded=True)
            lbl = QLabel("What is this extraction for?")
            lbl.setObjectName("cardTitle")
            card.add(lbl)
            card.add(self.ps5_anyps5)
            card.add(hint("AnyPS5's relinker makes app.exe from source/eboot.bin and its system libraries are copied "
                          "into libs/. If the relink fails the extraction is kept and the .pkg is not deleted."))
            card.add(self.ps5_kyty)
            card.add(hint("Kyty reads a .zar directly. app0/ is packed into <name>.zar and the extraction folder is "
                          "removed; source/ is not kept. Room is needed for the full extraction and the .zar at once."))
            group = QButtonGroup(self)
            group.addButton(self.ps5_anyps5)
            group.addButton(self.ps5_kyty)
            lay.addWidget(card)
        lay.addStretch(1)  # spare height goes below the cards, never into them
        page.setProperty("compact", True)  # SizedStack pins the page's height to its cards
        return page

    DL_COLS = {"size": 2, "source": 3, "status": 4, "speed": 5}
