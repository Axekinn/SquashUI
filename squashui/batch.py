"""The conversion engine: scans a folder, plans threads and disks, and runs every tool pool."""
from __future__ import annotations

import contextlib
import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable

from .constants import (
    APP_DIR, APP_NAME, APP_VERSION, ARCHIVE_EXTENSIONS,
    CHD_CODECS, DVD_HUNK_SIZE, ILLEGAL_NAME_CHARS, LINE_SPLIT_RE,
    MAXCSO_FORMAT, NO_PROGRESS_RE, PERCENT_RE, PHASE_WORK,
    PROGRESS_RE, RVZ_BLOCK_SIZE, SUBPROCESS_FLAGS,
)
from .formats import (
    check_pkg_folder, compare_trees, extract_iso,
    find_anyps5_libs, is_ps4_game_folder, iso_wants_dvd_mode, locate_pkg_root,
    park_tool_log, pkg_target, ps3_game_name, ps3_known_name,
    ps3_remember_name, Ps3Keys, read_pkg_info, read_ps3_key,
    read_ps3_sfo, verify_cso,
)
from .ps4merge import attach_updates, extracted_root, merged_sfo_for, overlay_move
from .models import (
    BatchSummary, exit_reason, Failure, fmt_size,
    path_bytes, remove_path, size_change, Source,
    source_bytes, space_report,
)
from .system import (
    _Aborted, _py7zr, _Stalled, _Watchdog,
    bind_to_app_lifetime, DiskGate, file_size, find_7zip,
    IO_RETRY_ATTEMPTS, IO_RETRY_DELAY, KILL_GRACE, STALL_RETRIES,
)


