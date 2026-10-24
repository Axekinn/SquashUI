"""Per-file control of downloads (stop one, add one mid-run), deleting what they left behind, and the helpers behind the
Download page's right-click menu. Runs against a real local HTTP server and a stand-in libtorrent: no network, no Qt."""
from __future__ import annotations

import hashlib
import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from squashui import dlfiles, torrent_runners, torrents
from squashui.downloads import DownloadRunner, RemoteFile


def payload(name: str, size: int) -> bytes:
    seed = hashlib.sha256(name.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


class Site(http.server.ThreadingHTTPServer):
    """Serves {path: bytes}. A path in `slow` is sent in small slices with a pause between them; Range is honoured."""
    daemon_threads = True

    def __init__(self, files: dict[str, bytes], slow: dict[str, float] | None = None):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.files, self.slow = files, slow or {}
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}{path}"


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        data = self.server.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        start = 0
        if rng := self.headers.get("Range"):
            start = int(rng.split("=")[1].split("-")[0])
        body = data[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        pause = self.server.slow.get(self.path, 0)
        try:
            for at in range(0, len(body), 64 * 1024):
                self.wfile.write(body[at:at + 64 * 1024])
                self.wfile.flush()
                if pause:
                    time.sleep(pause)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_HEAD = do_GET


def until(cond, timeout: float = 15.0, what: str = "condition") -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


class RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="squashui_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.events: list[tuple] = []
        self.pause = threading.Event()
        self.pause.set()
        self.sites: list[Site] = []

    def site(self, files, slow=None) -> Site:
        s = Site(files, slow)
        self.sites.append(s)
        self.addCleanup(s.server_close)
        self.addCleanup(s.shutdown)
        return s

    def start(self, jobs, threads=4) -> DownloadRunner:
        self.runner = DownloadRunner(jobs, self.tmp / "out", threads, False, False, self.events.append, self.pause)
        self.thread = threading.Thread(target=self.runner.run, daemon=True)
        self.thread.start()
        return self.runner

    def status(self, idx: int) -> str:
        rows = [e[2] for e in self.events if e[0] == "dl_row" and e[1] == idx]
        return rows[-1] if rows else ""

    def finish(self) -> None:
        self.thread.join(20)
        self.assertFalse(self.thread.is_alive(), "run never finished")
        self.assertEqual(self.events[-1][0], "dl_done")

    def summary(self):
        return self.events[-1][1]


class WebRunnerTests(RunnerCase):
    def test_plain_run_is_unchanged(self):
        data = {f"/f{i}": payload(f"f{i}", 50_000 + i) for i in range(3)}
        site = self.site(data)
        r = self.start([(i, RemoteFile(f"f{i}", site.url(f"/f{i}"), len(data[f"/f{i}"]))) for i in range(3)])
        self.finish()
        for i in range(3):
            self.assertEqual((self.tmp / "out" / f"f{i}").read_bytes(), data[f"/f{i}"])
        sm = self.summary()
        self.assertEqual((sm.done, sm.skipped, len(sm.failures), sm.stopped), (3, 0, 0, 0))
        self.assertEqual((r.files_finished, r.files_total, r.bytes_done, r.bytes_total),
                         (3, 3, sum(map(len, data.values())), sum(map(len, data.values()))))
        self.assertFalse(r._live)

    def test_stop_a_waiting_file(self):
        big = payload("big", 600_000)
        data = {"/big": big, "/b": payload("b", 20_000), "/c": payload("c", 20_000)}
        site = self.site(data, slow={"/big": 0.05})
        jobs = [(i, RemoteFile(n, site.url("/" + n), len(data["/" + n]))) for i, n in enumerate(("big", "b", "c"))]
        r = self.start(jobs, threads=1)
        until(lambda: r.is_busy(0), what="first file running")
        self.assertEqual(r.cancel([1]), [1])
        self.assertEqual(self.status(1), "Stopped")  # dropped from the queue at once, nothing to wind down
        self.assertFalse(r.is_live(1))
        self.finish()
        self.assertFalse((self.tmp / "out" / "b").exists())
        self.assertEqual((self.tmp / "out" / "c").read_bytes(), data["/c"])
        self.assertEqual((r.files_total, r.files_finished, r.bytes_total, r.bytes_done),
                         (2, 2, len(big) + 20_000, len(big) + 20_000))
        self.assertEqual(self.summary().stopped, 1)
        self.assertEqual(r.cancel([1]), [])  # not part of the run any more

    def test_stop_a_running_file_keeps_its_partial_and_leaves_the_totals(self):
        big = payload("big", 2_000_000)
        data = {"/big": big, "/small": payload("small", 30_000)}
        site = self.site(data, slow={"/big": 0.05})
        r = self.start([(0, RemoteFile("big", site.url("/big"), len(big))),
                        (1, RemoteFile("small", site.url("/small"), 30_000))], threads=2)
        part = self.tmp / "out" / "big.part"
        until(lambda: part.exists() and part.stat().st_size > 0, what="partial file")
        self.assertEqual(r.cancel([0]), [0])
        self.assertEqual(self.status(0), "Stopping...")
        until(lambda: self.status(0) == "Stopped", what="stop to land")
        self.finish()
        self.assertTrue(part.exists(), "partial download must be kept so it can resume")
        self.assertFalse((self.tmp / "out" / "big").exists())
        self.assertEqual((r.files_total, r.files_finished, r.bytes_total, r.bytes_done), (1, 1, 30_000, 30_000))
        self.assertFalse(r.is_busy(0))
        self.assertEqual(self.summary().stopped, 1)

    def test_stop_works_while_the_run_is_paused(self):
        big = payload("big", 2_000_000)
        site = self.site({"/big": big}, slow={"/big": 0.05})
        r = self.start([(0, RemoteFile("big", site.url("/big"), len(big)))], threads=1)
        until(lambda: (self.tmp / "out" / "big.part").exists(), what="download to begin")
        self.pause.clear()  # the run is paused...
        time.sleep(0.4)
        r.cancel([0])  # ...and the file is stopped without having to Resume first
        until(lambda: self.status(0) == "Stopped", what="stop while paused")
        self.finish()

    def test_add_a_file_while_running(self):
        big = payload("big", 1_500_000)
        data = {"/big": big, "/late": payload("late", 40_000)}
        site = self.site(data, slow={"/big": 0.05})
        r = self.start([(0, RemoteFile("big", site.url("/big"), len(big)))], threads=2)
        until(lambda: r.is_busy(0), what="first file running")
        late = RemoteFile("late", site.url("/late"), 40_000)
        self.assertEqual(r.add([(1, late), (0, RemoteFile("big", site.url("/big"), len(big)))]), [1])  # row 0 is already in
        self.assertEqual((r.files_total, r.bytes_total), (2, len(big) + 40_000))
        until(lambda: self.status(1) == "Done", what="the added file")
        self.assertTrue(r.is_busy(0), "the first file should still be going")
        self.finish()
        self.assertEqual((self.tmp / "out" / "late").read_bytes(), data["/late"])
        self.assertEqual((r.files_finished, r.files_total, r.bytes_done, r.bytes_total),
                         (2, 2, len(big) + 40_000, len(big) + 40_000))

    def test_a_finished_run_turns_added_files_away(self):
        site = self.site({"/a": payload("a", 1000)})
        r = self.start([(0, RemoteFile("a", site.url("/a"), 1000))])
        self.finish()
        self.assertIsNone(r.add([(1, RemoteFile("a2", site.url("/a"), 1000))]))

    def test_a_stopped_file_can_be_started_again_and_resumes(self):
        big = payload("big", 2_000_000)
        keepalive = payload("keep", 3_000_000)
        site = self.site({"/big": big, "/keep": keepalive}, slow={"/big": 0.03, "/keep": 0.05})
        r = self.start([(0, RemoteFile("big", site.url("/big"), len(big))),
                        (1, RemoteFile("keep", site.url("/keep"), len(keepalive)))], threads=2)
        part = self.tmp / "out" / "big.part"
        until(lambda: part.exists() and part.stat().st_size > 100_000, what="partial")
        r.cancel([0])
        until(lambda: self.status(0) == "Stopped", what="stop")
        kept = part.stat().st_size
        self.assertEqual(r.add([(0, RemoteFile("big", site.url("/big"), len(big)))]), [0])
        self.assertEqual(r.files_total, 2)
        self.finish()
        self.assertEqual((self.tmp / "out" / "big").read_bytes(), big)  # finished from the kept partial
        self.assertGreater(kept, 0)
        self.assertEqual((r.files_finished, r.files_total), (2, 2))
        self.assertEqual(r.bytes_done, r.bytes_total)

    def test_a_failed_file_started_again_isnt_counted_twice(self):
        site = self.site({"/ok": payload("ok", 800_000)}, slow={"/ok": 0.05})
        r = self.start([(0, RemoteFile("ok", site.url("/ok"), 800_000)),
                        (1, RemoteFile("gone", site.url("/gone"), 500))], threads=2)
        until(lambda: self.status(1).startswith("Failed"), what="the 404")
        self.assertEqual(len(r.failures), 1)
        self.assertEqual(r.add([(1, RemoteFile("gone", site.url("/gone"), 500))]), [1])
        until(lambda: len(r.failures) == 1 and r.is_live(0) and not r.is_live(1), what="second failure")
        self.finish()
        self.assertEqual((len(r.failures), r.files_finished, r.files_total), (1, 2, 2))
        self.assertEqual(r.bytes_total, 800_000)  # the failed file doesn't hold the bar back

    def test_a_finished_file_ignores_a_late_stop(self):
        site = self.site({"/a": payload("a", 1000), "/slow": payload("s", 500_000)}, slow={"/slow": 0.05})
        r = self.start([(0, RemoteFile("a", site.url("/a"), 1000)), (1, RemoteFile("s", site.url("/slow"), 500_000))])
        until(lambda: self.status(0) == "Done", what="first file")
        self.assertEqual(r.cancel([0]), [])
        r.cancel([1])
        self.finish()
        self.assertEqual(self.status(0), "Done")
        self.assertEqual((r.done, r.stopped), (1, 1))


class FileHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="squashui_test_")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dest = self.tmp / "dl"
        self.dest.mkdir()

    def touch(self, rel: str, data: bytes = b"x") -> Path:
        p = self.dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_files_on_disk_finds_the_file_and_its_partial(self):
        rf = RemoteFile("disc/game.iso", "http://x/game.iso", 10, "item")
        self.assertEqual(dlfiles.files_on_disk(rf, self.dest, True), [])
        part = self.touch("item/disc/game.iso.part")
        self.assertEqual(dlfiles.files_on_disk(rf, self.dest, True), [part])
        final = self.touch("item/disc/game.iso")
        self.assertEqual(dlfiles.files_on_disk(rf, self.dest, True), [final, part])
        self.assertEqual(dlfiles.files_on_disk(rf, self.dest, False), [])  # per-item setting changes the place

    def test_an_unsafe_name_never_resolves(self):
        rf = RemoteFile("../../evil", "http://x/e", 1)
        self.assertEqual(dlfiles.files_on_disk(rf, self.dest, False), [])
        self.assertIsNone(dlfiles.nearest_folder(rf, self.dest, False))

    def test_delete_removes_files_and_the_folders_they_leave_empty(self):
        a, b = self.touch("item/disc/a.iso"), self.touch("item/disc/b.iso.part")
        keep = self.touch("item/other/keep.txt")
        n, problems = dlfiles.delete_files([a, b], self.dest)
        self.assertEqual((n, problems), (2, []))
        self.assertFalse((self.dest / "item" / "disc").exists())
        self.assertTrue(keep.exists())  # a sibling folder with something in it stays
        self.assertTrue(self.dest.exists())  # and so does the save folder itself

    def test_delete_never_leaves_the_save_folder(self):
        outside = self.tmp / "outside.txt"
        outside.write_bytes(b"x")
        n, problems = dlfiles.delete_files([outside], self.dest)
        self.assertEqual(n, 0)
        self.assertEqual(len(problems), 1)
        self.assertTrue(outside.exists())
        link = self.dest / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("no symlinks here")
        self.assertEqual(dlfiles.delete_files([link], self.dest)[0], 0)  # a link out of the folder isn't followed
        self.assertTrue(outside.exists())

    def test_delete_of_a_missing_file_is_fine(self):
        self.assertEqual(dlfiles.delete_files([self.dest / "nope.iso"], self.dest), (0, []))

    def test_delete_retries_a_file_that_is_briefly_in_use(self):
        f = self.touch("busy.iso")
        real, calls = Path.unlink, []

        def flaky(self_, *a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "The process cannot access the file")
            return real(self_, *a, **k)

        with mock.patch.object(Path, "unlink", flaky):
            self.assertEqual(dlfiles.delete_files([f], self.dest, patience=10), (1, []))
        self.assertFalse(f.exists())

    def test_delete_reports_a_file_that_stays_locked(self):
        f = self.touch("locked.iso")
        with mock.patch.object(Path, "unlink", side_effect=PermissionError(13, "in use")):
            n, problems = dlfiles.delete_files([f], self.dest, patience=0.6)
        self.assertEqual(n, 0)
        self.assertIn("locked.iso", problems[0])

    def test_nearest_folder_walks_up_but_not_out(self):
        rf = RemoteFile("a/b/c.iso", "http://x/c", 1, "grp")
        self.assertEqual(dlfiles.nearest_folder(rf, self.dest, True), self.dest)
        self.touch("grp/a/x.txt")
        self.assertEqual(dlfiles.nearest_folder(rf, self.dest, True), self.dest / "grp" / "a")
        shutil.rmtree(self.dest)
        self.assertIsNone(dlfiles.nearest_folder(rf, self.dest, True))

    def test_compact_plan_renumbers_what_is_left(self):
        self.assertEqual(dlfiles.compact_plan(6, {1, 2, 5}), [0, None, None, 1, 2, None])
        self.assertEqual(dlfiles.compact_plan(3, set()), [0, 1, 2])
        self.assertEqual(dlfiles.compact_plan(2, {0, 1}), [None, None])

    def test_magnet_and_per_item_helpers(self):
        t = RemoteFile("n/f.iso", "torrent:abc123:4", 1, "n", source="Torrent")
        self.assertEqual(dlfiles.magnet_for(t), "magnet:?xt=urn:btih:abc123")
        self.assertIsNone(dlfiles.magnet_for(RemoteFile("f", "https://x/f", 1)))
        self.assertFalse(dlfiles.per_item_for(t, True))  # torrents keep their own layout
        self.assertTrue(dlfiles.per_item_for(RemoteFile("f", "https://x/f", 1), True))

    def test_explorer_command_only_on_windows(self):
        f = self.touch("a.iso")
        with mock.patch.object(os, "name", "nt"):
            self.assertEqual(dlfiles.explorer_select_command(f), ["explorer", "/select,", str(f)])
            self.assertIsNone(dlfiles.explorer_select_command(self.dest / "missing"))
        with mock.patch.object(os, "name", "posix"):
            self.assertIsNone(dlfiles.explorer_select_command(f))


