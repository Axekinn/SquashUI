"""Plain data for a batch (Config, Source, Failure, BatchSummary) and the size / time formatting used
in reports."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .constants import (
    CHD_EXTENSIONS, CSO_EXTENSIONS, OUTPUT_EXTENSION, PKG_EXTENSIONS,
    PKG_TOOLS, PS3_EXTENSIONS, RVZ_DEFAULT_LEVEL, RVZ_EXTENSIONS,
    TOOL_NAMES, XBOX_EXTENSIONS,
)
from .formats import (
    associated_files, is_gamecube_wii_image, is_ps3_image, is_ps4_game_folder,
    is_ps4_pkg, is_ps5_pkg, is_psp_image, is_xbox_image,
)


# --- Configuration and per-source state ---------------------------------------------
IO_MODES = {
    "Auto-detect (serialize spinning disks only)": "auto",
    "Mechanical HDD (always one disk-heavy task at a time)": "hdd",
    "SSD / NVMe (no limits)": "ssd",
}


@dataclass(frozen=True, slots=True)
class Config:
    source: Path
    dest: Path | None
    chdman: str
    extract: bool
    verify: bool
    delete: bool
    thread_budget: int
    dolphintool: str = ""
    mode: str = "chd"  # "chd", "rvz", "cso", "pkg" or "zar"
    maxcso: str = ""
    pkgext: str = ""  # ShadPS4Plus PKG_Extractor
    anyps5: str = ""
    zarchive: str = ""
    xdvdfs: str = ""  # xdvdfs.exe (Xbox tab)
    ps3dec: str = ""  # PS3Dec.exe from PS3QDD (PS3 tab)
    ps3_keys: str = ""  # folder of Redump-style .dkey files, named like the ISOs
    ps3_output: str = "iso"  # "iso" | "folder" | "both" (see PS3_OUTPUTS)
    relinker: str = ""  # AnyPS5's relinker.exe (PS5 page, AnyPS5 target)
    ps5_target: str = "anyps5"  # PS5 page: "anyps5" (source/ + app0/, relinked) | "kyty" (app0/ contents, eboot.bin at the root)
    pkg_output: str = "folder"  # "folder" | "zar": pack each PS4 extraction into a .zar
    pkg_tool: str = "shadplus"  # which extractor: a key of PKG_TOOLS
    pkg_merge: bool = False  # PS4 page: fold each update into the game it belongs to
    pkg_layout: str = "folder"  # "folder" | "shadps4" (see pkg_target)
    rvz_scrub: bool = False
    rvz_level: int = RVZ_DEFAULT_LEVEL
    chd_compression: str = "default"  # key of CHD_CODECS
    cso_speed: str = "default"  # "default" | "fast"
    overlap_verify: bool = True  # check one title while the next one converts (see BatchRunner.run)
    io_mode: str = "auto"  # "auto" | "hdd" | "ssd" (see DiskGate)
    temp_dir: str = ""  # extraction workspace root; blank = system temp. Point at a RAM disk / SSD.
    stall_minutes: float = 10  # a tool with no progress this long is killed and restarted once (0 = off)
    tool_output: bool = True  # show each tool's newest output line under its task in the process log

    @property
    def is_rvz(self) -> bool:
        return self.mode == "rvz"

    @property
    def is_cso(self) -> bool:
        return self.mode == "cso"

    @property
    def is_pkg(self) -> bool:
        return self.mode == "pkg"

    @property
    def ps5_kyty(self) -> bool:
        """PS5 extraction for KytyPS5: the game's own files (app0/) become the output, eboot.bin at its root."""
        return self.is_ps5_pkg and self.ps5_target == "kyty"

    @property
    def ps5_relink(self) -> bool:
        """PS5 extraction for AnyPS5, followed by its relinker."""
        return self.is_ps5_pkg and self.ps5_target == "anyps5"

    @property
    def is_ps3(self) -> bool:
        return self.mode == "ps3"

    @property
    def ps3_iso(self) -> bool:
        """The PS3 tab keeps the decrypted ISO."""
        return self.ps3_output in ("iso", "both")

    @property
    def ps3_folder(self) -> bool:
        """The PS3 tab unpacks the ISO into a loose folder."""
        return self.ps3_output in ("folder", "both")

    @property
    def is_xbox(self) -> bool:
        return self.mode == "xbox"

    @property
    def is_zar(self) -> bool:
        """The ZAR tab: loose PS4 game folders packed into .zar archives."""
        return self.mode == "zar"

    @property
    def packs_zar(self) -> bool:
        """The output is a .zar: always on the ZAR tab, and on the PKG pages when asked for."""
        return self.is_zar or (self.is_pkg and self.pkg_output == "zar")

    @property
    def out_ext(self) -> str:
        return ".zar" if self.packs_zar else OUTPUT_EXTENSION[self.mode]

    @property
    def tool_name(self) -> str:
        return PKG_TOOLS.get(self.pkg_tool, PKG_TOOLS["shadplus"]) if self.is_pkg else TOOL_NAMES[self.mode]

    @property
    def merges_updates(self) -> bool:
        """The PS4 page folds each update into the game it belongs to (the "Merge updates" option)."""
        return self.is_pkg and self.pkg_tool == "shadplus" and self.pkg_merge

    @property
    def is_ps5_pkg(self) -> bool:
        """The PKG tab is running a PS5 extractor (it takes PS5 debug packages, not PS4 ones)."""
        return self.is_pkg and self.pkg_tool == "anyps5"

    @property
    def pkg_exe(self) -> str:
        """Path of the chosen PKG extractor."""
        return self.anyps5 if self.pkg_tool == "anyps5" else self.pkgext

    @property
    def extensions(self) -> frozenset[str]:
        return (frozenset() if self.is_zar else PKG_EXTENSIONS if self.is_pkg else PS3_EXTENSIONS if self.is_ps3 else XBOX_EXTENSIONS if self.is_xbox else CSO_EXTENSIONS if self.is_cso else RVZ_EXTENSIONS if self.is_rvz
                else CHD_EXTENSIONS)

    def wants(self, path: Path) -> bool:
        """Does this tab handle the file? Plain .iso files are routed by header: PSP discs on the CSO tab,
        GameCube/Wii on the RVZ tab, everything else (PSP included) on the CHD tab."""
        if self.is_zar:  # folders, not files
            return path.is_dir() and is_ps4_game_folder(path)
        ext = path.suffix.lower()
        if ext not in self.extensions:
            return False
        if self.is_pkg:  # PS4 packages, or PS5 debug packages for pkg-to-anyps5; PS3 / Vita ones are another format
            return is_ps5_pkg(path) if self.is_ps5_pkg else is_ps4_pkg(path)
        if self.is_ps3:
            return is_ps3_image(path)
        if self.is_xbox:
            return is_xbox_image(path)
        if ext != ".iso":
            return True
        if self.is_cso:
            return is_psp_image(path)
        return is_gamecube_wii_image(path) == self.is_rvz


