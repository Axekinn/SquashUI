"""One-time carry-over of settings and folders from the pre-v10.1 (qbSquash) names."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .constants import APP_DIR, APP_NAME, LEGACY_APP_NAME
from .downloads import LOGIN_FILE, SESSION_FILE
from .torrents import TORRENT_DIR, TORRENT_FILE


def migrate_legacy_data() -> None:
    """v10.1 renamed qbSquash to SquashUI. Move the saved session, logins, torrent list and undo records (and the
    tools folder under %LOCALAPPDATA%) to their new names, once, so nothing is lost. Never overwrites."""
    renames = [(APP_DIR / "qbsquash_download_session.json", SESSION_FILE),
               (APP_DIR / "qbsquash_logins.json", LOGIN_FILE),
               (APP_DIR / "qbsquash_torrent_settings.json", TORRENT_FILE),
               (APP_DIR / "qbsquash_torrents", TORRENT_DIR)]
    renames += [(old, APP_DIR / old.name.replace("qbsquash_", "squashui_", 1)) for old in APP_DIR.glob("qbsquash_undo_*.json")]
    if local := os.environ.get("LOCALAPPDATA"):
        renames.append((Path(local) / LEGACY_APP_NAME, Path(local) / APP_NAME))
    for old, new in renames:
        if old.exists() and not new.exists():
            with contextlib.suppress(OSError):
                old.rename(new)
