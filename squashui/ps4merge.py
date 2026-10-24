"""Merging PS4 updates into their base game: a param.sfo writer / merger, the update-to-game matching done when a batch
is scanned, and the file-level overlay that lays an extracted update over an extracted game. The extraction itself is
the PKG extractor's job; everything here works on its output (and on the packages' own headers).

Nothing here runs a tool or touches the UI, so it can be tested on its own."""
from __future__ import annotations

import os
import re
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .formats import read_pkg_info, read_pkg_sfo

# ---- param.sfo ------------------------------------------------------------------------------------------------
SFO_MAGIC = b"\0PSF"
SFO_FMT_TEXT_RAW, SFO_FMT_TEXT, SFO_FMT_INT = 0x0004, 0x0204, 0x0404  # utf-8 no NUL, utf-8 with NUL, uint32

# What an update changes in the game's param.sfo. Everything else (CONTENT_ID, CATEGORY "gd", TITLE, the icon /
# trophy / entitlement fields ...) stays the base game's, so the merged folder is still a game and not a patch.
MERGE_KEYS = ("APP_VER", "VERSION", "SYSTEM_VER", "PUBTOOLINFO")


@dataclass(slots=True)
class SfoEntry:
    key: str
    fmt: int
    max_len: int
    data: bytes  # exactly what is stored (a text value includes its NUL for SFO_FMT_TEXT)

    @property
    def text(self) -> str:
        return self.data.split(b"\0")[0].decode("utf-8", "replace")


def read_sfo(blob: bytes) -> list[SfoEntry] | None:
    """Every entry of a PSF / param.sfo blob, in file order; None if it isn't one."""
    try:
        magic, _ver, key_table, data_table, count = struct.unpack_from("<4sIIII", blob, 0)
        if magic != SFO_MAGIC or count > 512:
            return None
        out = []
        for i in range(count):
            key_off, fmt, length, max_len, data_off = struct.unpack_from("<HHIII", blob, 20 + 16 * i)
            end = blob.index(b"\0", key_table + key_off)
            key = blob[key_table + key_off:end].decode("ascii", "replace")
            data = blob[data_table + data_off:data_table + data_off + length]
            if len(data) != length:
                return None
            out.append(SfoEntry(key, fmt, max_len, data))
        return out
    except (struct.error, ValueError):
        return None


def write_sfo(entries: list[SfoEntry]) -> bytes:
    """A PSF blob: keys sorted (a PSF requires it), the key table padded to 4, each value padded to its max length."""
    entries = sorted(entries, key=lambda e: e.key)
    keys, key_offsets = bytearray(), []
    for e in entries:
        key_offsets.append(len(keys))
        keys += e.key.encode("ascii") + b"\0"
    while len(keys) % 4:
        keys += b"\0"
    key_table = 20 + 16 * len(entries)
    data_table = key_table + len(keys)
    index, data, offset = bytearray(), bytearray(), 0
    for e, key_off in zip(entries, key_offsets):
        max_len = max(e.max_len, len(e.data))
        index += struct.pack("<HHIII", key_off, e.fmt, len(e.data), max_len, offset)
        data += e.data + b"\0" * (max_len - len(e.data))
        offset += max_len
    return struct.pack("<4sIIII", SFO_MAGIC, 0x101, key_table, data_table, len(entries)) + bytes(index) + bytes(keys) + bytes(data)


def merge_sfo(base: bytes, updates: list[bytes], keys: tuple[str, ...] = MERGE_KEYS) -> bytes:
    """The base game's param.sfo with the version fields of the updates (oldest first, so the newest wins).
    Raises ValueError when the base or an update isn't a readable param.sfo."""
    merged = read_sfo(base)
    if merged is None:
        raise ValueError("the game's param.sfo is not readable")
    by_key = {e.key: e for e in merged}
    for n, blob in enumerate(updates, 1):
        entries = read_sfo(blob)
        if entries is None:
            raise ValueError(f"update {n}'s param.sfo is not readable")
        for e in entries:
            if e.key in keys:
                by_key[e.key] = SfoEntry(e.key, e.fmt, e.max_len, e.data)
    return write_sfo(list(by_key.values()))


