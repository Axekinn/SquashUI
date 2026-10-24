"""Small CPU / GPU / RAM / Disk readout for the sidebar. Windows only, no extra packages (ctypes + PDH)."""
from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from .theme import AlignC, T

_WIN = sys.platform == "win32"
INTERVAL = 1.5  # seconds between samples
_PDH_FMT_DOUBLE = 0x200


class _Sampler:
    """Reads the four figures on a background thread; .values is {name: percent or None}."""

    def __init__(self) -> None:
        self.values: dict[str, float | None] = {"CPU": None, "GPU": None, "RAM": None, "Disk": None}
        self.ram_text = ""
        self._stop = threading.Event()
        self._prev_cpu: tuple[int, int, int] | None = None
        self._pdh = None
        self._q_disk = self._c_disk = self._q_gpu = self._c_gpu = None
        self._gpu_opened = 0.0

    def start(self) -> None:
        if _WIN:
            threading.Thread(target=self._loop, name="resource-monitor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        try:
            self._pdh = _Pdh()
        except Exception:
            self._pdh = None
        while not self._stop.is_set():
            for name, fn in (("CPU", self._cpu), ("RAM", self._ram), ("Disk", self._disk), ("GPU", self._gpu)):
                try:
                    self.values[name] = fn()
                except Exception:
                    self.values[name] = None
            self._stop.wait(INTERVAL)

    def _cpu(self) -> float | None:
        import ctypes
        from ctypes import wintypes
        idle, kern, user = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return None
        f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime  # noqa: E731
        cur = (f(idle), f(kern), f(user))
        prev, self._prev_cpu = self._prev_cpu, cur
        if prev is None:
            return None
        d_idle = cur[0] - prev[0]
        total = (cur[1] - prev[1]) + (cur[2] - prev[2])  # kernel time already includes idle
        return max(0.0, min(100.0, 100.0 * (total - d_idle) / total)) if total > 0 else None

    def _ram(self) -> float | None:
        import ctypes

        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        m = MemStatus()
        m.dwLength = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        gb = 1024 ** 3
        self.ram_text = f"{(m.ullTotalPhys - m.ullAvailPhys) / gb:.1f} / {m.ullTotalPhys / gb:.1f} GB"
        return float(m.dwMemoryLoad)

    def _disk(self) -> float | None:
        if self._pdh is None:
            return None
        if self._q_disk is None:
            self._q_disk, self._c_disk = self._pdh.query(r"\PhysicalDisk(_Total)\% Disk Time")
        self._pdh.collect(self._q_disk)
        v = self._pdh.value(self._c_disk)
        return None if v is None else max(0.0, min(100.0, v))

    def _gpu(self) -> float | None:
        if self._pdh is None:
            return None
        now = time.monotonic()
        if self._q_gpu is None or now - self._gpu_opened > 30:  # new processes add new engine instances
            if self._q_gpu is not None:
                self._pdh.close(self._q_gpu)
            self._q_gpu, self._c_gpu = self._pdh.query(r"\GPU Engine(*)\Utilization Percentage")
            self._gpu_opened = now
            self._pdh.collect(self._q_gpu)
            return self.values["GPU"]
        self._pdh.collect(self._q_gpu)
        per_type: dict[str, float] = {}
        for name, val in self._pdh.array(self._c_gpu):
            kind = name.rsplit("engtype_", 1)[-1]
            per_type[kind] = per_type.get(kind, 0.0) + val
        return max(0.0, min(100.0, max(per_type.values()))) if per_type else 0.0


class _Pdh:
    """Minimal Performance Data Helper wrapper (English counter names, so it works on any Windows language)."""

    def __init__(self) -> None:
        import ctypes
        self.ct = ctypes
        self.dll = ctypes.WinDLL("pdh")

    def query(self, path: str):
        ct = self.ct
        q, c = ct.c_void_p(), ct.c_void_p()
        if self.dll.PdhOpenQueryW(None, 0, ct.byref(q)) != 0:
            raise OSError("PdhOpenQuery")
        if self.dll.PdhAddEnglishCounterW(q, path, 0, ct.byref(c)) != 0:
            self.dll.PdhCloseQuery(q)
            raise OSError("PdhAddEnglishCounter " + path)
        self.dll.PdhCollectQueryData(q)
        return q, c

    def collect(self, q) -> None:
        self.dll.PdhCollectQueryData(q)

    def close(self, q) -> None:
        self.dll.PdhCloseQuery(q)

    def value(self, c) -> float | None:
        ct = self.ct

        class Val(ct.Structure):
            _fields_ = [("CStatus", ct.c_uint32), ("v", ct.c_double)]
        out = Val()
        if self.dll.PdhGetFormattedCounterValue(c, _PDH_FMT_DOUBLE, None, ct.byref(out)) != 0:
            return None
        return out.v

    def array(self, c) -> list[tuple[str, float]]:
        ct = self.ct

        class Val(ct.Structure):
            _fields_ = [("CStatus", ct.c_uint32), ("v", ct.c_double)]

        class Item(ct.Structure):
            _fields_ = [("name", ct.c_wchar_p), ("val", Val)]
        size, count = ct.c_uint32(0), ct.c_uint32(0)
        self.dll.PdhGetFormattedCounterArrayW(c, _PDH_FMT_DOUBLE, ct.byref(size), ct.byref(count), None)
        if size.value == 0:
            return []
        buf = ct.create_string_buffer(size.value)
        if self.dll.PdhGetFormattedCounterArrayW(c, _PDH_FMT_DOUBLE, ct.byref(size), ct.byref(count), buf) != 0:
            return []
        items = ct.cast(buf, ct.POINTER(Item))
        return [(items[i].name or "", items[i].val.v) for i in range(count.value) if items[i].val.CStatus in (0, 1)]


class ResourceMonitor(QWidget):
    """Four compact columns: label, percentage, thin bar."""

    ORDER = ("CPU", "GPU", "RAM", "Disk")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._sampler = _Sampler()
        self._sampler.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(int(INTERVAL * 1000))
        self.destroyed.connect(lambda *_: self._sampler.stop())

    def _tick(self) -> None:
        v = self._sampler.values
        tip = [f"{k}: {'n/a' if v[k] is None else f'{v[k]:.0f}%'}" for k in self.ORDER]
        if self._sampler.ram_text:
            tip[2] += f"  ({self._sampler.ram_text})"
        self.setToolTip("\n".join(tip) + "\nDisk = activity time; GPU = busiest engine")
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width() / len(self.ORDER)
        small, big = QFont(self.font()), QFont(self.font())
        small.setPointSizeF(max(6.5, self.font().pointSizeF() - 2.5))
        big.setPointSizeF(max(8.0, self.font().pointSizeF() - 1))
        big.setWeight(QFont.Weight.DemiBold)
        for i, key in enumerate(self.ORDER):
            val = self._sampler.values[key]
            x = i * w
            p.setFont(small)
            p.setPen(T.fg3)
            p.drawText(QRectF(x, 3, w, 12), AlignC, key)
            p.setFont(big)
            p.setPen(T.fg if val is not None else T.fg3)
            p.drawText(QRectF(x, 15, w, 15), AlignC, "n/a" if val is None else f"{val:.0f}%")
            bar = QRectF(x + 8, 33, w - 16, 3)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.select)
            p.drawRoundedRect(bar, 1.5, 1.5)
            if val is not None:
                p.setBrush(T.bad if val >= 90 else T.warn if val >= 75 else T.accent)
                p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * val / 100, bar.height()), 1.5, 1.5)