@dataclass(eq=False, slots=True)
class Source:
    path: Path
    is_archive: bool
    remaining: int = 1  # units of work (extraction + each conversion) still outstanding
    ok: bool = True
    outputs: list[Path] = field(default_factory=list)
    size: int = 1  # bytes on disk (archive, or image + track files); weights the ETA
    converted: bool = False  # did real conversion work happen (vs. everything skipped/failed)?
    attached: list[Path] = field(default_factory=list)  # PS4 update .pkg files merged into this game (oldest first)

    def originals(self) -> set[Path]:
        """Every file this source is made of, and so what cleanup deletes: the archive, or the image with its
        track files, plus any updates merged into it."""
        return ({self.path} if self.is_archive else associated_files(self.path)) | set(self.attached)


# --- End-of-batch reporting: failures, size savings, space report ---------------------
STAGE_TITLES = {"extract": "Extraction failed", "convert": "Conversion failed", "verify": "Verification failed"}


@dataclass(frozen=True, slots=True)
class Failure:
    stage: str  # key of STAGE_TITLES
    name: str
    reason: str
    source: Path  # the file or archive the item came from (what a retry re-runs)
    retryable: bool = True  # False when only a settings change can fix it


@dataclass(frozen=True, slots=True)
class BatchSummary:
    total: int
    failures: tuple[Failure, ...]
    kind: str = "CHD"  # output format name, for the space report
    converted: int = 0  # sources that fully succeeded (the ones the sizes below cover)
    original_bytes: int = 0
    output_bytes: int = 0
    deleted: bool = False  # were the originals of those sources deleted?


