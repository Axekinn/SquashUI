"""Fixtures shared by the tests: tiny but format-correct PKG / ISO / SFO files, fake command-line tools, a local
web server with Range support, and a way to run a BatchRunner to completion and collect its events."""
from __future__ import annotations

import contextlib
import http.server
import os
import stat
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TempDirTest(unittest.TestCase):
    """A fresh temporary folder per test (self.tmp), removed afterwards."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="squashui_test_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def write(self, rel: str, data: bytes | str = b"x") -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path


# ---- PS4 PKG / SFO ---------------------------------------------------------------------------------------
def build_sfo(entries: dict[str, str | int]) -> bytes:
    keys = sorted(entries)
    key_table = b"".join(k.encode() + b"\0" for k in keys)
    key_offsets, pos = [], 0
    for k in keys:
        key_offsets.append(pos)
        pos += len(k) + 1
    data_table, index = b"", b""
    for i, k in enumerate(keys):
        v = entries[k]
        if isinstance(v, int):
            raw, fmt, length, maxlen = struct.pack("<I", v), 0x0404, 4, 4
        else:
            raw = v.encode() + b"\0"
            fmt, length, maxlen = 0x0204, len(raw), len(raw)
        index += struct.pack("<HHIII", key_offsets[i], fmt, length, maxlen, len(data_table))
        data_table += raw
    key_start = 20 + len(index)
    data_start = key_start + len(key_table)
    return struct.pack("<4sIIII", b"\0PSF", 0x101, key_start, data_start, len(keys)) + index + key_table + data_table


def build_pkg(path: Path, content_id: str, category: str, title: str = "Test Game", version: str = "01.00",
              magic: bytes = b"\x7fCNT") -> Path:
    """A PS4 .pkg that is just a header, one entry table and a param.sfo: what read_pkg_info looks at."""
    sfo = build_sfo({"CATEGORY": category, "TITLE": title, "TITLE_ID": content_id[7:16], "APP_VER": version})
    table_at, sfo_at = 0x100, 0x200
    head = bytearray(0x80)
    head[0:4] = magic
    struct.pack_into(">I", head, 0x10, 1)
    struct.pack_into(">I", head, 0x18, table_at)
    head[0x40:0x40 + len(content_id)] = content_id.encode()
    entry = struct.pack(">6I", 0x1000, 0, 0, 0, sfo_at, len(sfo))
    blob = bytearray(sfo_at + len(sfo))
    blob[:0x80] = head
    blob[table_at:table_at + 32] = entry
    blob[sfo_at:] = sfo
    path.write_bytes(bytes(blob))
    return path


# ---- ISO 9660 ----------------------------------------------------------------------------------------------
def _both32(n: int) -> bytes:
    return struct.pack("<I", n) + struct.pack(">I", n)


def _record(name: bytes, extent: int, size: int, is_dir: bool) -> bytes:
    n = len(name)
    length = 33 + n + (0 if n % 2 else 1)
    rec = bytearray(length)
    rec[0] = length
    rec[2:10] = _both32(extent)
    rec[10:18] = _both32(size)
    rec[25] = 2 if is_dir else 0
    rec[28:32] = struct.pack("<H", 1) + struct.pack(">H", 1)
    rec[32] = n
    rec[33:33 + n] = name
    return bytes(rec)


def build_iso(path: Path, tree: dict, volume: str = "TESTDISC") -> Path:
    """A real ISO 9660 image from {name: bytes | {subfolder tree}}. Enough for the app's own readers."""
    sectors: dict[int, bytes] = {}
    nxt = [18]

    def alloc(data: bytes) -> tuple[int, int]:
        start = nxt[0]
        count = max(1, -(-len(data) // 2048))
        nxt[0] += count
        sectors[start] = data
        return start, len(data)

    def write_dir(entries: dict, parent_extent: int | None) -> tuple[int, int]:
        extent = nxt[0]
        nxt[0] += 1  # a folder here is always one sector
        recs = []
        for name, value in sorted(entries.items()):
            if isinstance(value, dict):
                ext, size = write_dir(value, extent)
                recs.append(_record(name.encode(), ext, size, True))
            else:
                ext, size = alloc(value)
                recs.append(_record(name.encode() + b";1", ext, size, False))
        body = _record(b"\x00", extent, 2048, True) + _record(b"\x01", parent_extent if parent_extent is not None else extent, 2048, True)
        body += b"".join(recs)
        assert len(body) <= 2048, "test ISO folder too large"
        sectors[extent] = body
        return extent, 2048

    root_extent, root_size = write_dir(tree, None)
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[40:72] = volume.encode().ljust(32)
    pvd[80:88] = _both32(nxt[0])
    pvd[156:190] = _record(b"\x00", root_extent, root_size, True)[:34]
    pvd[156] = 34
    out = bytearray(nxt[0] * 2048)
    out[16 * 2048:17 * 2048] = pvd
    for start, data in sectors.items():
        out[start * 2048:start * 2048 + len(data)] = data
    path.write_bytes(bytes(out))
    return path


# ---- fake command-line tools -------------------------------------------------------------------------------
def make_tool(path: Path, body: str) -> Path:
    """An executable Python script standing in for chdman / a PKG extractor."""
    path.write_text(f"#!{sys.executable}\nimport sys, os, pathlib\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


FAKE_CHDMAN = '''
a = sys.argv[1:]
if a[0] in ("createcd", "createdvd"):
    out = pathlib.Path(a[a.index("-o") + 1])
    src = pathlib.Path(a[a.index("-i") + 1])
    if "BROKEN" in src.name:
        print("Error: simulated failure"); sys.exit(1)
    out.write_bytes(b"MComprHD" + b"\\0" * 4096)
    print("Compression complete ... final ratio = 40.0%"); sys.exit(0)
if a[0] == "verify":
    ok = pathlib.Path(a[a.index("-i") + 1]).read_bytes()[:8] == b"MComprHD"
    print("Overall SHA-1 verification successful" if ok else "bad"); sys.exit(0 if ok else 1)
sys.exit(2)
'''

FAKE_PKG_EXTRACTOR = '''
pkg, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
if len(sys.argv) != 3:
    print("usage: pkg_extractor <pkg> <outdir>"); sys.exit(2)
out.mkdir(parents=True, exist_ok=True)
(out / "sce_sys").mkdir(exist_ok=True)
(out / "sce_sys" / "param.sfo").write_bytes(b"sfo")
if b"CATEGORY" in pkg.read_bytes() and b"gd\\0" in pkg.read_bytes():
    (out / "eboot.bin").write_bytes(b"elf")
(out / "data.bin").write_bytes(b"d" * 100)
print("done")
'''


# ---- a tiny HTTP server with Range support -----------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    pages: dict[str, str] = {}
    hits: list[tuple[str, str]] = []

    def log_message(self, *args) -> None:  # silence
        pass

    def do_GET(self) -> None:
        type(self).hits.append((self.path, self.headers.get("Range", "")))
        if self.path in self.pages:
            body = self.pages[self.path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = self.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def web_server(files: dict[str, bytes] | None = None, pages: dict[str, str] | None = None):
    handler = type("H", (_Handler,), {"files": files or {}, "pages": pages or {}, "hits": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()


def run_batch(cfg, only=None):
    """Run a BatchRunner to completion; return (events, summary)."""
    from squashui.batch import BatchRunner

    events: list[tuple] = []
    pause = threading.Event()
    pause.set()
    runner = BatchRunner(cfg, events.append, pause, only)
    runner.run()
    done = [e for e in events if e and e[0] == "done"]
    return events, (done[-1][1] if done else None)


def logs(events) -> str:
    return "\n".join(str(e[1]) for e in events if e and e[0] == "log")


WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", "Windows only")
POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "needs a POSIX shell script / shebang")