# ---- a stand-in libtorrent: enough of the API for TorrentRunner's swarm loop -------------------------------------
class _Info:
    def __init__(self, sizes):
        self.sizes = sizes

    def num_files(self): return len(self.sizes)
    def name(self): return "Pack"


class _Handle:
    def __init__(self, info, priorities, step):
        self.info, self.prio, self.step, self.removed = info, list(priorities), step, False
        self.have, self.calls, self.polls = [0] * len(info.sizes), [], 0

    def status(self):
        return types.SimpleNamespace(errc=types.SimpleNamespace(value=lambda: 0), state="downloading", num_peers=3)

    def file_progress(self):
        self.polls += 1
        if self.polls > 1:  # the first look is the 'what's already on disk' baseline
            for i, size in enumerate(self.info.sizes):
                if self.prio[i] > 0:
                    self.have[i] = min(size, self.have[i] + self.step)
        return list(self.have)

    def file_priority(self, fi, p):
        self.calls.append((fi, p))
        self.prio[fi] = p

    def pause(self): pass
    def resume(self): pass


class _Session:
    def __init__(self, pack=None):
        self.handles = []

    def apply_settings(self, pack): pass
    def pause(self): pass
    def resume(self): pass

    def add_torrent(self, params):
        h = _Handle(params.ti, params.file_priorities, self.step)
        self.handles.append(h)
        return h

    def remove_torrent(self, h):
        h.removed = True


