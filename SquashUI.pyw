"""Double-click launcher (no console window). Everything lives in the squashui package beside this file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from squashui.__main__ import run  # noqa: E402

sys.exit(run())
