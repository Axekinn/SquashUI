"""Reading disc, package and ISO formats: GameCube/Wii/PSP/Xbox/PS3 detection, PS4/PS5 PKG headers and
layouts, PS3 ISO extraction, CSO verification."""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .constants import (
    APP_DIR, CUE_FILE_RE, DOLPHIN_ONLY_EXTENSIONS, DVD_ISO_MIN_BYTES,
    GDI_FILE_RE, ILLEGAL_NAME_CHARS,
)
from .system import file_size


def is_gamecube_wii_image(path: Path) -> bool:
    """True for GameCube/Wii discs: known containers, or an .iso whose header carries Dolphin's magic."""
    ext = path.suffix.lower()
    if ext in DOLPHIN_ONLY_EXTENSIONS:
        return True
    if ext != ".iso":
        return False
    try:
        with path.open("rb") as f:
            header = f.read(0x20)
    except OSError:
        return False
    # Wii magic at 0x18, GameCube magic at 0x1C
    return len(header) == 0x20 and (header[0x18:0x1C] == b"\x5d\x1c\x9e\xa3" or header[0x1C:0x20] == b"\xc2\x33\x9f\x3d")


def associated_files(sheet: Path) -> set[Path]:
    """The sheet itself plus the track files it references (for post-conversion cleanup)."""
    files = {sheet}
    ext = sheet.suffix.lower()
    pattern = {".cue": CUE_FILE_RE, ".gdi": GDI_FILE_RE}.get(ext)
    if pattern is None:
        return files
    try:
        root = sheet.parent.resolve()
        with sheet.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if m := pattern.match(line):
                    track = sheet.parent / (m[1] or m[2])
                    # never delete anything outside the sheet's own folder tree
                    if track.is_file() and track.resolve().is_relative_to(root):
                        files.add(track)
    except OSError:
        pass
    return files


