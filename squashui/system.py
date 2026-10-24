"""OS and process helpers: tool lookup, 7-Zip, disk detection and the one-disk-at-a-time gate, the
stall watchdog, and the Windows job object that ends child tools with the app."""
from __future__ import annotations

import contextlib
import ctypes
import functools
import ntpath
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .constants import APP_DIR


@functools.cache
def _py7zr():
    try:
        import py7zr
    except ImportError as exc:
        raise RuntimeError("py7zr is not installed and no 7-Zip executable was found") from exc
    return py7zr


@functools.cache
def find_7zip() -> str | None:
    """A native 7-Zip is multithreaded and far faster than py7zr on large .7z files."""
    for name in ("7zz", "7z", "7za"):
        if found := shutil.which(name):
            return found
    if sys.platform == "win32":
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            if base := os.environ.get(env):
                candidate = Path(base) / "7-Zip" / "7z.exe"
                if candidate.is_file():
                    return str(candidate)
    return None


def portable_path(path: Path | str) -> str:
    """Show anything inside the app folder as ./sub/folder/file, so the whole folder can be moved or copied."""
    try:
        return "./" + Path(path).resolve().relative_to(APP_DIR).as_posix()
    except (ValueError, OSError):
        return str(path)


def resolve_tool_path(value: str) -> str:
    """Anchor a relative ./folder/tool.exe to the app folder (not the launch directory).

    Bare names such as "chdman" and absolute paths are returned unchanged, so PATH lookups still work.
    """
    path = Path(value)
    if value and not path.is_absolute() and (anchored := APP_DIR / path).exists():
        return str(anchored)
    return value


def resolve_dolphintool(value: str) -> str:
    """Accept DolphinTool itself, the Dolphin.exe beside it, or the Dolphin folder.

    A relative value such as ./tools/dolphintool/2506/DolphinTool.exe is relative to the app folder, not to
    whatever directory the app happened to be launched from. The result is always safe to execute.
    """
    if not value:
        return ""
    names = ("DolphinTool.exe", "dolphin-tool", "DolphinTool")
    lowered = {n.lower() for n in names}
    path = Path(value)
    if not path.is_absolute() and (anchored := APP_DIR / path).exists():
        path = anchored
    if path.is_file() and path.name.lower() in lowered:
        return str(path)
    if (found := shutil.which(value)) and Path(found).name.lower() in lowered:
        return value
    folder = path if path.is_dir() else path.parent
    for name in names:
        if (folder / name).is_file():
            return str(folder / name)
    return ""


