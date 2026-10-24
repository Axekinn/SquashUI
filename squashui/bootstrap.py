"""First-run dependency installer (skipped in a packaged build)."""
from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

from .constants import APP_NAME, SUBPROCESS_FLAGS


def ensure_dependencies() -> None:
    """Install anything missing in a single pip call (no imports needed to check). Does nothing in a packaged
    build, which already contains everything."""
    if getattr(sys, "frozen", False):
        return
    import importlib.util

    required = {"py7zr": "py7zr", "PySide6": "PySide6>=6.7", "libtorrent": "libtorrent"}  # 6.7+ ships the native Windows 11 style
    missing = [pip for mod, pip in required.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *missing],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=SUBPROCESS_FLAGS,
        )
    except Exception:
        pass  # the app degrades gracefully (no .7z fallback, no swarm torrents); PySide6 is checked again on import


def fatal(message: str) -> None:
    """Tell the person something stopped the app from starting (a message box on Windows, else stderr)."""
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)
    else:
        print(message, file=sys.stderr)


def missing_qt_message(exc: ImportError) -> str:
    return (f"{APP_NAME} needs PySide6 (6.7 or newer) for its window.\n\nOpen a command prompt and run:\n\n"
            f"    {Path(sys.executable).name} -m pip install \"PySide6>=6.7\"\n\n({exc})")
