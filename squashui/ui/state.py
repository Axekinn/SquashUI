"""Definitions shared by the window and its page mixins: tool table, per-page state."""
from __future__ import annotations

import shutil
from typing import Callable, TYPE_CHECKING

from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

from .widgets import PathRow

if TYPE_CHECKING:
    from ..organize import Finding, OrgTool


# --- The window -----------------------------------------------------------------------------------
# The conversion tools the window knows about: event prefix -> (display name, names to look for on PATH).
TOOLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chdman": ("chdman", ("chdman",)),
    "dolphin": ("DolphinTool", ("dolphin-tool", "DolphinTool")),
    "maxcso": ("maxcso", ("maxcso",)),
    "shadplus": ("ShadPS4Plus PKG_Extractor", ("pkg_extractor",)),
    "anyps5": ("pkg-to-anyps5", ("pkg-to-anyps5",)),
    "zarchive": ("ZArchive", ("zarchive",)),
    "ps3qdd": ("PS3QDD (PS3Dec)", ("ps3dec",)),
    "xdvdfs": ("xdvdfs", ("xdvdfs",)),
    "relinker": ("the AnyPS5 relinker", ()),
}


# The Settings window lists every tool once, in two groups: tool key -> label.
TOOL_GROUPS: dict[str, dict[str, str]] = {
    "Disc image tools": {"chdman": "CHDMan", "dolphin": "DolphinTool", "maxcso": "maxcso", "xdvdfs": "xdvdfs"},
    "PlayStation tools": {"shadplus": "ShadPS4Plus PKG_Extractor", "anyps5": "pkg-to-anyps5",
                          "relinker": "AnyPS5 relinker", "zarchive": "ZArchive (.zar)", "ps3qdd": "PS3Dec (PS3QDD)"},
}


LABEL_W = 124


def _which_any(names: tuple[str, ...]) -> str:
    for name in names:
        if found := shutil.which(name):
            return found
    return ""


class TabUI:
    """The widgets of one conversion page, so CHD, RVZ, CSO ... each remember their own folders and options."""

    def __init__(self, mode: str, always_checked: bool):
        self.source = PathRow("Source folder", label_width=LABEL_W)
        self.dest = PathRow("Destination folder", label_width=LABEL_W)
        self.extract = QCheckBox("Extract archives first")
        self.extract.setChecked(True)
        self.verify = QCheckBox()
        self.verify.setChecked(True)
        if always_checked:
            self.verify.setEnabled(False)
        self.delete = QCheckBox()


class OrgState:
    """One Organize page: its last scan, what is ticked, its options and its widgets."""

    def __init__(self, tool: OrgTool):
        self.tool = tool
        self.findings: list[Finding] = []
        self.checked: set[int] = set()
        self.token = 0
        self.message = ""
        self.opts: dict[str, Callable[[], object]] = {}
        self.tree: CheckTree | None = None
        self.status: QLabel | None = None
        self.scan_btn: QPushButton | None = None
        self.undo_btn: QPushButton | None = None
