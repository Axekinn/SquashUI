"""Finding files with identical contents, however they are named.

Reading every byte of a library would take hours, so candidates are narrowed in steps, each far cheaper than the next:
size, then a fingerprint of the file's start and end, then (only for what still matches) a full SHA-1. CHD files skip
all that: version 5 CHDs carry a SHA-1 of the disc's raw data in their header, the same for any compression setting,
so two CHDs of one disc are recognised by reading 124 bytes each. No Qt in here."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Callable

from .models import fmt_size


MIN_BYTES = 64 * 1024  # smaller files are never worth checksumming: tiny files match by accident (empty .cue, stubs)


EDGE_BYTES = 1 << 20  # how much of each end the quick fingerprint reads


CHUNK = 4 << 20


CHD_V5_RAW_SHA1 = slice(64, 84)  # offset of 'rawsha1' in a version 5 header


def chd_raw_sha1(path: Path) -> str | None:
    """SHA-1 of the disc's raw data from a v5 CHD header (same whatever compression made the file); None if unreadable."""
    try:
        with path.open("rb") as f:
            head = f.read(124)
    except OSError:
        return None
    if len(head) < 124 or head[:8] != b"MComprHD" or int.from_bytes(head[12:16], "big") != 5:
        return None
    raw = head[CHD_V5_RAW_SHA1]
    return raw.hex() if any(raw) else None


def quick_fingerprint(path: Path, size: int) -> str | None:
    """Hash of the first and last megabyte plus the size: tells different files apart without reading them whole."""
    h = hashlib.sha1(str(size).encode())
    try:
        with path.open("rb") as f:
            h.update(f.read(EDGE_BYTES))
            if size > 2 * EDGE_BYTES:
                f.seek(size - EDGE_BYTES)
                h.update(f.read(EDGE_BYTES))
    except OSError:
        return None
    return h.hexdigest()


def full_sha1(path: Path, stop: threading.Event) -> str | None:
    """SHA-1 of the whole file; None if it couldn't be read or the scan was stopped."""
    h = hashlib.sha1()
    try:
        with path.open("rb") as f:
            while chunk := f.read(CHUNK):
                if stop.is_set():
                    return None
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _split(paths: list[Path], key: Callable[[Path], str | None]) -> list[list[Path]]:
    """Groups of 2+ paths that share a key (paths whose key is None drop out)."""
    groups: dict[str, list[Path]] = {}
    for p in paths:
        if (k := key(p)) is not None:
            groups.setdefault(k, []).append(p)
    return [g for g in groups.values() if len(g) > 1]


def find_same_content(paths: list[Path], stop: threading.Event,
                      note: Callable[[str], None] = lambda _t: None) -> list[list[Path]]:
    """Groups of files with identical contents (each group has 2 or more). Stops early, with what it has, when asked."""
    sizes: dict[Path, int] = {}
    for p in paths:
        try:
            sizes[p] = p.stat().st_size
        except OSError:
            pass
    out: list[list[Path]] = []

    chds = [p for p in sizes if p.suffix.lower() == ".chd"]
    by_content = {p: chd_raw_sha1(p) for p in chds}
    out += _split([p for p in chds if by_content[p]], lambda p: "chd:" + by_content[p])
    rest = [p for p in sizes if sizes[p] >= MIN_BYTES and not (p in by_content and by_content[p])]

    by_size = _split(rest, lambda p: str(sizes[p]))
    candidates = [p for g in by_size for p in g]
    note(f"{len(candidates)} file(s) share a size with another; comparing their starts and ends...")
    quick: dict[Path, str | None] = {}
    for n, p in enumerate(candidates, 1):
        if stop.is_set():
            return out
        quick[p] = quick_fingerprint(p, sizes[p])
    finalists = [g for grp in by_size for g in _split(grp, lambda p: quick.get(p))]

    todo = [p for g in finalists for p in g]
    for n, p in enumerate(todo, 1):
        if stop.is_set():
            return out
        note(f"Checksumming {n} of {len(todo)}: {p.name} ({fmt_size(sizes[p])})")
        quick[p] = full_sha1(p, stop)  # reuse the dict: from here it holds full hashes
    for g in finalists:
        out += _split(g, lambda p: quick.get(p))
    return out