class TorrentRunnerTests(RunnerCase):
    def setUp(self) -> None:
        super().setUp()
        self.session = _Session()
        self.session.step = 400
        lt = types.SimpleNamespace(session=lambda pack: self.session, bdecode=lambda b: b,
                                   torrent_info=lambda b: _Info([1000, 1000, 1000]),
                                   add_torrent_params=lambda: types.SimpleNamespace())
        patches = [mock.patch.dict(sys.modules, {"libtorrent": lt}),
                   mock.patch.object(torrent_runners, "TORRENT_DIR", self.tmp / "torrents"),
                   mock.patch.object(torrents.ENGINE, "ses", None)]
        (self.tmp / "torrents").mkdir()
        (self.tmp / "torrents" / "ab12.torrent").write_bytes(b"fake")
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def torrent_jobs(self, n=3):
        return [(i, RemoteFile(f"Pack/f{i}.bin", f"torrent:ab12:{i}", 1000, "Pack", source="Torrent")) for i in range(n)]

    def start_swarm(self, jobs):
        self.runner = torrent_runners.TorrentRunner(
            jobs, self.tmp / "out", self.events.append, self.pause, {"adapter": "", "require_vpn": False, "dht": True})
        self.thread = threading.Thread(target=self.runner.run, daemon=True)
        self.thread.start()
        return self.runner

    def test_swarm_run_still_completes(self):
        r = self.start_swarm(self.torrent_jobs())
        self.finish()
        self.assertEqual((self.summary().done, self.summary().stopped), (3, 0))
        self.assertEqual((r.files_finished, r.files_total, r.bytes_done, r.bytes_total), (3, 3, 3000, 3000))
        self.assertTrue(self.session.handles[0].removed)
        self.assertFalse(r._live)

    def test_stop_one_file_of_a_torrent(self):
        r = self.start_swarm(self.torrent_jobs())
        until(lambda: "%" in self.status(1), what="download to start")
        self.assertEqual(r.cancel([1]), [1])
        until(lambda: self.status(1) == "Stopped", what="stop")
        self.finish()
        h = self.session.handles[0]
        self.assertIn((1, 0), h.calls)  # file 1 told not to download any more
        self.assertEqual(self.status(0), "Done")
        self.assertEqual(self.status(2), "Done")
        self.assertEqual((self.summary().done, self.summary().stopped), (2, 1))
        self.assertEqual((r.files_total, r.files_finished, r.bytes_total), (2, 2, 2000))
        self.assertEqual(r.bytes_done, r.bytes_total)

    def test_stopping_every_file_leaves_the_swarm(self):
        r = self.start_swarm(self.torrent_jobs(2))
        until(lambda: "%" in self.status(0), what="download to start")
        r.cancel([0, 1])
        self.finish()
        self.assertTrue(self.session.handles[0].removed)
        self.assertEqual((self.summary().done, self.summary().stopped, r.files_total), (0, 2, 0))

    def test_a_file_stopped_before_the_swarm_starts_is_never_requested(self):
        r = self.start_swarm(self.torrent_jobs(2))
        r.cancel([1])  # races the thread start: either way it must never be downloaded
        self.finish()
        self.assertEqual(self.status(1), "Stopped")
        self.assertEqual(self.session.handles[0].have[1], 0)

    def test_stopping_works_while_paused(self):
        r = self.start_swarm(self.torrent_jobs(2))
        until(lambda: "%" in self.status(0), what="download to start")
        self.pause.clear()
        time.sleep(0.7)
        r.cancel([0, 1])
        until(lambda: self.status(0) == "Stopped" and self.status(1) == "Stopped", what="stop while paused")
        self.pause.set()
        self.finish()

    def test_a_swarm_run_cannot_take_new_files(self):
        r = self.start_swarm(self.torrent_jobs(2))
        self.assertIsNone(r.add([(5, RemoteFile("x", "torrent:ab12:0", 1))]))  # the window queues it behind the run
        r.shutdown()
        self.thread.join(10)


if __name__ == "__main__":
    unittest.main()
