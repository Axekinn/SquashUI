"""Run with:  python -m squashui   (or double-click SquashUI.pyw)."""
from __future__ import annotations

import sys


def run() -> int:
    from .bootstrap import ensure_dependencies, fatal, missing_qt_message

    ensure_dependencies()  # the first start of a source checkout installs PySide6 and py7zr
    try:
        from .app import main
    except ImportError as exc:
        if (exc.name or "").split(".")[0] != "PySide6":
            raise  # a real bug, not a missing dependency
        fatal(missing_qt_message(exc))
        return 1
    return main()


if __name__ == "__main__":
    sys.exit(run())