def _iso_root_entries(f) -> dict[str, tuple[int, int, bool]]:
    """Name -> (first sector, size, is_dir) for the ISO 9660 root directory."""
    f.seek(16 * 2048)  # primary volume descriptor
    pvd = f.read(2048)
    if len(pvd) < 190 or pvd[0] != 1 or pvd[1:6] != b"CD001":
        return {}
    extent = int.from_bytes(pvd[158:162], "little")
    size = int.from_bytes(pvd[166:170], "little")
    f.seek(extent * 2048)
    data = f.read(min(size, 1 << 20))
    entries, pos = {}, 0
    while pos + 33 <= len(data):
        length = data[pos]
        if length == 0:  # records never straddle sectors; skip the padding
            pos = (pos // 2048 + 1) * 2048
            continue
        rec = data[pos:pos + length]
        name = rec[33:33 + rec[32]].decode("latin-1").split(";")[0].upper()
        entries[name] = (int.from_bytes(rec[2:6], "little"), int.from_bytes(rec[10:14], "little"), bool(rec[25] & 2))
        pos += length
    return entries


def iso_wants_dvd_mode(path: Path) -> bool:
    """PSP and PS2 discs belong in createdvd; PS1 and other CD data images in createcd."""
    try:
        with path.open("rb") as f:
            entries = _iso_root_entries(f)
            if "PSP_GAME" in entries:
                return True
            if cnf := entries.get("SYSTEM.CNF"):
                f.seek(cnf[0] * 2048)
                return b"BOOT2" in f.read(min(cnf[1], 4096)).upper()  # PS1 uses plain BOOT
        return path.stat().st_size > DVD_ISO_MIN_BYTES
    except OSError:
        return False


def is_psp_image(path: Path) -> bool:
    """True for an ISO 9660 image with a PSP_GAME folder in its root (a PSP UMD dump)."""
    try:
        with path.open("rb") as f:
            return "PSP_GAME" in _iso_root_entries(f)
    except OSError:
        return False


def verify_cso(cso: Path, source: Path, stop: threading.Event,
               progress: Callable[[float], None] | None = None) -> str | None:
    """Decompress a CSO (v1) block by block and compare it with the source ISO, byte for byte.

    maxcso has no verify command, so this is the lossless proof. Returns None when the CSO reproduces
    the source exactly, otherwise a short reason.
    """
    last = -1.0
    try:
        with cso.open("rb") as f, source.open("rb") as src:
            head = f.read(24)
            if len(head) < 24 or head[:4] != b"CISO":
                return "not a valid CSO file (bad header)"
            total = int.from_bytes(head[8:16], "little")
            block = int.from_bytes(head[16:20], "little")
            version, shift = head[20], head[21]
            if version > 1:
                return f"unsupported CSO version {version}"
            if block <= 0 or total <= 0:
                return "CSO header is damaged"
            count = (total + block - 1) // block
            raw = f.read((count + 1) * 4)
            if len(raw) != (count + 1) * 4:
                return "CSO block index is truncated"
            index = struct.unpack(f"<{count + 1}I", raw)
            done = 0
            for i in range(count):
                if stop.is_set():
                    return "aborted"
                start, end = index[i], index[i + 1]
                a, b = (start & 0x7FFFFFFF) << shift, (end & 0x7FFFFFFF) << shift
                f.seek(a)
                data = f.read(b - a)
                if start & 0x80000000:  # stored uncompressed (may carry alignment padding)
                    out = data[:block]
                else:
                    out = zlib.decompressobj(-15).decompress(data)
                want = min(block, total - done)
                out = out[:want]
                if len(out) != want:
                    return f"block {i} decompressed to the wrong size"
                if src.read(want) != out:
                    return f"data differs from the source near byte {done:,}"
                done += want
                if progress and (pct := done * 100 / total) - last >= 1.0:
                    last = pct
                    progress(pct)
            if src.read(1):
                return "the source is longer than the CSO's data"
    except (OSError, zlib.error) as exc:
        return f"could not read back the CSO: {exc}"
    return None


# ---- PS4 PKG packages --------------------------------------------------------------------------------------
PKG_MAGIC = b"\x7fCNT"  # PS4 packages; PS3 / Vita packages start with b"\x7fPKG"


PS5_PKG_MAGIC = b"\x7fFIH"  # PS5 debug packages (what pkg-to-anyps5 reads)


PKG_ENTRY_PARAM_SFO = 0x1000  # the unencrypted param.sfo inside the package's entry table


_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@dataclass(frozen=True, slots=True)
class PkgInfo:
    """What a PS4 package says about itself (read from the file's own param.sfo, without extracting anything)."""
    content_id: str
    title_id: str  # CUSA12345
    category: str  # gd = game, gp = update (patch), ac = additional content (DLC)
    title: str
    version: str

    @property
    def kind(self) -> str:
        return {"gd": "game", "gp": "update", "ac": "DLC"}.get(self.category, self.category or "package")

    @property
    def label(self) -> str:
        """A DLC's folder name: the last 16 characters of its content ID (the entitlement label)."""
        return self.content_id[20:]


def parse_sfo(data: bytes) -> dict[str, str | int] | None:
    """Read a PSF / param.sfo blob into {key: value}; None if it isn't one."""
    try:
        magic, _ver, key_table, data_table, count = struct.unpack_from("<4sIIII", data, 0)
        if magic != b"\0PSF" or count > 512:
            return None
        out: dict[str, str | int] = {}
        for i in range(count):
            key_off, fmt, length, _max, data_off = struct.unpack_from("<HHIII", data, 20 + 16 * i)
            key = data[key_table + key_off:data.index(b"\0", key_table + key_off)].decode("ascii", "replace")
            raw = data[data_table + data_off:data_table + data_off + length]
            out[key] = struct.unpack("<I", raw[:4])[0] if fmt == 0x0404 else raw.split(b"\0")[0].decode("utf-8", "replace")
        return out
    except (struct.error, ValueError):
        return None


def is_ps4_pkg(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == PKG_MAGIC
    except OSError:
        return False


def is_ps5_pkg(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == PS5_PKG_MAGIC
    except OSError:
        return False


def _pkg_sfo_blob(f, head: bytes) -> bytes | None:
    """The unencrypted param.sfo from a PS4 package's entry table (`f` is the open file, `head` its first 0x80 bytes)."""
    count, table = struct.unpack_from(">I", head, 0x10)[0], struct.unpack_from(">I", head, 0x18)[0]
    if not 0 < count <= 8192:
        return None
    f.seek(table)
    entries = f.read(32 * count)
    for i in range(len(entries) // 32):
        entry_id, _name, _f1, _f2, offset, size = struct.unpack_from(">6I", entries, 32 * i)
        if entry_id == PKG_ENTRY_PARAM_SFO and 0 < size <= 1 << 20:
            f.seek(offset)
            return f.read(size)
    return None


def read_pkg_sfo(path: Path) -> bytes | None:
    """The raw param.sfo of a PS4 .pkg, without extracting anything; None if it can't be read."""
    try:
        with path.open("rb") as f:
            head = f.read(0x80)
            if len(head) < 0x80 or head[:4] != PKG_MAGIC:
                return None
            return _pkg_sfo_blob(f, head)
    except (OSError, struct.error):
        return None


def read_pkg_info(path: Path) -> PkgInfo | None:
    """PkgInfo from a PS4 .pkg (header, entry table, then its param.sfo); None if it can't be read."""
    try:
        with path.open("rb") as f:
            head = f.read(0x80)
            if len(head) < 0x80 or head[:4] != PKG_MAGIC:
                return None
            content_id = head[0x40:0x64].split(b"\0")[0].decode("ascii", "replace")
            blob = _pkg_sfo_blob(f, head)
    except OSError:
        return None
    sfo = parse_sfo(blob) if blob else None
    if not sfo:
        return None
    version = str(sfo.get("APP_VER") or sfo.get("VERSION") or "")
    return PkgInfo(content_id, str(sfo.get("TITLE_ID") or content_id[7:16]), str(sfo.get("CATEGORY") or ""),
                   str(sfo.get("TITLE") or ""), version)


def pkg_target(info: PkgInfo | None, stem: str, out_dir: Path, layout: str) -> Path:
    """Where a package's files end up. 'shadps4' follows shadPS4's own layout: a game in CUSAxxxxx, its update in
    CUSAxxxxx-UPDATE beside it, DLC under addcont/CUSAxxxxx/<label>. Anything unrecognised falls back to a
    folder named after the file."""
    if layout == "shadps4" and info and _SAFE_ID.fullmatch(info.title_id):
        if info.category == "gd":
            return out_dir / info.title_id
        if info.category == "gp":
            return out_dir / f"{info.title_id}-UPDATE"
        if info.category == "ac" and _SAFE_ID.fullmatch(info.label):
            return out_dir / "addcont" / info.title_id / info.label
    return out_dir / (ILLEGAL_NAME_CHARS.sub("", stem).strip(" .") or "package")


def locate_pkg_root(folder: Path) -> Path:
    """The folder that actually holds the game's files. Some extractors put them in a <Content ID> subfolder."""
    def looks_like_game(d: Path) -> bool:
        # app0/ + source/ is what pkg-to-anyps5 writes for a PS5 package; they must not be mistaken for a wrapper
        return ((d / "sce_sys").is_dir() or (d / "eboot.bin").is_file()
                or (d / "app0").is_dir() or (d / "source").is_dir())

    try:
        if looks_like_game(folder):
            return folder
        kids = [c for c in folder.iterdir() if c.name.lower() != "debug_log.txt"]
        if len(kids) == 1 and kids[0].is_dir():
            return kids[0]
    except OSError:
        pass
    return folder


def find_anyps5_libs(relinker: Path) -> Path | None:
    """The folder of AnyPS5 system libraries (.prx) that belongs to a relinker: beside it (./tools/anyps5/<tag>/libs),
    or where an AnyPS5 source build puts them (build/core/libs/libs, with the relinker in build/core/relinker)."""
    for cand in (relinker.parent / "libs", relinker.parent.parent / "libs" / "libs", relinker.parent.parent.parent / "libs"):
        try:
            if cand.is_dir() and any(cand.glob("*.prx")):
                return cand
        except OSError:
            continue
    return None


def is_ps4_game_folder(d: Path) -> bool:
    """A loose, already-extracted PS4 game / update / DLC folder: it has its sce_sys/param.sfo or its eboot.bin."""
    try:
        return (d / "sce_sys" / "param.sfo").is_file() or (d / "eboot.bin").is_file()
    except OSError:
        return False


def tree_manifest(root: Path) -> dict[str, int]:
    """{relative path (lower case, as ZArchive looks names up): size} of every file under a folder."""
    return {p.relative_to(root).as_posix().lower(): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def compare_trees(source: Path, unpacked: Path) -> str | None:
    """None when `unpacked` holds exactly the files of `source` at the same sizes, otherwise what differs."""
    try:
        want, got = tree_manifest(source), tree_manifest(unpacked)
    except OSError as exc:
        return f"the files couldn't be compared: {exc}"
    missing = sorted(k for k in want if k not in got)
    extra = sorted(k for k in got if k not in want)
    wrong = sorted(k for k in want if k in got and want[k] != got[k])
    if not (missing or extra or wrong):
        return None
    parts = [f"{len(items)} file(s) {what} (first: {items[0]})"
             for items, what in ((missing, "missing from the .zar"), (wrong, "of a different size"),
                                 (extra, "not in the folder")) if items]
    return "the .zar does not match the folder: " + "; ".join(parts)


def check_pkg_folder(root: Path, info: PkgInfo | None, ps5: bool = False) -> str | None:
    """A cheap sanity check of an extraction: None if it looks complete, otherwise why it doesn't. An extractor may
    report success even when it couldn't decrypt everything, so this runs every time."""
    try:
        if not any(p.is_file() for p in root.rglob("*")):
            return "nothing was extracted"
        if ps5:  # pkg-to-anyps5 writes source/ (relinker input) and app0/ (the game files)
            if not ((root / "app0").is_dir() or (root / "source").is_dir()):
                return "neither app0/ nor source/ was written - the extraction looks incomplete"
            return None
        if info is not None and info.category == "gd":  # a base game always has its executable
            eboot = root / "eboot.bin"
            if not eboot.is_file() or eboot.stat().st_size == 0:
                return "eboot.bin is missing or empty - the extraction looks incomplete"
    except OSError as exc:
        return f"the extracted files couldn't be read: {exc}"
    return None


def park_tool_log(folder: Path, logs: Path | None, stem: str) -> None:
    """Some extractors write a debug_log.txt; keep it out of the game folder (and under a name that says whose it is)."""
    try:
        candidates = [folder / "debug_log.txt", *(c / "debug_log.txt" for c in folder.iterdir() if c.is_dir())]
        for log in candidates:
            if log.is_file():
                if logs:
                    shutil.move(str(log), str(logs / f"{stem}.log"))
                else:
                    log.unlink()
                return
    except OSError:
        pass


# --- Xbox discs: XISO packing with xdvdfs ----------------------------------------------
# The XDVDFS volume starts at sector 32 of the game partition. A Redump image carries a video partition first, so
# the partition begins at a fixed offset that depends on the disc generation; a rebuilt XISO begins at 0.
XDVDFS_MAGIC = b"MICROSOFT*XBOX*MEDIA"


XDVDFS_OFFSETS = (0x00000000, 0x18300000, 0x0FD90000, 0x02080000)  # XISO, XGD1, XGD2, XGD3


def is_xbox_image(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            for offset in XDVDFS_OFFSETS:
                f.seek(offset + 32 * PS3_SECTOR)
                if f.read(len(XDVDFS_MAGIC)) == XDVDFS_MAGIC:
                    return True
    except OSError:
        pass
    return False


# --- PS3 discs: decrypting with PS3Dec (shipped in PS3QDD) and unpacking the ISO ---------
PS3_SECTOR = 2048


PS3_NAME_CACHE = APP_DIR / "ps3_names.json"  # source ISO name + size -> "Game Name - Title ID" (lets a re-run skip finished titles)


_PS3_CACHE_LOCK = threading.Lock()


def _ps3_cache_key(path: Path) -> str:
    return f"{path.name.lower()}|{file_size(path)}"


def ps3_cached_name(path: Path) -> str | None:
    with _PS3_CACHE_LOCK:
        try:
            data = json.loads(PS3_NAME_CACHE.read_text("utf-8"))
        except (OSError, ValueError):
            return None
    name = data.get(_ps3_cache_key(path)) if isinstance(data, dict) else None
    return name if isinstance(name, str) and name else None


def ps3_remember_name(path: Path, name: str) -> None:
    with _PS3_CACHE_LOCK:
        try:
            data = json.loads(PS3_NAME_CACHE.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[_ps3_cache_key(path)] = name
        try:
            tmp = PS3_NAME_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=0), "utf-8")
            os.replace(tmp, PS3_NAME_CACHE)
        except OSError:
            pass  # only a convenience


@dataclass(frozen=True, slots=True)
class IsoEntry:
    name: str
    is_dir: bool
    extents: tuple[tuple[int, int], ...]  # (first sector, bytes) of each piece; files over 4 GB have several

    @property
    def size(self) -> int:
        return sum(length for _sector, length in self.extents)


def _iso_pvd_root(f) -> tuple[int, int] | None:
    """(sector, bytes) of the ISO 9660 root directory."""
    f.seek(16 * PS3_SECTOR)
    pvd = f.read(PS3_SECTOR)
    if len(pvd) < 190 or pvd[0] != 1 or pvd[1:6] != b"CD001":
        return None
    return int.from_bytes(pvd[158:162], "little"), int.from_bytes(pvd[166:170], "little")


def _iso_read_dir(f, extent: int, size: int) -> list[IsoEntry]:
    f.seek(extent * PS3_SECTOR)
    data = f.read(min(size, 256 << 20))
    entries: list[IsoEntry] = []
    pending: tuple[str, bool, list[tuple[int, int]]] | None = None
    pos = 0
    while pos + 33 <= len(data):
        length = data[pos]
        if length == 0:  # records never straddle sectors; skip the padding
            pos = (pos // PS3_SECTOR + 1) * PS3_SECTOR
            continue
        rec = data[pos:pos + length]
        pos += length
        if length < 34 or 33 + rec[32] > length:
            break
        raw = rec[33:33 + rec[32]]
        if raw in (b"\x00", b"\x01"):  # "." and ".."
            continue
        is_dir = bool(rec[25] & 2)
        name = raw.decode("latin-1").split(";")[0]
        if not is_dir and name.endswith("."):
            name = name[:-1]
        piece = (int.from_bytes(rec[2:6], "little"), int.from_bytes(rec[10:14], "little"))
        if pending is None:
            pending = (name, is_dir, [])
        pending[2].append(piece)
        if rec[25] & 0x80:  # more extents of this file follow
            continue
        entries.append(IsoEntry(pending[0], pending[1], tuple(pending[2])))
        pending = None
    return entries


def _iso_lookup(f, parts: list[str]) -> IsoEntry | None:
    root = _iso_pvd_root(f)
    if root is None:
        return None
    extent, size = root
    found = None
    for i, part in enumerate(parts):
        found = next((e for e in _iso_read_dir(f, extent, size) if e.name.upper() == part.upper()), None)
        if found is None or (i < len(parts) - 1 and not found.is_dir):
            return None
        extent, size = found.extents[0]
    return found


def read_ps3_sfo(iso: Path) -> dict[str, str | int] | None:
    """PS3_GAME/PARAM.SFO of a (decrypted) PS3 disc image; None when it can't be read - e.g. still encrypted."""
    try:
        with iso.open("rb") as f:
            entry = _iso_lookup(f, ["PS3_GAME", "PARAM.SFO"])
            if entry is None or entry.is_dir or not 0 < entry.size <= 1 << 20:
                return None
            data = b""
            for sector, length in entry.extents:
                f.seek(sector * PS3_SECTOR)
                data += f.read(length)
            return parse_sfo(data)
    except (OSError, IndexError, ValueError, OverflowError, MemoryError):
        return None


def is_ps3_image(path: Path) -> bool:
    """A PS3 disc image, decrypted or not: the volume descriptor and the root folder are never encrypted."""
    try:
        with path.open("rb") as f:
            f.seek(16 * PS3_SECTOR)
            pvd = f.read(PS3_SECTOR)
            if len(pvd) >= 72 and pvd[1:6] == b"CD001" and pvd[40:72].rstrip(b" \0").upper().startswith(b"PS3VOLUME"):
                return True
            entries = _iso_root_entries(f)
        return "PS3_GAME" in entries or "PS3_DISC.SFB" in entries
    except (OSError, IndexError, ValueError):
        return False


def ps3_game_name(sfo: dict | None, fallback: str) -> str:
    """"Game Name - Title ID" from PARAM.SFO, made safe for a file or folder name."""
    def tidy(text) -> str:
        text = re.sub(r"\s+", " ", str(text or "").replace(":", " -"))  # (a newline in the title becomes a space)
        return re.sub(r"\s+", " ", ILLEGAL_NAME_CHARS.sub("", text)).strip(" .")

    title, title_id = tidy((sfo or {}).get("TITLE")), tidy((sfo or {}).get("TITLE_ID"))
    base = title or tidy(fallback) or "PS3 game"
    return f"{base} - {title_id}" if title_id else base


def ps3_known_name(path: Path) -> str | None:
    """The final name of a disc, if it is already known without decrypting (cached, or a decrypted image)."""
    if name := ps3_cached_name(path):
        return name
    sfo = read_ps3_sfo(path)
    return ps3_game_name(sfo, path.stem) if sfo else None


def read_ps3_key(path: Path) -> str | None:
    """A disc key from a .dkey / .key file: 32 hex digits as text (Redump), or the 16 raw bytes."""
    try:
        raw = path.read_bytes()[:4096]
    except OSError:
        return None
    if len(raw) == 16:
        return raw.hex()
    m = re.search(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])", raw.decode("ascii", "ignore"))
    return m.group(0).lower() if m else None


class Ps3Keys:
    """Finds a disc's key file by the ISO's name, like PS3QDD: beside the ISO first, then in the key folders."""

    def __init__(self, folders):
        self.folders = [Path(f) for f in folders if f]
        self._index: dict[str, Path] | None = None
        self._lock = threading.Lock()

    def _build(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for folder in self.folders:
            for root, _dirs, names in os.walk(folder):
                for name in names:
                    stem, ext = os.path.splitext(name)
                    if ext.lower() in (".dkey", ".key"):
                        index.setdefault(stem.lower(), Path(root, name))
        return index

    def find(self, iso: Path) -> Path | None:
        for ext in (".dkey", ".key"):
            if (beside := iso.with_suffix(ext)).is_file():
                return beside
        with self._lock:
            if self._index is None:
                self._index = self._build()
            return self._index.get(iso.stem.lower())


def extract_iso(iso: Path, dest: Path, stop: threading.Event, progress: Callable[[float], None] | None = None) -> str | None:
    """Unpack an ISO 9660 image into loose files. Returns None on success, else why it failed."""
    try:
        with iso.open("rb") as f:
            root = _iso_pvd_root(f)
            if root is None:
                return "the image is not an ISO 9660 disc"
            dest.mkdir(parents=True, exist_ok=True)
            files: list[tuple[Path, IsoEntry]] = []
            total, seen, stack = 0, set(), [(root[0], root[1], Path())]
            while stack:
                extent, size, rel = stack.pop()
                if extent in seen:
                    continue
                seen.add(extent)
                for entry in _iso_read_dir(f, extent, size):
                    name = ILLEGAL_NAME_CHARS.sub("_", entry.name).strip(" .") or "_"
                    target = rel / name
                    if entry.is_dir:
                        (dest / target).mkdir(parents=True, exist_ok=True)
                        stack.append((entry.extents[0][0], entry.extents[0][1], target))
                    else:
                        files.append((target, entry))
                        total += entry.size
            done, last = 0, -1.0
            for target, entry in files:
                out = dest / target
                with out.open("wb") as w:
                    for sector, length in entry.extents:
                        f.seek(sector * PS3_SECTOR)
                        left = length
                        while left > 0:
                            if stop.is_set():
                                return "aborted"
                            chunk = f.read(min(left, 4 << 20))
                            if not chunk:
                                return f"the image ends inside {target.as_posix()}"
                            w.write(chunk)
                            left -= len(chunk)
                            done += len(chunk)
                            if progress and total and (pct := done * 100 / total) - last >= 1.0:
                                last = pct
                                progress(min(pct, 99.9))
                if out.stat().st_size != entry.size:
                    return f"{target.as_posix()} was not extracted completely"
    except OSError as exc:
        return f"could not extract: {exc}"
    return None