def fmt_size(n: int) -> str:
    size = float(abs(n))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.2f} {unit}"
        size /= 1024
    return f"{n} B"


def fmt_duration(seconds: float) -> str:
    m = int(max(0, seconds) // 60)
    d, rest = divmod(m, 1440)
    h, m = divmod(rest, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else "<1m"


def path_bytes(path: Path) -> int:
    """Size of a file, or of everything under a folder; 0 if unreadable."""
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def remove_path(path: Path) -> None:
    """Delete a file or a whole folder tree (a .part may be either)."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def source_bytes(src_file: Path) -> int:
    """Size of one title's disc image and its track files; 0 if unreadable."""
    try:
        return sum(path_bytes(f) for f in associated_files(src_file))  # a ZAR source is a whole folder
    except OSError:
        return 0


def size_change(before: int, out: Path) -> str:
    """'71% smaller (4.20 GB -> 1.22 GB)' for a title's source size vs its output, '' if unreadable."""
    after = path_bytes(out)
    if not before or not after:
        return ""
    pct = abs(before - after) / before * 100
    word = "smaller" if after < before else "larger" if after > before else "same size"
    head = f"{pct:.0f}% {word}" if after != before else word
    return f"{head} ({fmt_size(before)} -> {fmt_size(after)})"


def space_report(sm: BatchSummary) -> str:
    """Before/after sizes for everything that converted successfully. Empty when there's nothing to compare."""
    if not sm.converted or not sm.original_bytes:
        return ""
    if sm.kind == "PKG":  # extraction doesn't shrink anything, so a before/after verdict would only mislead
        return (f"Extracted {sm.converted} package(s): {fmt_size(sm.output_bytes)} of game files from "
                f"{fmt_size(sm.original_bytes)} of PKG files.\n"
                + ("The PKG files were deleted." if sm.deleted else "The PKG files were kept."))
    if sm.kind == "PS3":  # decrypting / unpacking keeps the size
        return (f"Processed {sm.converted} PS3 disc(s): {fmt_size(sm.output_bytes)} of output from "
                f"{fmt_size(sm.original_bytes)} of encrypted ISOs.\n"
                + ("The encrypted ISOs were deleted." if sm.deleted else "The encrypted ISOs were kept."))
    diff = sm.original_bytes - sm.output_bytes
    pct = abs(diff) / sm.original_bytes * 100
    if diff > 0:
        verdict = f"Space saved: {fmt_size(diff)} ({pct:.0f}% smaller)"
        if pct >= 50:
            verdict += " - nice!"
    elif diff < 0:
        verdict = f"Output is {fmt_size(diff)} LARGER ({pct:.0f}%) - common when the originals were already compressed"
    else:
        verdict = "No size change"
    claim = ("Originals were deleted, so that space is now free." if sm.deleted else
             "Originals were kept - delete them to actually reclaim the space." if diff > 0 else "")
    lines = [
        f"Originals ({sm.converted} item(s)):  {fmt_size(sm.original_bytes)}",
        f"{sm.kind} files:  {fmt_size(sm.output_bytes)}",
        verdict,
    ]
    if claim:
        lines.append(claim)
    return "\n".join(lines)


def exit_reason(tool: str, rc: int, output: str) -> str:
    """'chdman exit code 1 - <last line of its output>' for the failure report."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return f"{tool} exit code {rc}" + (f" - {lines[-1]}" if lines else "")