# --- Disk I/O gate: one disk-heavy tool at a time on a spinning drive ------------
class _Aborted(Exception):
    """Raised in a worker when the batch is stopped while it waits for a disk or for temp space."""


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _win_disk_info(path: str) -> tuple[object | None, bool | None, str]:
    """(physical disk number, spins?, note) for the volume holding `path`, straight from the storage driver.

    Uses the same two IOCTLs as Windows' own tools: GET_DEVICE_NUMBER (which physical disk, so two
    partitions of one HDD share a gate) and the seek-penalty property (HDD = True, SSD / NVMe = False).
    Either can come back unknown (network drives, some RAID / virtual disks) -> None, and `note` says why.
    Raises OSError only when the volume could not be opened at all.
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumePathNameW.restype = wintypes.BOOL
    k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    wintypes.LPVOID]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD), ("Extra", ctypes.c_ubyte * 1)]

    class SeekPenalty(ctypes.Structure):
        _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD), ("IncursSeekPenalty", ctypes.c_ubyte)]

    class DeviceNumber(ctypes.Structure):
        _fields_ = [("DeviceType", wintypes.DWORD), ("DeviceNumber", wintypes.DWORD),
                    ("PartitionNumber", wintypes.DWORD)]

    invalid = ctypes.c_void_p(-1).value
    last_error = lambda: str(ctypes.WinError(ctypes.get_last_error()))
    problems: list[str] = []

    # Ways to open the volume. First choice: the volume GUID of whatever is mounted at `path`, so a folder
    # mount point (D:\Mounts\HDD) reports its own disk instead of the drive letter it hangs off. Fallback:
    # the plain drive letter.
    devices: list[str] = []
    root, guid = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(1024)
    if k32.GetVolumePathNameW(path, root, 1024) and k32.GetVolumeNameForVolumeMountPointW(root.value, guid, 1024):
        devices.append(guid.value.rstrip("\\"))
    else:
        problems.append(f"volume lookup: {last_error()}")
    if len(drive := ntpath.splitdrive(path)[0]) == 2:
        devices.append("\\\\.\\" + drive)
    if not devices:
        raise OSError("; ".join(problems) or "no volume found for this path")

    for device in devices:
        # dwShareMode 3 = FILE_SHARE_READ | FILE_SHARE_WRITE, 3 = OPEN_EXISTING; access 0 = query only (no admin).
        handle = k32.CreateFileW(device, 0, 3, None, 3, 0, None)
        if handle is None or handle == invalid:
            problems.append(f"open {device}: {last_error()}")
            continue
        try:
            def ioctl(code, in_obj, out_obj) -> bool:
                returned = wintypes.DWORD()
                return bool(k32.DeviceIoControl(handle, code, ctypes.byref(in_obj) if in_obj is not None else None,
                                                ctypes.sizeof(in_obj) if in_obj is not None else 0,
                                                ctypes.byref(out_obj), ctypes.sizeof(out_obj),
                                                ctypes.byref(returned), None))

            notes: list[str] = []
            penalty = SeekPenalty()
            # IOCTL_STORAGE_QUERY_PROPERTY, StorageDeviceSeekPenaltyProperty (7), PropertyStandardQuery (0)
            if ioctl(0x002D1400, Query(7, 0), penalty):
                spins: bool | None = bool(penalty.IncursSeekPenalty)
            else:
                spins = None
                notes.append(f"seek-penalty query: {last_error()}")
            number = DeviceNumber()
            if ioctl(0x002D1080, None, number):  # IOCTL_STORAGE_GET_DEVICE_NUMBER
                disk: object | None = number.DeviceNumber
            else:
                disk = None
                notes.append(f"disk number query: {last_error()}")
        finally:
            k32.CloseHandle(handle)
        if spins is not None or disk is not None:
            return disk, spins, "; ".join(notes)
        problems.extend(notes)
    raise OSError("; ".join(problems))


def _posix_disk_info(path: str) -> tuple[object | None, bool | None, str]:
    """Same question on Linux, answered from sysfs (queue/rotational of the whole disk)."""
    try:
        st = os.stat(path)
        node = Path(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}").resolve()
        for cand in (node / "queue" / "rotational", node.parent / "queue" / "rotational"):
            if cand.is_file():
                return str(cand.parent.parent), cand.read_text().strip() == "1", ""
    except (OSError, ValueError) as exc:
        return None, None, str(exc)
    return None, None, "no sysfs entry for this device"


class DiskGate:
    """One disk-heavy tool at a time per spinning disk.

    On an HDD two sequential streams (say 7-Zip writing a workspace while chdman reads another one)
    become random seeks and throughput collapses. Every extraction, conversion and verification pass
    declares which paths it reads/writes and takes an exclusive lock on each physical disk involved,
    so the heads only ever serve one stream. Disks that are not gated (SSD, NVMe, RAM disk) are never
    blocked, so an HDD source feeding a fast temp folder still overlaps extraction with conversion.

    mode: "auto" gates disks that report a seek penalty, "hdd" gates every disk, "ssd" gates nothing.
    """

    def __init__(self, mode: str = "auto"):
        self.mode = mode
        self.exempt: set = set()  # disk ids that are never gated (the user's RAM-disk / SSD temp folder)
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._locks: dict = {}

    def _probe(self, path: Path | str) -> tuple[tuple, bool | None, str]:
        """(disk id, spins?, why-unknown). A failed probe is reported, never silently swallowed."""
        p = _existing_parent(Path(os.path.abspath(path)))
        try:
            vol = os.stat(p).st_dev
        except OSError as exc:
            return ("path", str(p)), None, f"stat failed: {exc}"
        with self._lock:
            if (hit := self._cache.get(vol)) is not None:
                return hit
        probe = _win_disk_info if sys.platform == "win32" else _posix_disk_info
        try:
            disk, spins, note = probe(str(p))
        except Exception as exc:
            disk, spins, note = None, None, f"{type(exc).__name__}: {exc}"
        result = ((("disk", disk) if disk is not None else ("vol", vol)), spins, note)
        with self._lock:
            self._cache[vol] = result
        return result

    def disk_id(self, path: Path | str) -> tuple:
        return self._probe(path)[0]

    def kind(self, path: Path | str) -> str:
        spins = self._probe(path)[1]
        return "unknown" if spins is None else "HDD" if spins else "SSD"

    def describe(self, path: Path | str) -> str:
        """kind() for the log, with the reason when the drive couldn't be identified."""
        note = self._probe(path)[2]
        kind = self.kind(path)
        return f"{kind} ({note})" if kind == "unknown" and note else kind

    def key(self, path: Path | str):
        """Disk id if access to `path` must be serialized, else None."""
        if self.mode == "ssd":
            return None
        disk, spins, _ = self._probe(path)
        if disk in self.exempt:
            return None
        return disk if self.mode == "hdd" or spins else None

    @contextlib.contextmanager
    def hold(self, paths, stop: threading.Event, on_wait: Callable[[], None] | None = None):
        keys = sorted({k for p in paths if (k := self.key(p)) is not None}, key=repr)  # fixed order: no deadlock
        taken = []
        try:
            for k in keys:
                with self._lock:
                    lock = self._locks.setdefault(k, threading.Lock())
                if not lock.acquire(blocking=False):
                    if on_wait:
                        on_wait()
                        on_wait = None
                    while not lock.acquire(timeout=0.25):
                        if stop.is_set():
                            raise _Aborted
                taken.append(lock)
            if stop.is_set():
                raise _Aborted
            yield
        finally:
            for lock in reversed(taken):
                lock.release()