def plan_workers(budget: int, sources: int, one_at_a_time: bool, serialize: bool) -> tuple[int, int]:
    """(concurrent conversions, cores for each) for a thread budget.

    RVZ runs one at a time (DolphinTool threads itself, and overlapping runs share its virtual-NAND files), and so
    does PKG extraction (the extractors are multi-threaded and the work is disk-bound).
    Otherwise about four cores per chdman / maxcso is the sweet spot; with few inputs each gets more. When the
    disks force one tool at a time anyway, that one tool gets every core.
    """
    budget = max(1, budget)
    if one_at_a_time or serialize:
        return 1, budget
    workers = max(1, min(budget // 4, sources))
    return workers, max(1, budget // workers)


@dataclass(eq=False, slots=True)
class _ToolRun:
    """What one conversion tool run produced, and how to check it."""
    rc: int
    output: str
    label: str
    verify: Callable[[], bool]
    needs_source: bool  # the check reads the source image (RVZ / CSO), so the source must outlive it
    place: Callable[[Path], None] | None = None  # moves the result into place at `out` (default: rename the .part)
    always_check: bool = False  # run the check even when the Verify box is off (it is cheap and guards the output)
    out: Path | None = None  # where the result really ends up, when the tool decided its name (PS3: from PARAM.SFO)


@dataclass(eq=False, slots=True)
class _Pending:
    """A freshly converted .part that still needs its check and the final rename."""
    part: Path
    out: Path
    label: str
    tid: int
    owner: "Source"
    before: int  # source bytes, measured now because the source may be deleted before the rename
    verify: Callable[[], bool]
    needs_source: bool
    place: Callable[[Path], None] | None = None
    always_check: bool = False


class Workspace:
    """Temp extraction folder, deleted as soon as its last conversion finishes."""

    def __init__(self, path: Path, jobs: int, on_empty: Callable[[Path], None]):
        self.path, self._left, self._on_empty = path, jobs, on_empty
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            self._left -= 1
            if self._left:
                return
        self._on_empty(self.path)


@dataclass(frozen=True)
class _Plan:
    """How one batch is spread over threads and disks; decided once, before any work starts."""
    budget: int
    workers: int
    cores: int
    extract_workers: int
    overlap: bool
    serial_convert: bool
    serial_extract: bool
    has_archives: bool
    out_root: Path
    temp_root: Path


class BatchRunner:
    EXTRACT_WORKERS = 2

    def __init__(self, cfg: Config, emit: Callable[[tuple], None], pause_event: threading.Event,
                 only: frozenset[Path] | None = None):
        self.cfg = cfg
        self._only = only  # retry mode: just these source files/archives
        self._failures: list[Failure] = []
        self._work_done = self._work_total = 0  # bytes; feeds the ETA
        self._ctx = threading.local()  # which Source the current worker thread is handling
        self._verifier: ThreadPoolExecutor | None = None  # set by run() when checks overlap conversions
        self._emit = emit
        self.pause_event = pause_event
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._procs: set[subprocess.Popen] = set()
        self._parts: set[Path] = set()
        self._workspaces: set[Path] = set()
        self._claims: dict[Path, Source] = {}
        self._ps3_keys = Ps3Keys([cfg.ps3_keys, str(Path(cfg.ps3dec).parent / "Keys") if cfg.ps3dec else ""])
        self._completed = 0
        self._total = 0
        self._slots = threading.BoundedSemaphore(1)
        self._gate = DiskGate(cfg.io_mode)

    # ---- event helpers ------------------------------------------------------
    def _log(self, msg: str) -> None:
        self._emit(("log", msg))

    def _new_task(self, text: str) -> int:
        tid = next(self._ids)
        self._emit(("new", tid, text))
        return tid

    def _set(self, tid: int, state: str, msg: str) -> None:
        self._emit(("update", tid, state, msg))

    def _progress(self, phase: str, cur: int, total: int) -> None:
        self._emit(("progress", phase, cur, total))

    def _fail(self, stage: str, label: str, reason: str, retryable: bool = True) -> None:
        """Remember a failure for the end-of-batch report (the per-file log line is written separately)."""
        src = getattr(self._ctx, "src", None)
        if src is None:
            return
        with self._lock:
            self._failures.append(Failure(stage, label.removeprefix("└─ "), reason, src.path, retryable))

    # ---- control ------------------------------------------------------------
    def _checkpoint(self) -> bool:
        """Block while paused; False means the batch was aborted."""
        self.pause_event.wait()
        return not self._stop.is_set()

    def shutdown(self) -> None:
        """Abort: kill children, wake paused workers, remove partial output."""
        self._stop.set()
        self.pause_event.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass
        for proc in procs:
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        with self._lock:
            parts, spaces = list(self._parts), list(self._workspaces)
        for part in parts:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        for space in spaces:
            shutil.rmtree(space, ignore_errors=True)

    # ---- subprocess helpers -------------------------------------------------
    def _popen(self, cmd: list[str], **kw) -> subprocess.Popen:
        if sys.platform != "win32" and (nice := shutil.which("nice")):
            cmd = [nice, "-n", "10", *cmd]  # same idea as BELOW_NORMAL on Windows
        proc = subprocess.Popen(cmd, creationflags=SUBPROCESS_FLAGS, stdin=subprocess.DEVNULL, **kw)
        bind_to_app_lifetime(proc)
        with self._lock:
            self._procs.add(proc)
        if self._stop.is_set():
            proc.kill()
        return proc

    def _forget(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def _retry_io(self, action: Callable[[], None], what: str, attempts: int | None = None) -> None:
        """Run a file operation that Windows refuses while another program has the file open (antivirus
        scanning a fresh file, the search indexer, a tool that hasn't fully exited yet)."""
        attempts = attempts or IO_RETRY_ATTEMPTS
        for attempt in range(attempts):
            try:
                return action()
            except PermissionError as exc:
                if attempt == 0:
                    self._log(f"{what}: in use by another program ({exc.strerror or exc}) - waiting for it "
                              "to be released...\n")
                if attempt == attempts - 1 or self._stop.wait(IO_RETRY_DELAY):
                    raise

    def _drop_part(self, part: Path) -> None:
        """Delete a leftover .part. Never raises: a locked leftover must not turn a finished title into an error."""
        try:
            self._retry_io(lambda: remove_path(part), part.name, attempts=5)
        except OSError:
            self._log(f"Warning: couldn't delete {part.name} (still in use); it is replaced on the next run.\n")

    def _reap(self, proc: subprocess.Popen) -> bool:
        """After a kill: True once the process has really exited. A tool stuck in an uninterruptible disk
        request can survive a kill for a long time; the caller then abandons it instead of waiting."""
        try:
            proc.wait(timeout=KILL_GRACE)
            return True
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            return False

    def _run_gated(self, args: list[str], tid: int, state: str, label: str, exe: str | None = None,
                   disk: tuple[Path, ...] = (), watch: Path | None = None, stage: str = "convert",
                   clean: Path | None = None, progress_re: re.Pattern = PROGRESS_RE,
                   cwd: Path | None = None) -> tuple[int, str]:
        """Run a tool once the disks it touches (`disk`) are free; see DiskGate.

        A tool that stalls is killed and restarted (`clean`: a half-written output to delete first). If it
        stalls again, or can't be killed, _Stalled propagates so the caller skips the title.
        """
        wait = lambda: self._set(tid, "[  QUEUED    ]", f"{label} (waiting for disk)")
        for attempt in range(1 + STALL_RETRIES):
            try:
                with self._gate.hold(disk, self._stop, wait):
                    return self._spawn(args, tid, state, label, exe, watch, stage, progress_re, cwd)
            except _Stalled as stall:
                self._log(f"\n--- STALLED: {label} made no progress for {stall.minutes:g} min and was killed ---\n")
                if clean:
                    self._drop_part(clean)
                if stall.abandoned or attempt == STALL_RETRIES or self._stop.is_set():
                    raise
                self._set(tid, "[  RETRYING  ]", f"{label} (stalled - restarting)")
        raise AssertionError("unreachable")

    def _spawn(self, args: list[str], tid: int, state: str, label: str, exe: str | None = None,
               watch: Path | None = None, stage: str = "convert",
               progress_re: re.Pattern = PROGRESS_RE, cwd: Path | None = None) -> tuple[int, str]:
        """Run chdman (or DolphinTool / maxcso / a PKG extractor via exe=), streaming its output, under a watchdog.

        Output is read on a helper thread so a tool stuck in the kernel can never block this one forever.
        """
        proc = self._popen([exe or self.cfg.chdman, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           bufsize=0, cwd=cwd)
        last_pct = -1.0
        output: deque[str] = deque(maxlen=40)
        row, latest = label, ""  # the task row's first line, and the newest line the tool itself printed

        def show() -> None:
            """The row is its status line plus, under it, the tool's newest output line: always replaced, never
            appended, so a glance at the log shows the tool is alive without scrolling past a wall of text."""
            tail = f"\n      > {latest}" if latest and self.cfg.tool_output else ""
            self._set(tid, state, row + tail)

        failure: list[BaseException] = []
        finished = threading.Event()
        dog = _Watchdog(proc, self.cfg.stall_minutes, watch)

        def handle(raw: bytes) -> None:
            nonlocal last_pct, row, latest
            if m := progress_re.search(raw):
                pct = float(m.group(1))
                if pct - last_pct >= 1.0 or (pct >= 99.9 and last_pct < 99.9):
                    last_pct = pct
                    row = f"({pct:04.1f}%) {label}"
                    show()
            elif text := raw.decode(errors="replace").strip():
                output.append(text)
                if self.cfg.tool_output:
                    latest = text[:160]
                    show()

        def pump() -> None:
            tail = b""
            try:
                while chunk := proc.stdout.read(8192):
                    dog.ping()
                    *lines, tail = LINE_SPLIT_RE.split(tail + chunk)
                    for raw in lines:
                        if raw:
                            handle(raw)
                if tail:
                    handle(tail)
            except Exception as exc:  # re-raised on the calling thread
                failure.append(exc)
            finally:
                finished.set()

        abandoned = False
        try:
            with dog:
                threading.Thread(target=pump, daemon=True, name="pump").start()
                while not finished.wait(0.5):
                    if dog.stalled.is_set():
                        break
                if dog.stalled.is_set():
                    abandoned = not self._reap(proc)
                    if abandoned or proc.returncode != 0:  # (rc 0 = it finished just before the kill)
                        raise _Stalled(self.cfg.stall_minutes, stage, abandoned)
                rc = proc.wait()
            if failure:
                raise failure[0]
            return rc, "\n".join(output)
        finally:
            if not abandoned:  # an abandoned tool stays registered so Stop can still try to kill it
                finished.wait(1.0)
                try:
                    proc.stdout.close()
                except OSError:
                    pass
                self._forget(proc)

    # ---- archive helpers ----------------------------------------------------
    def _list_target_stems(self, archive: Path) -> tuple[list[str], int | None]:
        """Peek at the archive's index: the disc names inside (so extraction can be skipped when outputs
        already exist) and the total unpacked size (None if unknown), used to size the temp workspace."""
        size = None
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                size = sum(i.file_size for i in z.infolist())
        else:
            with _py7zr().SevenZipFile(archive) as z:
                names = z.getnames()
                try:
                    total = z.archiveinfo().uncompressed
                    size = int(sum(total) if isinstance(total, (list, tuple)) else total)
                except Exception:
                    size = None
        stems = {}
        for name in names:
            p = PureWindowsPath(name)  # handles both / and \ separators
            if p.suffix.lower() in self.cfg.extensions:
                stems[p.stem] = None
        return list(stems), size

    def _extract(self, archive: Path, dest: Path) -> None:
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest)
            return
        if sevenzip := find_7zip():
            for attempt in range(1 + STALL_RETRIES):
                proc = self._popen(
                    [sevenzip, "x", "-y", "-bso0", "-bsp0", f"-o{dest}", str(archive)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                with _Watchdog(proc, self.cfg.stall_minutes, watch=dest) as dog:
                    rc, give_up = None, None
                    while rc is None:
                        try:
                            rc = proc.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            if dog.stalled.is_set():
                                give_up = give_up or time.monotonic() + KILL_GRACE
                                if time.monotonic() > give_up:
                                    break
                if rc is not None:
                    self._forget(proc)  # (an unkillable tool stays registered so Stop can retry the kill)
                if dog.stalled.is_set() and rc != 0:
                    abandoned = rc is None
                    self._log(f"\n--- STALLED: extracting {archive.name} made no progress for "
                              f"{self.cfg.stall_minutes:g} min and was killed ---\n")
                    if abandoned or attempt == STALL_RETRIES or self._stop.is_set():
                        raise _Stalled(self.cfg.stall_minutes, "extract", abandoned)
                    shutil.rmtree(dest, ignore_errors=True)  # restart from a clean workspace
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                if rc not in (0, 1):  # 1 = non-fatal warning
                    raise RuntimeError(f"7-Zip exited with code {rc}")
                return
        with _py7zr().SevenZipFile(archive) as z:
            z.extractall(path=dest)

    def _make_workspace(self, needed: int | None, tid: int, name: str) -> Path:
        """Create the extraction folder. With a user-chosen temp folder (RAM disk / SSD) wait for earlier
        workspaces to be cleaned up when it is too full, and fall back to system temp only if the archive
        can never fit there."""
        root = self.cfg.temp_dir or None
        if root and needed:
            want = int(needed * 1.02) + (64 << 20)
            try:
                if want > shutil.disk_usage(root).total:
                    self._log(f"Temp folder is smaller than {name} ({needed / 2**30:.1f} GB unpacked) - using system temp.")
                    root = None
                else:
                    while shutil.disk_usage(root).free < want:
                        if self._stop.is_set():
                            raise _Aborted
                        with self._lock:
                            others = bool(self._workspaces)
                        if not others:  # nothing left to wait for; the space is taken by something else
                            self._log(f"Temp folder is full - using system temp for {name}.")
                            root = None
                            break
                        self._set(tid, "[  QUEUED    ]", f"{name} (waiting for temp space)")
                        time.sleep(0.5)
            except OSError:
                root = None
        return Path(tempfile.mkdtemp(prefix=f"{APP_NAME.lower()}_", dir=root))

    def _acquire_slot(self) -> bool:
        while not self._slots.acquire(timeout=0.25):
            if self._stop.is_set():
                return False
        if self._stop.is_set():
            self._slots.release()
            return False
        return True

    def _release_workspace(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
        with self._lock:
            self._workspaces.discard(path)
        self._slots.release()

    # ---- bookkeeping --------------------------------------------------------
    def _record(self, src: Source, out: Path | None) -> None:
        with self._lock:
            if out is None:
                src.ok = False
            else:
                src.outputs.append(out)

    def _release_part(self, src: Source) -> None:
        with self._lock:
            src.remaining -= 1
            if src.remaining:
                return
            self._completed += 1
            done = self._completed
            if src.converted:
                self._work_done += src.size
            else:  # skipped or failed: no work happened, and none is left to do
                self._work_total -= src.size
            work = (self._work_done, self._work_total)
        self._progress(PHASE_WORK, done, self._total)
        self._emit(("work", *work))

    # ---- conversion steps -----------------------------------------------------
    def _verify(self, chd: Path, tid: int, label: str) -> bool:
        self._set(tid, "[ VERIFYING  ]", f"(00.0%) {label}")
        rc, output = self._run_gated(["verify", "-i", str(chd)], tid, "[ VERIFYING  ]", label, disk=(chd,),
                                      stage="verify")
        if rc == 0:
            return True
        if not self._stop.is_set():
            self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - exit code {rc})")
            self._log(f"\n--- VERIFY ERROR FOR {label} ---\n{output}\n")
            self._fail("verify", label, exit_reason("chdman verify", rc, output))
        return False

    def _image_sha1(self, image: Path, tid: int, label: str) -> str | None:
        rc, output = self._run_gated(["verify", "-i", str(image), "-a", "sha1"], tid, "[ VERIFYING  ]", label,
                                      exe=self.cfg.dolphintool, disk=(image,), stage="verify")
        hashes = re.findall(r"\b[0-9a-fA-F]{40}\b", output)
        return hashes[-1].lower() if rc == 0 and hashes else None

    def _verify_rvz(self, rvz: Path, source: Path | None, tid: int, label: str) -> bool:
        """Lossless proof: DolphinTool must report the same SHA-1 for the RVZ as for the source disc.

        A scrubbed RVZ intentionally differs from its source, so then the check is integrity only:
        the whole RVZ must decompress and hash without errors.
        """
        if source is None or self.cfg.rvz_scrub:
            self._set(tid, "[ VERIFYING  ]", f"(hashing RVZ) {label}")
            if self._image_sha1(rvz, tid, label):
                return True
            if not self._stop.is_set():
                self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - RVZ unreadable)")
                self._log(f"\n--- VERIFY ERROR FOR {label} ---\nDolphinTool could not read the RVZ back.\n")
                self._fail("verify", label, "DolphinTool could not read the RVZ back")
            return False
        self._set(tid, "[ VERIFYING  ]", f"(hashing source) {label}")
        want = self._image_sha1(source, tid, label)
        self._set(tid, "[ VERIFYING  ]", f"(hashing RVZ) {label}")
        got = self._image_sha1(rvz, tid, label)
        if want and got and want == got:
            return True
        if not self._stop.is_set():
            why = ("could not hash the source disc" if not want else
                   "could not hash the RVZ" if not got else "SHA-1 mismatch between source and RVZ")
            self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - {why})")
            self._log(f"\n--- VERIFY ERROR FOR {label} ---\nSource SHA-1: {want}\nRVZ SHA-1:    {got}\n")
            self._fail("verify", label, why[0].upper() + why[1:])
        return False

    def _verify_cso(self, cso: Path, source: Path, tid: int, label: str) -> bool:
        """Lossless proof: the CSO is decompressed and compared with the source ISO (see verify_cso)."""
        state, last = "[ VERIFYING  ]", [-1.0]
        self._set(tid, state, f"(00.0%) {label}")

        def progress(pct: float) -> None:
            if pct - last[0] >= 1.0 or pct >= 99.9:
                last[0] = pct
                self._set(tid, state, f"({pct:04.1f}%) {label}")

        wait = lambda: self._set(tid, "[  QUEUED    ]", f"{label} (waiting for disk)")
        with self._gate.hold((cso, source), self._stop, wait):
            why = verify_cso(cso, source, self._stop, progress)
        if why is None:
            return True
        if not self._stop.is_set():
            self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - {why})")
            self._log(f"\n--- VERIFY ERROR FOR {label} ---\n{why}\n")
            self._fail("verify", label, why[0].upper() + why[1:])
        return False

    def _xbox_checksum(self, image: Path, tid: int, label: str) -> frozenset[str]:
        """xdvdfs' deterministic checksum of an image's contents (the same for an ISO and its XISO), as the set of
        hashes it printed; empty when it could not be read."""
        rc, output = self._run_gated(["checksum", str(image)], tid, "[ VERIFYING  ]", label, exe=self.cfg.xdvdfs,
                                     disk=(image,), stage="verify", progress_re=NO_PROGRESS_RE)
        return frozenset(h.lower() for h in re.findall(r"\b[0-9a-fA-F]{32,}\b", output)) if rc == 0 else frozenset()

    def _verify_xbox(self, xiso: Path, source: Path, tid: int, label: str) -> bool:
        """Lossless proof: the XISO and the source ISO must have the same contents checksum."""
        self._set(tid, "[ VERIFYING  ]", f"(checksum of source) {label}")
        want = self._xbox_checksum(source, tid, label)
        self._set(tid, "[ VERIFYING  ]", f"(checksum of XISO) {label}")
        got = self._xbox_checksum(xiso, tid, label)
        if want and want == got:
            return True
        if not self._stop.is_set():
            why = ("could not checksum the source ISO" if not want else
                   "could not checksum the XISO" if not got else "the XISO's contents differ from the source ISO")
            self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - {why})")
            self._log(f"\n--- VERIFY ERROR FOR {label} ---\n{why}\n")
            self._fail("verify", label, why[0].upper() + why[1:])
        return False

    def _verify_ps3(self, out: Path, tid: int, label: str) -> bool:
        """An existing PS3 result must still show its PARAM.SFO (a decrypted ISO) / hold PS3_GAME (a folder)."""
        self._set(tid, "[ VERIFYING  ]", f"{label} (reading PARAM.SFO)")
        ok = (out / "PS3_GAME" / "PARAM.SFO").is_file() if out.is_dir() else read_ps3_sfo(out) is not None
        if not ok and not self._stop.is_set():
            self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - PARAM.SFO not readable)")
            self._fail("verify", label, "The existing PS3 output has no readable PARAM.SFO")
        return ok

    def _existing_output(self, out: Path, tid: int, label: str, src_file: Path | None = None) -> Path | None:
        kind = "FOLDER" if out.is_dir() else out.suffix[1:].upper()
        # Only re-verify pre-existing outputs when we're about to delete originals because of them.
        if self.cfg.delete and self.cfg.verify:
            if out.suffix.lower() == ".zar":  # compared with the folder on the ZAR tab, else it must just unpack
                ok = self._verify_zar(out, src_file if self.cfg.is_zar else None, tid, label)
            elif self.cfg.is_ps3:
                ok = self._verify_ps3(out, tid, label)
            elif self.cfg.is_xbox:
                if src_file is None:
                    self._set(tid, "[   ERROR    ]", f"{label} (Can't verify an existing XISO without its source)")
                    self._fail("verify", label, "Existing XISO can't be verified without its source", retryable=False)
                    return None
                ok = self._verify_xbox(out, src_file, tid, label)
            elif self.cfg.is_pkg:
                ok = self._verify_pkg(out, src_file, read_pkg_info(src_file) if src_file else None, tid, label)
            elif out.suffix.lower() == ".cso":
                if src_file is None:
                    self._set(tid, "[   ERROR    ]", f"{label} (Can't verify an existing CSO without its source)")
                    self._fail("verify", label, "Existing CSO can't be verified without its source", retryable=False)
                    return None
                ok = self._verify_cso(out, src_file, tid, label)
            elif out.suffix.lower() == ".rvz":
                if src_file is None and not self.cfg.rvz_scrub:
                    self._set(tid, "[   ERROR    ]", f"{label} (Can't verify an existing RVZ without its source)")
                    self._fail("verify", label, "Existing RVZ can't be verified without its source", retryable=False)
                    return None
                ok = self._verify_rvz(out, src_file, tid, label)
            else:
                ok = self._verify(out, tid, label)
            if not ok:
                return None
            self._set(tid, "[  VERIFIED  ]", f"{label} ({kind} exists)")
        else:
            self._set(tid, "[  SKIPPED   ]", f"{label} ({kind} exists)")
        return out

    def _discard_part(self, part: Path) -> None:
        self._drop_part(part)
        with self._lock:
            self._parts.discard(part)

    def _run_chd(self, src_file: Path, part: Path, tid: int, label: str, cores: int) -> _ToolRun:
        if src_file.suffix.lower() == ".iso" and iso_wants_dvd_mode(src_file):
            label = f"{label} (DVD mode)"
            mode = ["createdvd", "-hs", DVD_HUNK_SIZE]
        else:
            mode = ["createcd"]
        codec = CHD_CODECS.get(self.cfg.chd_compression, CHD_CODECS["default"])["dvd" if mode[0] == "createdvd" else "cd"]
        self._set(tid, "[ CONVERTING ]", f"(00.0%) {label}")

        def run(codec: str | None) -> tuple[int, str]:
            args = [*mode, "-i", str(src_file), "-o", str(part), "-np", str(cores)]
            return self._run_gated([*args, *(["-c", codec] if codec else [])], tid, "[ CONVERTING ]", label,
                                   disk=(src_file, part), watch=part, clean=part)

        rc, output = run(codec)
        if rc != 0 and codec and not self._stop.is_set() and "compressor" in output.lower():
            # An older chdman that doesn't know the codec: fall back rather than fail the game.
            self._log(f"\n{label}: this chdman rejected '-c {codec}', retrying with default compression.\n")
            part.unlink(missing_ok=True)
            rc, output = run(None)
        return _ToolRun(rc, output, label, lambda: self._verify(part, tid, label), needs_source=False)

    def _run_rvz(self, src_file: Path, part: Path, tid: int, label: str, cores: int) -> _ToolRun:
        label = f"{label} (RVZ)"
        self._set(tid, "[ CONVERTING ]", label)  # DolphinTool reports no percentage
        args = ["convert", "-i", str(src_file), "-o", str(part), "-f", "rvz",
                "-c", "zstd", "-l", str(self.cfg.rvz_level), "-b", RVZ_BLOCK_SIZE]
        if self.cfg.rvz_scrub:
            args.append("--scrub")
        rc, output = self._run_gated(args, tid, "[ CONVERTING ]", label, exe=self.cfg.dolphintool,
                                     disk=(src_file, part), watch=part, clean=part)
        return _ToolRun(rc, output, label, lambda: self._verify_rvz(part, src_file, tid, label), needs_source=True)

    def _run_cso(self, src_file: Path, part: Path, tid: int, label: str, cores: int) -> _ToolRun:
        self._set(tid, "[ CONVERTING ]", f"(00.0%) {label}")

        def run(fast: bool) -> tuple[int, str]:
            args = [f"--format={MAXCSO_FORMAT}", f"--threads={cores}", *(["--fast"] if fast else []),
                    str(src_file), "-o", str(part)]
            return self._run_gated(args, tid, "[ CONVERTING ]", label, exe=self.cfg.maxcso, disk=(src_file, part),
                                   watch=part, clean=part, progress_re=PERCENT_RE)

        fast = self.cfg.cso_speed == "fast"
        rc, output = run(fast)
        if rc != 0 and fast and not self._stop.is_set() and re.search(r"unrecogni[sz]ed|unknown|invalid option", output, re.I):
            self._log(f"\n{label}: this maxcso rejected '--fast', retrying with default compression.\n")
            part.unlink(missing_ok=True)
            rc, output = run(False)
        return _ToolRun(rc, output, label, lambda: self._verify_cso(part, src_file, tid, label), needs_source=True)

    def _run_pkg(self, src_file: Path, part: Path, tid: int, label: str, cores: int) -> _ToolRun:
        info = read_pkg_info(src_file)  # None for PS5 packages (no CNT header): they are named after the file
        if info:
            label = f"{label} ({info.kind}{f' v{info.version}' if info.version else ''})"
        self._set(tid, "[ EXTRACTING ]", f"(00.0%) {label}")
        logs: Path | None = APP_DIR / "pkg_logs"  # an extractor may write debug_log.txt into its working folder
        try:
            logs.mkdir(parents=True, exist_ok=True)
        except OSError:
            logs = None
        if self.cfg.is_ps5_pkg:  # pkg-to-anyps5 writes no log file into its working folder
            logs = None
        pkg, out = str(src_file), str(part)

        def run(args: list[str]) -> tuple[int, str]:
            return self._run_gated(args, tid, "[ EXTRACTING ]", label, exe=self.cfg.pkg_exe, disk=(src_file, part),
                                   watch=part, clean=part, progress_re=PERCENT_RE, cwd=logs)

        started = time.monotonic()
        if self.cfg.is_ps5_pkg:
            # <pkg> <outdir>: source/ (relinker input) and app0/ (every game file). --jobs = files decoded at once.
            jobs = str(max(1, min(cores, 8)))
            rc, output = run([pkg, out, "--jobs", jobs])
            if rc != 0 and not self._stop.is_set() and re.search(r"unexpected argument|unrecogni[sz]ed|unknown (?:option|flag)|invalid option", output, re.I):
                self._log(f"\n{label}: this pkg-to-anyps5 rejected '--jobs', retrying without it.\n")
                self._drop_part(part)
                rc, output = run([pkg, out])
        elif updates := list(getattr(getattr(self._ctx, "src", None), "attached", ())):
            rc, output = self._run_merge(src_file, part, tid, label, updates, logs)
        else:
            rc, output = run([pkg, out])  # ShadPS4Plus PKG_Extractor: <pkg> <outdir>
        if rc == 0:
            elapsed = max(time.monotonic() - started, 0.001)
            park_tool_log(part, logs, src_file.stem)
            # so two extractors can be compared on the same package
            self._log(f"{self.cfg.tool_name}: {src_file.name} extracted in {elapsed:.1f} s "
                      f"({fmt_size(int(file_size(src_file) / elapsed))}/s)\n")

        if rc == 0 and self.cfg.packs_zar and check_pkg_folder(locate_pkg_root(part), info, self.cfg.is_ps5_pkg) is None:
            return self._zar_from_extraction(part, tid, label)  # (an unsound extraction is never archived)
        if rc == 0 and self.cfg.ps5_kyty and self._ps5_game_root(part, label) is None:
            return _ToolRun(1, "pkg-to-anyps5 wrote no app0/ folder, so there are no game files to keep.", label,
                            lambda: False, needs_source=False)
        if rc == 0 and self.cfg.ps5_relink and check_pkg_folder(part, info, True) is None:
            self._relink_ps5(part, tid, label)  # (a failure is reported, but the extraction is kept)

        def place(dest: Path) -> None:
            # KytyPS5 wants eboot.bin at the root, so its folder is app0/ itself; otherwise flatten a
            # <Content ID> / <title ID> subfolder, if the tool made one
            root = part / "app0" if self.cfg.ps5_kyty else locate_pkg_root(part)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(root, dest)

        return _ToolRun(rc, output, label, lambda: self._verify_pkg(part, src_file, info, tid, label),
                        needs_source=False, place=place, always_check=True)

    def _run_merge(self, pkg: Path, part: Path, tid: int, label: str, updates: list[Path],
                   logs: Path | None) -> tuple[int, str]:
        """The "Merge updates" option: extract the game, then each of its updates (oldest first), all with the same
        PKG extractor, and lay every update over the game file by file, as installing the patch would. The game's
        param.sfo is rewritten with the newest update's version fields. The result is left in `part` as one ordinary
        game folder (sce_sys/ and eboot.bin at its root), checked and placed like any other extraction."""
        scratch = part.with_name(part.name + ".work")
        with self._lock:
            self._parts.add(scratch)
        try:
            self._retry_io(lambda: remove_path(scratch), f"Old {scratch.name}")
            scratch.mkdir(parents=True, exist_ok=True)

            def extract(source: Path, dest: Path, what: str) -> tuple[int, str, Path | None]:
                text = f"{label} ({what})"
                self._set(tid, "[ EXTRACTING ]", f"(00.0%) {text}")
                rc, output = self._run_gated([str(source), str(dest)], tid, "[ EXTRACTING ]", text,
                                             exe=self.cfg.pkg_exe, disk=(source, scratch), watch=dest, clean=dest,
                                             progress_re=PERCENT_RE, cwd=logs)
                if rc != 0:
                    return rc, f"{source.name}: {output}", None
                park_tool_log(dest, logs, source.stem)  # its debug_log.txt must not end up in the game folder
                root = extracted_root(dest)
                if root is None:
                    return 1, f"{source.name}: the extractor wrote no files.", None
                return 0, "", root

            rc, output, root = extract(pkg, scratch / "base", "game")
            if rc != 0:
                return rc, output
            versions = []
            for n, upd in enumerate(updates, 1):
                if self._stop.is_set():
                    return 1, "stopped"
                upd_dir = scratch / f"update{n}"
                rc, output, upd_root = extract(upd, upd_dir, f"update {n}/{len(updates)}")
                if rc != 0:
                    return rc, output
                self._set(tid, "[ EXTRACTING ]", f"(merging update {n}/{len(updates)}) {label}")
                moved = overlay_move(upd_root, root)
                self._drop_part(upd_dir)  # frees its space before the next update is extracted
                info = read_pkg_info(upd)
                versions.append(f"v{info.version}" if info and info.version else upd.name)
                self._log(f"{label}: laid {upd.name} over the game ({moved} file(s)).\n")
            try:
                blob = merged_sfo_for(pkg, updates)
            except ValueError as exc:
                return 1, f"The merged param.sfo could not be made: {exc}"
            (root / "sce_sys").mkdir(parents=True, exist_ok=True)
            (root / "sce_sys" / "param.sfo").write_bytes(blob)
            self._log(f"{label}: merged {', '.join(versions)} into the game and wrote its param.sfo.\n")
            self._retry_io(lambda: os.replace(root, part), f"Moving {part.name} into place")
            return 0, ""
        finally:
            self._drop_part(scratch)
            with self._lock:
                self._parts.discard(scratch)

    # ---- PS5: KytyPS5 / AnyPS5 ------------------------------------------------------
    def _ps5_game_root(self, part: Path, label: str) -> Path | None:
        """app0/: the game's own files with eboot.bin at its root, which is what KytyPS5 reads (as a folder or as
        the root of a .zar). None when the extraction has no app0/."""
        root = part / "app0"
        if not root.is_dir():
            return None
        if not (root / "eboot.bin").is_file():
            self._log(f"\nNote: {label}: app0/ has no eboot.bin at its root, which KytyPS5 needs there to detect "
                      "the game.\n")
        return root

    def _relink_ps5(self, part: Path, tid: int, label: str) -> None:
        """AnyPS5: relinker [--windows] source/eboot.bin app.exe, then AnyPS5's libs/ beside the result. The
        relinker writes the converted modules into app0/ itself. A failure never throws the extraction away (it can
        be 100+ GB): it is reported, and the package is kept rather than deleted."""
        eboot = part / "source" / "eboot.bin"
        win = sys.platform == "win32"
        target = part / ("app.exe" if win else "app.elf")
        why: str | None = None
        if not eboot.is_file():
            why = "source/eboot.bin was not written, so there is nothing to relink"
        else:
            self._set(tid, "[ CONVERTING ]", f"(relinking) {label}")
            try:
                rc, output = self._run_gated([*(["--windows"] if win else []), str(eboot), str(target)], tid,
                                             "[ CONVERTING ]", label, exe=self.cfg.relinker, disk=(part,),
                                             watch=target, clean=target, progress_re=PERCENT_RE)
                if rc != 0:
                    why = exit_reason("relinker", rc, output)
                elif not target.is_file():
                    why = f"the relinker wrote no {target.name}"
            except _Stalled as stall:
                why = f"the relinker made no progress for {stall.minutes:g} min"
        if why is None:
            libs = find_anyps5_libs(Path(self.cfg.relinker))
            if libs is None:
                self._log(f"\nNote: {label}: no AnyPS5 libs/ folder was found beside the relinker, so none was "
                          "copied. Copy AnyPS5's .prx libraries into libs/ next to the app before running it.\n")
            else:
                try:
                    shutil.copytree(libs, part / "libs", dirs_exist_ok=True)
                except OSError as exc:
                    why = f"the AnyPS5 libraries couldn't be copied: {exc}"
        if why is None:
            self._log(f"{label}: relinked to {target.name} with {Path(self.cfg.relinker).name}\n")
            return
        self._log(f"\n--- RELINK FAILED: {label} ---\n{why}\nThe extracted files were kept (source/ + app0/); "
                  "run the relinker on source/eboot.bin yourself, or try a newer AnyPS5 release.\n")
        if not self._stop.is_set():
            self._fail("convert", label, f"Relink failed (extraction kept): {why}", retryable=False)
            src = getattr(self._ctx, "src", None)
            if src is not None:
                with self._lock:
                    src.ok = False  # so the original .pkg is not deleted

    # ---- ZArchive (.zar) -------------------------------------------------------
    def _pack_zar(self, folder: Path, target: Path, tid: int, label: str) -> tuple[int, str]:
        """zarchive.exe <folder> <target.zar>: the archive's root is the folder's contents. It refuses to
        overwrite, and deletes what it wrote if it fails halfway."""
        self._set(tid, "[ CONVERTING ]", f"(packing) {label}")
        return self._run_gated([str(folder), str(target)], tid, "[ CONVERTING ]", label, exe=self.cfg.zarchive,
                               disk=(folder, target), watch=target, clean=target, progress_re=PERCENT_RE)

    def _run_xbox(self, src_file: Path, part: Path, tid: int, label: str, cores: int) -> _ToolRun:
        """Xbox tab: an ISO -> a .xiso with the video partition and the padding left out (xdvdfs pack)."""
        self._set(tid, "[ CONVERTING ]", f"{label}")
        with self._watch_growth(tid, "[ CONVERTING ]", label, part, 0):
            rc, output = self._run_gated(["pack", str(src_file), str(part)], tid, "[ CONVERTING ]", label,
                                         exe=self.cfg.xdvdfs, disk=(src_file, part), watch=part, clean=part,
                                         progress_re=NO_PROGRESS_RE)
        return _ToolRun(rc, output, label, lambda: self._verify_xbox(part, src_file, tid, label), needs_source=True)

    @contextlib.contextmanager
    def _watch_growth(self, tid: int, state: str, label: str, path: Path, total: int):
        """Show progress for a tool that prints none, from how far its output file has grown."""
        done = threading.Event()

        def loop() -> None:
            last = -1.0
            while not done.wait(1.0):
                try:
                    written = path.stat().st_size
                except OSError:
                    continue
                if not total:  # the result's size is not known beforehand (a trimmed image): show what is written
                    self._set(tid, state, f"({fmt_size(written)} written) {label}")
                    continue
                pct = min(99.9, written * 100 / total)
                if pct - last >= 0.5:
                    last = pct
                    self._set(tid, state, f"({pct:04.1f}%) {label}")

        thread = threading.Thread(target=loop, daemon=True, name="growth")
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join(2)

    def _ps3_complete(self, out: Path) -> bool:
        """Are all the outputs the PS3 tab is set to make already there?"""
        name = out.name.removesuffix(".iso") if out.suffix.lower() == ".iso" else out.name
        need = ([out.with_name(name + ".iso")] if self.cfg.ps3_iso else []) + ([out.with_name(name)] if self.cfg.ps3_folder else [])
        return all(p.exists() for p in need)

    def _run_ps3(self, src_file: Path, part: Path, tid: int, label: str, cores: int) -> _ToolRun:
        """PS3 tab: an encrypted disc ISO -> decrypted ISO and/or loose folder, named "Game Name - Title ID"."""
        cfg = self.cfg
        out_dir = part.parent
        src_size = file_size(src_file)
        wait = lambda: self._set(tid, "[  QUEUED    ]", f"{label} (waiting for disk)")

        def fail(why: str) -> _ToolRun:
            return _ToolRun(1, why, label, lambda: False, needs_source=False)

        dec, temp_ws, fpart = part, None, None
        ok = False
        try:
            if read_ps3_sfo(src_file) is not None:  # not encrypted: PS3Dec has nothing to do
                if cfg.ps3_iso:
                    self._log(f"{label}: already decrypted - copying it.\n")
                    with self._watch_growth(tid, "[ COPYING    ]", label, part, src_size), \
                            self._gate.hold((src_file, part), self._stop, wait):
                        self._set(tid, "[ COPYING    ]", f"(00.0%) {label}")
                        with src_file.open("rb") as r, part.open("wb") as w:
                            while chunk := r.read(8 << 20):
                                if self._stop.is_set():
                                    raise _Aborted
                                w.write(chunk)
                else:
                    dec = src_file
            else:
                keyfile = self._ps3_keys.find(src_file)
                if keyfile is None:
                    return fail(f"No .dkey found for {src_file.name}. Put {src_file.stem}.dkey next to the ISO or in the Keys folder.")
                key = read_ps3_key(keyfile)
                if key is None:
                    return fail(f"{keyfile.name} does not hold a 32-digit hex disc key.")
                if not cfg.ps3_iso and cfg.temp_dir:  # folder only: the decrypted ISO is a throw-away
                    temp_ws = self._make_workspace(src_size, tid, label)
                    with self._lock:
                        self._workspaces.add(temp_ws)
                    dec = temp_ws / "decrypted.iso"
                self._set(tid, "[ DECRYPTING ]", f"(00.0%) {label}")
                with self._watch_growth(tid, "[ DECRYPTING ]", label, dec, src_size):
                    rc, output = self._run_gated(["d", "key", key, str(src_file), str(dec)], tid, "[ DECRYPTING ]", label,
                                                 exe=cfg.ps3dec, disk=(src_file, dec), watch=dec, clean=dec,
                                                 progress_re=NO_PROGRESS_RE)
                if rc != 0:
                    return _ToolRun(rc, output, label, lambda: False, needs_source=False)
                if file_size(dec) != src_size:
                    return fail(f"PS3Dec wrote {fmt_size(file_size(dec))} but the ISO is {fmt_size(src_size)}.")

            sfo = read_ps3_sfo(dec)
            if sfo is None:
                return fail("The decrypted image has no readable PS3_GAME/PARAM.SFO: the key does not match this disc, "
                            "or it is not a PS3 game disc.")
            name = ps3_game_name(sfo, src_file.stem)
            ps3_remember_name(src_file, name)
            iso_t, dir_t = out_dir / f"{name}.iso", out_dir / name

            if cfg.ps3_folder and not dir_t.exists():
                fpart = part.with_name(part.name.removesuffix(".part") + ".extract")
                self._drop_part(fpart)
                with self._lock:
                    self._parts.add(fpart)
                state = "[ EXTRACTING ]"
                self._set(tid, state, f"(00.0%) {label}")
                with self._gate.hold((dec, fpart), self._stop, wait):
                    why = extract_iso(dec, fpart, self._stop, lambda pct: self._set(tid, state, f"({pct:04.1f}%) {label}"))
                if self._stop.is_set():
                    raise _Aborted
                if why:
                    return fail(why)
                if not (fpart / "PS3_GAME" / "PARAM.SFO").is_file():
                    return fail("The extracted folder has no PS3_GAME/PARAM.SFO.")
                if not cfg.ps3_iso and dec != src_file:
                    self._drop_part(dec)  # the throw-away decrypted ISO: give the space back now

            moves = ([(dec, iso_t)] if cfg.ps3_iso and not iso_t.exists() else []) + ([(fpart, dir_t)] if fpart else [])
            primary = iso_t if cfg.ps3_iso else dir_t

            def place(_dest: Path) -> None:
                for source, target in moves:
                    if source.exists():  # (a retry after a locked file finds the earlier moves done)
                        os.replace(source, target)
                if fpart is not None:
                    self._discard_part(fpart)
                if temp_ws is not None:
                    shutil.rmtree(temp_ws, ignore_errors=True)
                    with self._lock:
                        self._workspaces.discard(temp_ws)

            ok = True
            return _ToolRun(0, "", label, lambda: True, needs_source=False, place=place, out=primary)
        finally:
            if not ok:
                if fpart is not None:
                    self._discard_part(fpart)
                if temp_ws is not None:
                    shutil.rmtree(temp_ws, ignore_errors=True)
                    with self._lock:
                        self._workspaces.discard(temp_ws)

    def _run_zar(self, src_dir: Path, part: Path, tid: int, label: str, cores: int) -> _ToolRun:
        """ZAR tab: one game folder -> one .zar."""
        try:
            inside = part.resolve().is_relative_to(src_dir.resolve())
        except OSError:
            inside = False
        if inside:  # the archive would be packed into itself
            return _ToolRun(1, "The destination is inside the folder being packed; choose another Dest Dir.",
                            label, lambda: False, needs_source=True)
        rc, output = self._pack_zar(src_dir, part, tid, label)
        return _ToolRun(rc, output, label, lambda: self._verify_zar(part, src_dir, tid, label), needs_source=True)

    def _zar_from_extraction(self, part: Path, tid: int, label: str) -> _ToolRun:
        """PKG tab with .zar output: the package is extracted into `part`; pack it, then drop the folder."""
        if self.cfg.is_ps5_pkg:
            # KytyPS5 wants the game's own files with eboot.bin at the archive root: that is app0/. (source/ is only
            # the AnyPS5 relinker's input and goes with the extraction folder.)
            content = self._ps5_game_root(part, label)
            if content is None:
                return _ToolRun(1, "pkg-to-anyps5 wrote no app0/ folder, so there is nothing to pack.", label,
                                lambda: False, needs_source=False)
        else:
            content = locate_pkg_root(part)
        zpart = part.with_name(part.name.removesuffix(".part") + ".pack")
        self._drop_part(zpart)
        with self._lock:
            self._parts.add(zpart)
        rc, output = self._pack_zar(content, zpart, tid, label)
        if rc != 0:
            self._log(f"\n--- ZArchive could not pack {label} ---\n")
            self._discard_part(zpart)
            return _ToolRun(rc, output, label, lambda: False, needs_source=False)
        self._drop_part(part)  # the extracted files are in the archive now: free the space at once

        def verify() -> bool:
            self._set(tid, "[ VERIFYING  ]", label)
            if file_size(zpart) > 0:
                return True
            self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - the .zar is empty)")
            self._fail("verify", label, "The .zar is empty")
            self._discard_part(zpart)
            return False

        def place(dest: Path) -> None:
            os.replace(zpart, dest)

        return _ToolRun(0, output, label, verify, needs_source=False, place=place, always_check=True)

    def _verify_zar(self, zar: Path, source: Path | None, tid: int, label: str) -> bool:
        """Unpack the .zar again. With the source folder (ZAR tab) every file must be there at its size;
        without it (an existing .zar of a package) the whole archive must simply unpack to something."""
        state = "[ VERIFYING  ]"
        self._set(tid, state, f"(unpacking to compare) {label}")
        why: str | None = None
        work = Path(tempfile.mkdtemp(prefix="squashui_zar_", dir=self.cfg.temp_dir or None))
        with self._lock:
            self._workspaces.add(work)
        try:
            if file_size(zar) == 0:
                why = "the .zar is empty"
            else:
                rc, output = self._run_gated([str(zar), str(work)], tid, state, label, exe=self.cfg.zarchive,
                                             disk=(zar, work), stage="verify", clean=work, progress_re=PERCENT_RE)
                if rc != 0:
                    why = exit_reason("ZArchive", rc, output)
                elif source is not None:
                    why = compare_trees(source, work)
                elif not any(f.is_file() for f in work.rglob("*")):
                    why = "the .zar unpacked to nothing"
        finally:
            shutil.rmtree(work, ignore_errors=True)
            with self._lock:
                self._workspaces.discard(work)
        if why is None:
            return True
        if not self._stop.is_set():
            self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - {why})")
            self._log(f"\n--- VERIFY ERROR FOR {label} ---\n{why}\n")
            self._fail("verify", label, why[0].upper() + why[1:])
        return False

    def _verify_pkg(self, folder: Path, src_file: Path | None, info: PkgInfo | None, tid: int, label: str) -> bool:
        """Sanity-check an extracted package folder (see check_pkg_folder)."""
        self._set(tid, "[ VERIFYING  ]", label)
        root = locate_pkg_root(folder)
        why = check_pkg_folder(root, info, self.cfg.is_ps5_pkg)
        if why is None:
            if src_file is not None and (pkg_size := file_size(src_file)) and (got := path_bytes(root)) < pkg_size * 0.9 - (64 << 20):
                self._log(f"Note: {label} extracted to {fmt_size(got)}, noticeably less than the {fmt_size(pkg_size)} "
                          "package. Updates can legitimately omit files; check it if the game doesn't run.\n")
            return True
        if not self._stop.is_set():
            self._set(tid, "[   ERROR    ]", f"{label} (Verification failed - {why})")
            self._log(f"\n--- VERIFY ERROR FOR {label} ---\n{why}\n")
            self._fail("verify", label, why[0].upper() + why[1:])
        return False

    def _ps5_unreadable(self, src_file: Path, tid: int, label: str) -> str | None:
        """Ask pkg-to-anyps5 to list the package (reads headers, writes nothing). Why it can't be read, or None.

        Retail and encrypted packages share the debug packages' FIH header, so only the tool can tell them
        apart. Exit status 2 is its 'package cannot be read' (retail flag, encrypted filesystem or SELF segments);
        any other failure (e.g. a build without --list) is not taken as proof, and extraction goes ahead."""
        self._set(tid, "[ VERIFYING  ]", f"{label} (checking the package is readable)")
        rc, output = self._run_gated([str(src_file), "--list"], tid, "[ VERIFYING  ]", label, exe=self.cfg.pkg_exe,
                                     disk=(src_file,), progress_re=PERCENT_RE)
        if rc != 2:
            return None
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        return lines[-1] if lines else "pkg-to-anyps5 cannot read this package"

    def _output_path(self, src_file: Path, out_dir: Path) -> Path:
        if self.cfg.is_zar:
            return out_dir / f"{ILLEGAL_NAME_CHARS.sub('', src_file.name).strip(' .') or 'game'}.zar"
        if self.cfg.is_ps3:
            name = ps3_known_name(src_file) or ps3_game_name(None, src_file.stem)
            return out_dir / (name + (".iso" if self.cfg.ps3_iso else ""))
        if self.cfg.is_pkg:
            target = pkg_target(read_pkg_info(src_file), src_file.stem, out_dir, self.cfg.pkg_layout)
            return target.with_name(target.name + ".zar") if self.cfg.packs_zar else target
        return out_dir / f"{src_file.stem}{self.cfg.out_ext}"

    def _convert_one(self, src_file: Path, out_dir: Path, tid: int, label: str,
                     cores: int, owner: Source) -> Path | _Pending | None:
        """Convert one disc image. Returns its finished output (already existed / duplicate), a _Pending that still
        needs its check (see _finish_one), or None when it failed or the batch was stopped."""
        out = self._output_path(src_file, out_dir)

        with self._lock:
            prior = self._claims.get(out)
            if prior is None:
                self._claims[out] = owner
        if prior is not None:  # two inputs would write the same output
            if prior is owner:
                self._set(tid, "[  SKIPPED   ]", f"{label} (Duplicate name in same source)")
                return out
            self._set(tid, "[   ERROR    ]", f"{label} (Another source already maps to {out.name})")
            self._fail("convert", label, f"Another source already maps to {out.name}", retryable=False)
            return None

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and not (self.cfg.is_ps3 and not self._ps3_complete(out)):
            return self._existing_output(out, tid, label, src_file)
        if self.cfg.packs_zar and not self.cfg.zarchive:
            self._set(tid, "[   ERROR    ]", f"{label} (ZArchive not available - set it in Conversion Tool)")
            self._fail("convert", label, "ZArchive not available", retryable=False)
            return None
        missing = ((not self.cfg.dolphintool) if self.cfg.is_rvz else (not self.cfg.maxcso) if self.cfg.is_cso
                   else (not self.cfg.pkg_exe) if self.cfg.is_pkg
                   else (not self.cfg.ps3dec) if self.cfg.is_ps3
                   else (not self.cfg.xdvdfs) if self.cfg.is_xbox else False)
        if missing:
            tool = self.cfg.tool_name
            self._set(tid, "[   ERROR    ]", f"{label} ({tool} not available - set it in Conversion Tool)")
            self._fail("convert", label, f"{tool} not available", retryable=False)
            return None

        # Write to <name>.part and rename only after success, so a crash/abort can never leave a
        # half-written file that a later run mistakes for a finished one.
        part = out.with_name(out.name + ".part")
        self._retry_io(lambda: remove_path(part), f"Old {part.name}")
        with self._lock:
            self._parts.add(part)
        pending = None
        try:
            if self.cfg.is_ps5_pkg and (why := self._ps5_unreadable(src_file, tid, label)):
                if not self._stop.is_set():
                    self._set(tid, "[  FLAGGED   ]", f"{label} (not an unencrypted PS5 debug package - skipped)")
                    self._log(f"\n--- FLAGGED: {label} ---\npkg-to-anyps5 cannot read this package: {why}\n"
                              "It is probably a retail or otherwise encrypted package. The file was left untouched.\n")
                    self._fail("convert", label, f"Retail / encrypted PS5 package: {why}", retryable=False)
                return None
            run_tool = {"chd": self._run_chd, "rvz": self._run_rvz, "cso": self._run_cso,
                        "pkg": self._run_pkg, "zar": self._run_zar, "ps3": self._run_ps3,
                        "xbox": self._run_xbox}[self.cfg.mode]
            run = run_tool(src_file, part, tid, label, cores)
            if run.rc != 0:
                if not self._stop.is_set():
                    self._set(tid, "[   ERROR    ]", f"{run.label} (Conversion failed - exit code {run.rc})")
                    self._log(f"\n--- ERROR FOR {run.label} ---\n{run.output}\n")
                    self._fail("convert", run.label, exit_reason(self.cfg.tool_name, run.rc, run.output))
                return None
            final = run.out or out
            if final != out:  # the tool named its own result (PS3: from PARAM.SFO)
                with self._lock:
                    prior = self._claims.setdefault(final, owner)
                if prior is not owner:
                    self._set(tid, "[   ERROR    ]", f"{label} (Another source already maps to {final.name})")
                    self._fail("convert", label, f"Another source already maps to {final.name}", retryable=False)
                    return None
            pending = _Pending(part, final, run.label, tid, owner, source_bytes(src_file), run.verify, run.needs_source,
                               run.place, run.always_check)
            return pending
        finally:
            if pending is None:  # on success the part lives on until _finish_one has checked and renamed it
                self._discard_part(part)

    def _finish_one(self, p: _Pending) -> Path | None:
        """Check a converted .part (if verifying) and move it into place."""
        checked = self.cfg.verify or p.always_check
        try:
            if checked and not p.verify():
                return None
            place = (lambda: p.place(p.out)) if p.place else (lambda: os.replace(p.part, p.out))
            self._retry_io(place, f"Finishing {p.out.name}")
        finally:
            self._discard_part(p.part)
        p.owner.converted = True
        change = (f"extracted {fmt_size(path_bytes(p.out))}" if self.cfg.is_pkg and not self.cfg.packs_zar
                  else f"{'extracted' if p.out.is_dir() else 'decrypted'} {fmt_size(path_bytes(p.out))}" if self.cfg.is_ps3
                  else size_change(p.before, p.out))
        self._set(p.tid, "[  VERIFIED  ]" if checked else "[ CONVERTED  ]",
                  f"{p.label} - {change}" if change else p.label)
        return p.out

    # ---- jobs ---------------------------------------------------------------
    def _stall_failure(self, tid: int, label: str, stall: _Stalled, doing: str) -> None:
        """Log and record a title that is skipped because its tool hung."""
        why = (f"Stalled: no progress for {stall.minutes:g} min"
               + (" - the tool could not be terminated and may still hold its files" if stall.abandoned
                  else " (restarted once, stalled again)" if STALL_RETRIES else ""))
        self._set(tid, "[  STALLED   ]", f"{label} ({doing} made no progress - skipped)")
        self._log(f"Skipping {label}: {why}\n")
        if not self._stop.is_set():
            self._fail(stall.stage, label, why)

    def _guarded(self, src: Source, tid: int, label: str, name: str, action: Callable[[], Path | None]) -> Path | None:
        """Run one stage of a title, turning every way it can go wrong into a status line and a report entry."""
        self._ctx.src = src
        try:
            return action()
        except _Aborted:
            pass  # batch stopped while waiting for a disk
        except _Stalled as stall:
            self._stall_failure(tid, label, stall, "verification" if stall.stage == "verify" else "conversion")
        except PermissionError as exc:
            self._set(tid, "[   ERROR    ]", f"{label} (File in use by another program)")
            self._log(f"File in use while processing {name}: {exc}\n")
            self._fail("convert", label, f"A file stayed locked by another program (antivirus or indexer?): {exc.filename or exc}")
        except Exception as exc:
            self._set(tid, "[   ERROR    ]", f"{label} (Unexpected error)")
            self._log(f"Unhandled error processing {name}: {exc}")
            self._fail("convert", label, f"Unexpected error: {exc}")
        return None

    def _conclude(self, src: Source, out: Path | None, ws: Workspace | None) -> None:
        """A title is finished (or failed): book it and let go of its extracted files."""
        self._record(src, out)
        self._release_part(src)
        if ws:
            ws.release()

    def _convert_job(self, src: Source, target: Path, out_dir: Path, tid: int, label: str,
                     cores: int, ws: Workspace | None = None) -> None:
        result = self._guarded(src, tid, label, target.name, lambda: (
            self._convert_one(target, out_dir, tid, label, cores, src) if self._checkpoint() else None))
        if isinstance(result, _Pending):
            if self._verifier is not None:
                # Check it on the verify pool while this worker moves on to the next disc. A check that doesn't
                # need the source lets the extracted files go right away, which frees temp space sooner.
                self._set(tid, "[  QUEUED    ]", f"{result.label} (waiting to verify)")
                if ws and not result.needs_source:
                    ws.release()
                    ws = None  # released: nothing left for the verify job (or the fallback below) to let go of
                try:
                    self._verifier.submit(self._verify_job, src, result, ws)
                    return
                except RuntimeError:  # pool already shut down (batch stopping): check it here instead
                    pass
            pending = result
            result = self._guarded(src, tid, label, target.name, lambda: self._finish_one(pending))
        self._conclude(src, result, ws)

    def _verify_job(self, src: Source, pending: _Pending, ws: Workspace | None) -> None:
        def stage() -> Path | None:
            if self._checkpoint():
                return self._finish_one(pending)
            self._discard_part(pending.part)  # aborted before the check started
            return None

        self._conclude(src, self._guarded(src, pending.tid, pending.label, pending.out.name, stage), ws)

    def _file_job(self, src: Source, cores: int) -> None:
        title = src.path.name if self.cfg.is_zar else src.path.stem  # a folder name keeps its dots (v1.05)
        tid = self._new_task(f"[  WAITING   ] {title}")
        out_dir = self.cfg.dest or src.path.parent
        self._convert_job(src, src.path, out_dir, tid, title, cores)

    def _extract_job(self, src: Source, convert_pool: ThreadPoolExecutor, cores: int) -> None:
        name = src.path.name
        tid = self._new_task(f"[  WAITING   ] {name}")
        out_dir = self.cfg.dest or src.path.parent
        slot, temp = False, None
        self._ctx.src = src
        try:
            if not self._checkpoint():
                self._record(src, None)
                return

            try:
                stems, needed = self._list_target_stems(src.path)
            except Exception:
                stems, needed = None, None  # can't peek (e.g. no py7zr) - fall through to real extraction
            if stems is not None and not stems:
                self._set(tid, "[  SKIPPED   ]", f"{name} (No valid disc images)")
                return

            # Everything already converted? Then there is no reason to extract at all.
            if stems:
                found = [q if (q := out_dir / f"{s}{self.cfg.out_ext}").exists() else None for s in stems]
                # An existing (unscrubbed) RVZ or a CSO can only be proven against its source, so that case still extracts.
                needs_source = (self.cfg.delete and self.cfg.verify and any(found)
                                and (self.cfg.is_cso or (self.cfg.is_rvz and not self.cfg.rvz_scrub)))
                if all(found) and not needs_source:
                    existing = found
                    multi = len(existing) > 1
                    if multi:
                        self._set(tid, "[  SKIPPED   ]", f"{name} ({len(existing)} outputs exist)")
                    for p in existing:
                        sub = self._new_task(f"[  WAITING   ] └─ {p.stem}") if multi else tid
                        self._record(src, self._existing_output(p, sub, f"└─ {p.stem}" if multi else p.stem))
                    return

            # Bounded read-ahead: extraction stays one archive ahead of the converters
            # without filling the temp drive.
            if not (slot := self._acquire_slot()):
                self._record(src, None)
                return
            temp = self._make_workspace(needed, tid, name)
            with self._lock:
                self._workspaces.add(temp)
            # On a spinning disk the archive read and the workspace write never overlap another tool's I/O.
            wait = lambda: self._set(tid, "[  QUEUED    ]", f"{name} (waiting for disk)")
            with self._gate.hold((src.path, temp), self._stop, wait):
                self._set(tid, "[ EXTRACTING ]", name)
                started = time.monotonic()
                self._log(f"  [extract] {name}: unpacking ({fmt_size(src.size)}) ...")
                self._extract(src.path, temp)
                self._log(f"  [extract] {name}: unpacked in {time.monotonic() - started:.0f} s")

            targets = sorted(p for p in temp.rglob("*") if p.is_file() and self.cfg.wants(p))
            if not targets:
                self._set(tid, "[  SKIPPED   ]", f"{name} (No valid disc images)")
                return

            multi = len(targets) > 1
            if multi:
                self._set(tid, "[  UNZIPPED  ]", f"{name} ({len(targets)} games)")
            ws = Workspace(temp, len(targets), self._release_workspace)
            with self._lock:
                src.remaining += len(targets)
            slot, temp = False, None  # ownership moves to the workspace
            for target in targets:
                sub = self._new_task(f"[  WAITING   ] └─ {target.stem}") if multi else tid
                label = f"└─ {target.stem}" if multi else target.stem
                convert_pool.submit(self._convert_job, src, target, out_dir, sub, label, cores, ws)
        except _Aborted:
            self._record(src, None)
        except _Stalled as stall:
            self._record(src, None)
            self._stall_failure(tid, name, stall, "extraction" if stall.stage == "extract" else "verification")
        except Exception as exc:
            self._record(src, None)
            self._set(tid, "[   ERROR    ]", f"{name} (Extraction failed)")
            self._log(f"Extraction error for {name}: {exc}")
            if not self._stop.is_set():
                self._fail("extract", name, str(exc) or type(exc).__name__)
        finally:
            if temp:
                shutil.rmtree(temp, ignore_errors=True)
                with self._lock:
                    self._workspaces.discard(temp)
            if slot:
                self._slots.release()
            self._release_part(src)

    # ---- orchestration ------------------------------------------------------
    def _scan_game_folders(self) -> list[Source]:
        """ZAR tab: every PS4 game / update / DLC folder under the source (the source itself may be one). A found
        folder is taken whole, so nothing inside it is looked at again."""
        sources: list[Source] = []
        root = self.cfg.source
        if is_ps4_game_folder(root):
            sources.append(Source(root, False))
        else:
            for cur, dirs, _files in os.walk(root):
                rest = []
                for d in sorted(dirs):
                    found = Path(cur, d)
                    if is_ps4_game_folder(found):
                        sources.append(Source(found, False))
                    else:
                        rest.append(d)
                dirs[:] = rest
        if self._only is not None:
            sources = [s for s in sources if s.path in self._only]
        sources.sort(key=lambda s: str(s.path).lower())
        return sources

    def _scan(self) -> list[Source]:
        if self.cfg.is_zar:
            return self._scan_game_folders()
        sources, skipped = [], 0
        for root, _dirs, files in os.walk(self.cfg.source):
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext in self.cfg.extensions:
                    path = Path(root, name)
                    if self.cfg.wants(path):
                        sources.append(Source(path, False))
                    else:
                        skipped += 1  # an .iso that belongs on the other tab
                elif self.cfg.extract and ext in ARCHIVE_EXTENSIONS:
                    sources.append(Source(Path(root, name), True))
        if skipped and self.cfg.is_pkg:
            what = ("PS5 debug packages (retail, PS4, PS3 / Vita, or damaged)" if self.cfg.is_ps5_pkg
                    else "PS4 packages (PS3 / Vita, PS5, or damaged)")
            self._log(f"Skipped {skipped} .pkg file(s) that are not {what}.\n")
        elif skipped and self.cfg.is_xbox:
            self._log(f"Skipped {skipped} .iso file(s) that are not Xbox / Xbox 360 discs (no XDVDFS volume).\n")
        elif skipped and self.cfg.is_ps3:
            self._log(f"Skipped {skipped} .iso file(s) that are not PS3 discs (no PS3_GAME folder).\n")
        elif skipped and self.cfg.is_cso:
            self._log(f"Skipped {skipped} .iso file(s) that are not PSP discs (no PSP_GAME folder).\n")
        elif skipped:
            other = "CHD" if self.cfg.is_rvz else "RVZ"
            self._log(f"Skipped {skipped} .iso file(s) that belong to the {other} tab.\n")
        if self.cfg.merges_updates:
            sources = attach_updates(sources, self._log)  # before the retry filter: a retried game keeps its updates
        if self._only is not None:
            sources = [s for s in sources if s.path in self._only]
        sources.sort(key=lambda s: str(s.path).lower())
        return sources

    def run(self) -> None:
        summary = None
        try:
            self._log(f"=== {APP_NAME} {APP_VERSION} ===\n")
            self._log(f"--- {self.cfg.mode.upper()} Batch Started: {self.cfg.source} ---\n")
            self._progress("Scanning Files", 0, 1)
            sources = self._scan()
            if not sources:
                self._log("ERROR: No valid files or archives found to process.")
                return
            self._size_sources(sources)
            plan = self._plan(sources)
            self._log_plan(plan)
            self._log_mode_notes()
            self._progress(PHASE_WORK, 0, self._total)
            self._run_pools(sources, plan)
            if self._stop.is_set():
                return
            summary = self._finish(sources)
        except Exception as exc:
            self._log(f"Fatal error: {exc!r}")
        finally:
            self._emit(("done", summary))

    def _size_sources(self, sources: list[Source]) -> None:
        """Total and per-title byte counts, which drive the progress bar and the ETA."""
        self._total = len(sources)
        for src in sources:
            try:
                src.size = max(1, sum(path_bytes(f) for f in src.originals()))
            except OSError:
                pass
        self._work_total = sum(src.size for src in sources)
        self._emit(("work", 0, self._work_total))

    def _plan(self, sources: list[Source]) -> _Plan:
        """Decide how the batch is spread over threads and disks (nothing starts yet)."""
        budget = max(1, self.cfg.thread_budget)

        # --- disk layout: where does each kind of I/O land, and does that disk need protecting? ---
        gate = self._gate
        out_root = self.cfg.dest or self.cfg.source
        temp_root = Path(self.cfg.temp_dir) if self.cfg.temp_dir else Path(tempfile.gettempdir())
        has_archives = any(s.is_archive for s in sources)
        has_loose = not all(s.is_archive for s in sources)
        if self.cfg.temp_dir:
            # A folder picked on purpose (RAM disk / SSD) is never throttled, unless the source or the
            # output lives on that same disk.
            gate.exempt = {gate.disk_id(temp_root)} - {gate.disk_id(self.cfg.source), gate.disk_id(out_root)}
        convert_io = [out_root, *([self.cfg.source] if has_loose else []), *([temp_root] if has_archives else [])]
        serial_convert = any(gate.key(p) is not None for p in convert_io)
        serial_extract = has_archives and any(gate.key(p) is not None for p in (self.cfg.source, temp_root))

        workers, cores = plan_workers(budget, len(sources), self.cfg.is_rvz or self.cfg.is_pkg or self.cfg.is_ps3, serial_convert)
        if (self.cfg.is_zar or self.cfg.is_xbox) and not serial_convert:  # zarchive / xdvdfs are single-threaded: a few at once
            workers = max(1, min(budget, len(sources), 4))
        # A chdman verify / CSO check is single-threaded, so while one title is being checked the other cores
        # would idle. Checking on its own pool lets the next conversion start straight away. Not for RVZ (one
        # DolphinTool at a time) and not when the disks serialize everything anyway.
        overlap = (self.cfg.overlap_verify and self.cfg.verify and not self.cfg.is_rvz and not self.cfg.is_pkg
                   and not self.cfg.is_zar and not self.cfg.is_ps3 and not serial_convert)
        extract_workers = 1 if serial_extract else self.EXTRACT_WORKERS
        return _Plan(budget, workers, cores, extract_workers, overlap, serial_convert, serial_extract,
                     has_archives, out_root, temp_root)

    def _log_plan(self, plan: _Plan) -> None:
        budget, workers, cores = plan.budget, plan.workers, plan.cores
        overlap, serial_convert, serial_extract = plan.overlap, plan.serial_convert, plan.serial_extract
        has_archives, out_root, temp_root, extract_workers = plan.has_archives, plan.out_root, plan.temp_root, plan.extract_workers
        gate = self._gate
        seven = find_7zip()
        per_task = ("managed by DolphinTool" if self.cfg.is_rvz else f"managed by {self.cfg.tool_name}" if self.cfg.is_pkg or self.cfg.is_ps3
                    else "single-threaded tool" if self.cfg.is_zar or self.cfg.is_xbox else f"{cores} (--threads)" if self.cfg.is_cso else f"{cores} (-np)")
        self._log(
            f"Thread Budget: {budget} | Concurrent Conversions: {workers} | Cores per Task: {per_task}"
            f" | 7-Zip: {'native' if seven else 'py7zr fallback'}\n"
        )
        io = f"Disk I/O: {self.cfg.io_mode} | Source: {gate.describe(self.cfg.source)} | Output: {gate.describe(out_root)}"
        if has_archives:
            io += f" | Temp: {gate.describe(temp_root)}{' (custom)' if self.cfg.temp_dir else ''}"
        if overlap:
            io += " | Checks overlap the next conversion"
        if serial_convert or serial_extract:
            io += " | Disk-heavy tasks serialized" + (f" (extract workers: {extract_workers})" if has_archives else "")
        self._log(io + "\n")
        touched = [self.cfg.source, out_root, *([temp_root] if has_archives else [])]
        if self.cfg.io_mode == "auto" and any(gate.kind(p) == "unknown" for p in touched):
            self._log("Note: couldn't tell whether a drive is a spinning disk. If one is, set "
                      "Disk I/O Mode to 'Mechanical HDD'.\n")

    def _log_mode_notes(self) -> None:
        """One banner line per feature the chosen mode uses (what runs, and how it is verified)."""
        if self.cfg.is_pkg:
            layout = (("one folder per PKG: the game files, eboot.bin at its root (for KytyPS5)"
                       if self.cfg.ps5_kyty else "one folder per PKG (source/ + app0/"
                       + (", relinked with AnyPS5's relinker)" if self.cfg.ps5_relink else ", for AnyPS5)"))
                      if self.cfg.is_ps5_pkg
                      else "shadPS4 layout (CUSAxxxxx / CUSAxxxxx-UPDATE / addcont)" if self.cfg.pkg_layout == "shadps4"
                      else "one folder per PKG")
            self._log(f"PKG: {self.cfg.tool_name} | {layout} | one extraction at a time | each result is "
                      "checked before it is kept\n")
            if self.cfg.merges_updates:
                self._log("PKG: Merge updates is on - each update is extracted and laid over the game with the same "
                          "title ID, and its param.sfo takes the newest version; updates with no game here are "
                          "extracted on their own\n")
        if self.cfg.packs_zar:
            self._log("ZAR: packed with ZArchive (shadPS4 0.17+ reads a .zar directly) | "
                      + ("Verify: unpack again and compare every file with the folder"
                         if self.cfg.is_zar and self.cfg.verify else "Verify: off - only a failed pack is reported"
                         if self.cfg.is_zar else "each extraction is checked before it is packed"
                         + (" (app0/ only, for KytyPS5)" if self.cfg.is_ps5_pkg else "")) + "\n")
        if self.cfg.is_xbox:
            self._log("Xbox: xdvdfs pack (video partition and padding left out) | "
                      "Verify: xdvdfs checksum of the XISO compared with the source ISO\n")
        if self.cfg.is_ps3:
            what = {"iso": "decrypted ISO", "folder": "loose folder", "both": "decrypted ISO + loose folder"}[self.cfg.ps3_output]
            self._log(f"PS3: PS3Dec + the .dkey that matches each ISO | output: {what} | named 'Game Name - Title ID' "
                      "from PARAM.SFO | one title at a time | each result is checked before it is kept\n")
        if self.cfg.is_cso:
            self._log(f"CSO: {MAXCSO_FORMAT} via maxcso, {self.cfg.cso_speed} compression | "
                      "Verify: decompress and compare with the source ISO\n")
        if self.cfg.mode == "chd":
            self._log(f"CHD: compression = {self.cfg.chd_compression}\n")
        if self.cfg.is_rvz:
            self._log(f"RVZ: zstd level {self.cfg.rvz_level} | Scrub: {'ON' if self.cfg.rvz_scrub else 'off'}\n")


    def _run_pools(self, sources: list[Source], plan: _Plan) -> None:
        workers, cores, overlap, extract_workers = plan.workers, plan.cores, plan.overlap, plan.extract_workers
        self._slots = threading.BoundedSemaphore(workers + 1)
        with ThreadPoolExecutor(workers if overlap else 1, thread_name_prefix="verify") as verify_pool, \
             ThreadPoolExecutor(workers, thread_name_prefix="convert") as convert_pool, \
             ThreadPoolExecutor(extract_workers, thread_name_prefix="extract") as extract_pool:
            self._verifier = verify_pool if overlap else None
            for src in sources:
                if src.is_archive:
                    extract_pool.submit(self._extract_job, src, convert_pool, cores)
                else:
                    convert_pool.submit(self._file_job, src, cores)


    def _finish(self, sources: list[Source]) -> BatchSummary:
        converted, orig_bytes, out_bytes = self._measure(sources)  # before cleanup removes the originals
        if self.cfg.delete:
            self._cleanup_originals(sources)
        self._log("\n--- Batch Process Complete ---")
        summary = BatchSummary(self._total, tuple(self._failures), "ZAR" if self.cfg.packs_zar else self.cfg.mode.upper(),
                               converted, orig_bytes, out_bytes, self.cfg.delete)
        if report := space_report(summary):
            self._log("\n" + report)
        return summary

    @staticmethod
    def _measure(sources: list[Source]) -> tuple[int, int, int]:
        """(sources, original bytes, output bytes) over fully successful sources only, so it's like for like."""
        count = orig = new = 0
        for src in sources:
            if not (src.ok and src.outputs):
                continue
            originals = src.originals()
            try:
                o = sum(path_bytes(f) for f in originals)
                n = sum(path_bytes(f) for f in set(src.outputs))
            except OSError:
                continue
            count, orig, new = count + 1, orig + o, new + n
        return count, orig, new

    def _cleanup_originals(self, sources: list[Source]) -> None:
        self._log("\n--- Cleaning up original files ---")
        self._progress("Cleanup", 1, 1)
        for src in sources:
            # Only delete when EVERY game from that source converted (and verified) successfully.
            if not (src.ok and src.outputs):
                continue
            for f in src.originals():
                try:
                    remove_path(f)  # a file, or a whole game folder on the ZAR tab
                except OSError:
                    pass