# ---- matching updates to their game ---------------------------------------------------------------------------
def version_key(version: str) -> tuple[int, ...]:
    """'01.05' -> (1, 5): update versions compare numerically, not as text."""
    return tuple(int(n) for n in re.findall(r"\d+", version))


def attach_updates(sources: list, log: Callable[[str], None]) -> list:
    """Fold every update (category gp) into the base game (gd) of the same title ID, when both are in `sources`.
    The game's Source gets the update paths in `attached` (oldest first); the updates stop being sources of their
    own. An update whose game isn't there stays a source and is extracted on its own. Returns the new list."""
    bases: dict[str, object] = {}
    updates: list[tuple[object, str, str]] = []
    for src in sources:
        if src.is_archive or src.path.suffix.lower() != ".pkg":
            continue
        info = read_pkg_info(src.path)
        if info is None or not info.title_id:
            continue
        if info.category == "gd":
            bases.setdefault(info.title_id, src)
        elif info.category == "gp":
            updates.append((src, info.title_id, info.version))
    consumed = set()
    for src, title_id, version in sorted(updates, key=lambda u: (u[1], version_key(u[2]))):
        base = bases.get(title_id)
        if base is not None:
            base.attached.append(src.path)
            consumed.add(id(src))
            log(f"{base.path.name}: update v{version or '?'} ({src.path.name}) will be merged into it.\n")
    return [s for s in sources if id(s) not in consumed]


# ---- extracted folders ----------------------------------------------------------------------------------------
# A Content ID (UP0000-CUSA00001_00-ABCDEFGHIJKLMNOP) or a title ID (CUSA00001): what an extractor names its wrapper folder.
_ID_RE = re.compile(r"^(?:[A-Z]{2}\d{4}-)?[A-Z]{4}\d{5}(?:_\d{2}(?:-[A-Z0-9]{16})?)?$", re.IGNORECASE)


def _is_game(folder: Path) -> bool:
    return (folder / "sce_sys").is_dir() or (folder / "eboot.bin").is_file()


def extracted_root(folder: Path) -> Path | None:
    """The folder holding the files of what an extractor wrote: the folder itself, or the one <Content ID> / <title ID>
    subfolder it put them in. A patch that only carries data (no eboot.bin, no sce_sys) must not have its own data/
    folder mistaken for a wrapper, so a lone subfolder counts as one only by its name or by what it holds.
    None when nothing was extracted."""
    root = folder
    try:
        for _ in range(2):
            if _is_game(root):
                break
            kids = [c for c in root.iterdir() if c.name.lower() != "debug_log.txt"]
            if len(kids) == 1 and kids[0].is_dir() and (_ID_RE.match(kids[0].name) or _is_game(kids[0])):
                root = kids[0]
            else:
                break
        return root if any(p.is_file() for p in root.rglob("*")) else None
    except OSError:
        return None


def overlay_move(update_root: Path, game_root: Path) -> int:
    """Lay an extracted update over an extracted game: each update file replaces the game's file of that name (new
    ones are added). Files are moved, not copied, so on one drive this is instant. Returns how many files moved."""
    moved = 0
    for f in [p for p in update_root.rglob("*") if p.is_file()]:
        target = game_root / f.relative_to(update_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        try:
            os.replace(f, target)
        except OSError:  # another drive
            shutil.move(str(f), target)
        moved += 1
    return moved


def merged_sfo_for(game_pkg: Path, update_pkgs: list[Path]) -> bytes:
    """The merged param.sfo, built from the param.sfo each package carries in its own header (so it doesn't depend
    on how the extractor laid its folders out). Raises ValueError when one can't be read."""
    base = read_pkg_sfo(game_pkg)
    if base is None:
        raise ValueError(f"{game_pkg.name} has no readable param.sfo")
    blobs = []
    for p in update_pkgs:
        blob = read_pkg_sfo(p)
        if blob is None:
            raise ValueError(f"{p.name} has no readable param.sfo")
        blobs.append(blob)
    return merge_sfo(base, blobs)