# --- Process supervision: hung-tool watchdog and locked-file retries -----------------
STALL_RETRIES = 1  # a stalled tool is restarted this many times before the title is skipped


KILL_GRACE = 20.0  # seconds a killed tool gets to really exit before it is abandoned


IO_RETRY_ATTEMPTS = 40  # a file another program holds open (antivirus, indexer) is retried ...


IO_RETRY_DELAY = 3.0  # ... every few seconds, ~2 minutes in all


class _Stalled(Exception):
    """A tool made no progress for `minutes` and was killed. `abandoned`: it would not die (stuck in a
    disk request), so it may still hold its files."""

    def __init__(self, minutes: float, stage: str, abandoned: bool = False):
        super().__init__(f"no progress for {minutes:g} min")
        self.minutes, self.stage, self.abandoned = minutes, stage, abandoned


def _cpu_seconds(pid: int) -> float | None:
    """CPU time (user + kernel) the process has used so far; None if it can't be read."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            k32.GetProcessTimes.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                           ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                ticks = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime  # 100 ns units
                return (ticks(kernel) + ticks(user)) / 1e7
            finally:
                k32.CloseHandle(handle)
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def _tree_size(path: Path | None) -> int:
    """Bytes in a file or folder (-1 if unreadable). Only ever compared with its previous value."""
    if path is None:
        return -1
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return -1


class _Watchdog:
    """Kills a tool that has made no progress for `minutes` (0 = never).

    Progress is any of: output from the tool (ping), growth of the watched file/folder, or the tool using
    CPU. A tool blocked on a dead drive shows none of them; a working one always shows at least one, even
    a silent read-only hash on a slow disk.
    """

    INTERVAL = 5.0  # seconds between checks

    def __init__(self, proc: subprocess.Popen, minutes: float, watch: Path | None = None):
        self.proc, self.limit, self.watch = proc, minutes * 60, watch
        self.stalled = threading.Event()
        self._done = threading.Event()
        self._last = time.monotonic()

    def ping(self) -> None:
        self._last = time.monotonic()

    def __enter__(self) -> "_Watchdog":
        if self.limit > 0:
            threading.Thread(target=self._watch, daemon=True, name="watchdog").start()
        return self

    def __exit__(self, *exc) -> None:
        self._done.set()

    def _watch(self) -> None:
        size, cpu = _tree_size(self.watch), _cpu_seconds(self.proc.pid)
        patience = self.limit if cpu is not None else self.limit * 3  # fewer signals -> be more patient
        tick = time.monotonic()
        while not self._done.wait(self.INTERVAL):
            now = time.monotonic()
            if now - tick > self.INTERVAL * 6:  # the PC slept; that isn't the tool's fault
                self._last = now
            tick = now
            new_size, new_cpu = _tree_size(self.watch), _cpu_seconds(self.proc.pid)
            if new_size != size:
                size, self._last = new_size, now
            if cpu is not None and new_cpu is not None and new_cpu - cpu >= 0.5:
                cpu, self._last = new_cpu, now
            if now - self._last > patience:
                self.stalled.set()
                try:
                    self.proc.kill()
                except OSError:
                    pass
                return


# -----------------------------------------------------------------------------
# Tool processes die with the app (Windows)
# -----------------------------------------------------------------------------
# Killing a Windows program (End Task, a crash, a frozen window) does not kill the programs it started, so
# chdman / DolphinTool / maxcso / 7-Zip would carry on until their current disc finished. Every tool process is
# therefore put in a job object that Windows tears down - killing all of its members - the moment this app's
# last handle to it closes, which happens however the app ends.
_tool_job = None  # the job handle; kept for the life of the app so the job only closes when the app does


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JobBasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def bind_to_app_lifetime(proc: subprocess.Popen) -> None:
    """Make proc die when this app does, however the app ends. Best effort: never raises."""
    global _tool_job
    if sys.platform != "win32":
        return
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _tool_job is None:
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = _JobExtendedLimits()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = extended limits
                return
            _tool_job = job
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject(_tool_job, wintypes.HANDLE(int(proc._handle)))
    except Exception:
        pass  # the normal kill-on-close paths still apply


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
