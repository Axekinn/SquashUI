"""The torrent and TorBox download runners."""
from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from pathlib import Path

from .downloads import DL_MAX_PARALLEL, DownloadRunner, DownloadSummary, LinkError, RemoteFile
from .models import fmt_size
from .torbox import _torbox_match, torbox_add, torbox_cached, torbox_files
from .torrents import ENGINE, ip_is_local, TORRENT_DIR


class TorrentRunner(DownloadRunner):
    """Downloads the ticked files of one or more torrents. Same events and progress bookkeeping as the web
    downloader, so the file list, progress bar, pause button and summary all work unchanged."""

    def __init__(self, jobs, dest: Path, emit, pause_event: threading.Event, settings: dict):
        super().__init__(jobs, dest, 1, False, False, emit, pause_event)
        self.settings = settings
        self._queue.clear()  # the swarm loop works from self.jobs; the web workers' queue stays empty until TorBox fills it

    def set_threads(self, n: int) -> None:  # parallel-file count means nothing here; the swarm decides
        pass

    def add(self, jobs) -> None:
        """A swarm session is built from its file list up front, so it can't take files mid-run. None tells the window
        to line them up behind this run instead."""
        return None

    def _apply_cancels(self, tracks: list[dict]) -> None:
        """Files the person stopped: drop them from their torrent (priority 0), and leave the swarm for a torrent once
        nothing in it is wanted any more. Runs on the swarm thread, which is the only one that touches libtorrent."""
        if not self._cancelled:
            return
        for t in tracks:
            gone = [(idx, rf, fi) for idx, rf, fi in t["items"] if idx in self._cancelled and idx not in t["done"]]
            for idx, rf, fi in gone:
                t["done"].add(idx)
                if t["h"] is not None:
                    with contextlib.suppress(Exception):
                        t["h"].file_priority(fi, 0)
                self._finish_cancel(idx, rf)
            if gone and len(t["done"]) == len(t["items"]) and t["h"] is not None:
                with contextlib.suppress(Exception):
                    ENGINE.ses.remove_torrent(t["h"])
                t["h"] = None

    @staticmethod
    def _error_of(status) -> str:
        try:
            if status.errc.value():
                return str(status.errc.message())
        except AttributeError:
            pass
        return str(getattr(status, "error", "") or "")

    def _summary(self, started: float) -> DownloadSummary:
        return DownloadSummary(len(self.jobs), self.done, self.skipped, tuple(self.failures), self.bytes_received,
                               time.monotonic() - started, self.stopped)

    def _vpn_lost(self, ses, tracks: list[dict]) -> None:
        """Kill switch: stop everything the moment the VPN address disappears, carry on only once it is back."""
        ses.pause()
        self._emit(("log", "VPN adapter disappeared - ALL torrent traffic is paused until it comes back."))
        self._emit(("notice", "VPN adapter disappeared - all torrent traffic is paused until it comes back."))
        for t in tracks:
            for idx, _rf, _fi in t["items"]:
                if idx not in t["done"]:
                    self._emit(("dl_speed", idx, ""))
                    self._emit(("dl_row", idx, "Paused - VPN dropped"))
        while not self._stop.wait(3):
            self._apply_cancels(tracks)
            try:
                ENGINE.ensure(self.settings)
            except LinkError:
                continue
            ses.resume()
            self._emit(("log", f"VPN is back ({ENGINE.label}) - resuming."))
            self._emit(("notice", ""))
            return

    def _poll(self, t: dict, now: float) -> None:
        handle, status = t["h"], t["h"].status()
        if err := self._error_of(status):
            for idx, rf, _fi in t["items"]:
                if idx not in t["done"]:
                    t["done"].add(idx)
                    self._fail(idx, rf, err)
            return
        checking = "check" in str(status.state).lower()
        progress = handle.file_progress()
        for idx, rf, fi in t["items"]:
            if idx in t["done"]:
                continue
            size = rf.size or 0
            have = min(int(progress[fi]) if fi < len(progress) else 0, size)
            if checking:
                self._emit(("dl_row", idx, "Checking existing files..."))
                continue
            if not t["primed"]:
                self._sync(idx, have)  # what was already on disk isn't "received"
            elif have != self._counted.get(idx, 0):
                self._add(idx, have - self._counted.get(idx, 0), received=True)
            prev = t["prev"].get(idx)
            if prev and now - prev[0] >= 0.4:
                inst = max(0.0, (have - prev[1]) / (now - prev[0]))
                t["speed"][idx] = inst if not t["speed"].get(idx) else t["speed"][idx] * 0.6 + inst * 0.4
                self._emit(("dl_speed", idx, f"{fmt_size(int(t['speed'][idx]))}/s"))
                t["prev"][idx] = (now, have)
            elif not prev:
                t["prev"][idx] = (now, have)
            if have >= size:
                t["done"].add(idx)
                with self._lock:
                    self.files_finished += 1
                    if not t["primed"]:
                        self.skipped += 1
                    else:
                        self.done += 1
                self._settle(idx, "skipped" if not t["primed"] else "done")
                self._emit(("dl_speed", idx, ""))
                if not t["primed"]:
                    self._emit(("dl_row", idx, "Already downloaded"))
                else:
                    self._emit(("dl_row", idx, "Done"))
                    self._emit(("log", f"Downloaded {rf.name} ({fmt_size(size)})"))
            elif have == 0 and status.num_peers == 0:
                self._emit(("dl_row", idx, "Looking for peers..."))
            else:
                self._emit(("dl_row", idx, self._pct(have, size)))
        if not checking:
            t["primed"] = True
        t["peers"] = status.num_peers

    def run(self) -> None:
        summary, started = None, time.monotonic()
        try:
            if self._swarm(self.jobs):
                summary = self._summary(started)
                self._emit(("log", f"--- Torrent download complete: {self.done} downloaded ({fmt_size(self.bytes_received)}), "
                                   f"{self.skipped} already present, {len(self.failures)} failed ---"))
        except Exception as exc:
            self._emit(("log", f"Fatal torrent error: {exc!r}"))
            self._emit(("notice", f"Torrent error: {exc}"))
        finally:
            self._emit(("dl_done", summary))

    def _swarm(self, jobs: list[tuple[int, RemoteFile]]) -> bool:
        """Download these torrent file entries through libtorrent. False if it was stopped part-way."""
        tracks: list[dict] = []
        try:
            try:
                ENGINE.ensure(self.settings)
            except LinkError as exc:
                for idx, rf in jobs:
                    self._fail(idx, rf, str(exc))
                self._emit(("log", f"Torrent download not started: {exc}"))
                self._emit(("notice", f"Torrent download not started: {exc}"))
                return True
            lt, ses = ENGINE.lt, ENGINE.ses
            for idx, rf in [j for j in jobs if j[0] in self._cancelled]:  # stopped while TorBox was being asked
                self._finish_cancel(idx, rf)
            jobs = [j for j in jobs if j[0] not in self._cancelled]
            self._emit(("log", f"\n--- Torrent download started: {len(jobs)} file(s) -> {self.dest} "
                               f"| network: {ENGINE.label} ---"))
            groups: dict[str, list] = {}
            for idx, rf in jobs:
                try:
                    _scheme, ih, fi = rf.url.split(":")
                    groups.setdefault(ih, []).append((idx, rf, int(fi)))
                except ValueError:
                    self._fail(idx, rf, "Not a torrent file entry")
            for ih, items in groups.items():
                try:
                    info = lt.torrent_info(lt.bdecode((TORRENT_DIR / f"{ih}.torrent").read_bytes()))
                    priorities = [0] * info.num_files()
                    for _idx, _rf, fi in items:
                        if 0 <= fi < len(priorities):
                            priorities[fi] = 4
                    params = lt.add_torrent_params()
                    params.ti, params.save_path, params.file_priorities = info, str(self.dest), priorities
                    handle = ses.add_torrent(params)
                except (OSError, RuntimeError, ValueError) as exc:
                    for idx, rf, _fi in items:
                        self._fail(idx, rf, f"Couldn't start this torrent ({exc}) - fetch the list again")
                    continue
                tracks.append({"h": handle, "items": items, "done": set(), "prev": {}, "speed": {}, "primed": False,
                               "peers": 0, "name": info.name()})
            user_paused, last_log = False, 0.0
            while not self._stop.is_set() and any(len(t["done"]) < len(t["items"]) for t in tracks):
                self._apply_cancels(tracks)
                if not any(len(t["done"]) < len(t["items"]) for t in tracks):
                    break
                if not self.pause_event.is_set():
                    if not user_paused:
                        user_paused = True
                        for t in tracks:
                            t["h"].pause()
                            for idx, _rf, _fi in t["items"]:
                                if idx not in t["done"]:
                                    self._emit(("dl_speed", idx, ""))
                                    self._emit(("dl_row", idx, "Paused"))
                    self._stop.wait(0.3)
                    continue
                if user_paused:
                    user_paused = False
                    for t in tracks:
                        t["h"].resume()
                if ENGINE.bound_ip and not ip_is_local(ENGINE.bound_ip):
                    self._vpn_lost(ses, tracks)
                    continue
                now = time.monotonic()
                for t in tracks:
                    if len(t["done"]) < len(t["items"]):
                        self._poll(t, now)
                        if len(t["done"]) == len(t["items"]):  # finished: leave the swarm, never seed
                            with contextlib.suppress(Exception):
                                ses.remove_torrent(t["h"])
                            t["h"] = None
                if now - last_log > 30:
                    last_log = now
                    for t in tracks:
                        if t["h"] is not None:
                            self._emit(("log", f"{t['name']}: {t['peers']} peer(s) connected"))
                self._stop.wait(0.5)
            return not self._stop.is_set()
        finally:
            for t in tracks:
                if t["h"] is not None:
                    with contextlib.suppress(Exception):
                        ENGINE.ses.remove_torrent(t["h"])


class TorBoxRunner(TorrentRunner):
    """Torrent downloads that look on TorBox first. Cached files are fetched over HTTPS with the web downloader's
    machinery (parallel, resumable, pausable); everything else goes to the swarm, VPN rules and all. Both run at the
    same time and share one progress bar and one summary."""

    def __init__(self, jobs, dest: Path, threads: int, emit, pause_event: threading.Event, settings: dict):
        super().__init__(jobs, dest, emit, pause_event, settings)
        self.threads = min(DL_MAX_PARALLEL, max(1, threads))
        self.from_cache = 0

    def set_threads(self, n: int) -> None:  # applies to the cached (HTTPS) files
        DownloadRunner.set_threads(self, n)

    def _cache_phase(self) -> tuple[list[tuple[int, RemoteFile]], list[tuple[int, RemoteFile]]]:
        """-> (jobs that still need the swarm, jobs that TorBox can serve over HTTPS)."""
        groups: dict[str, list[tuple[int, RemoteFile]]] = {}
        for idx, rf in self.jobs:
            try:
                _scheme, ih, _fi = rf.url.split(":")
                groups.setdefault(ih.lower(), []).append((idx, rf))
            except ValueError:
                self._fail(idx, rf, "Not a torrent file entry")
        swarm: list[tuple[int, RemoteFile]] = []
        direct: list[tuple[int, RemoteFile]] = []
        self._emit(("log", "Checking TorBox for cached copies..."))
        for ih, items in groups.items():
            for idx, _rf in items:
                self._emit(("dl_row", idx, "Checking TorBox cache..."))
        try:
            cached = torbox_cached(list(groups))
        except LinkError as exc:
            self._emit(("log", f"TorBox check failed ({exc}) - using the swarm instead."))
            for idx, _rf in self.jobs:
                self._emit(("dl_source", idx, "Torrent"))
            return [job for items in groups.values() for job in items], []
        for ih, items in groups.items():
            if self._stop.is_set():
                break
            label = items[0][1].group or ih
            if ih not in cached:
                self._emit(("log", f"Not cached on TorBox: {label} - using the swarm."))
                swarm += items
                for idx, _rf in items:
                    self._emit(("dl_source", idx, "Torrent"))
                continue
            try:
                tid = torbox_add(ih, items[0][1].group)
                tb_files: list[dict] = []
                deadline = time.monotonic() + 45
                while not self._stop.is_set():
                    tb_files = torbox_files(tid)
                    if tb_files or time.monotonic() > deadline:
                        break
                    self._stop.wait(1.5)
                if not tb_files:
                    raise LinkError("TorBox didn't list the files in time")
            except LinkError as exc:
                self._emit(("log", f"TorBox couldn't serve {label} ({exc}) - using the swarm."))
                swarm += items
                for idx, _rf in items:
                    self._emit(("dl_source", idx, "Torrent"))
                continue
            served = 0
            for idx, rf in items:
                match = _torbox_match(rf, tb_files)
                if match is None:
                    swarm.append((idx, rf))
                    self._emit(("dl_source", idx, "Torrent"))
                    continue
                self._emit(("dl_source", idx, "TorBox"))
                direct.append((idx, RemoteFile(rf.name, f"torbox:{tid}:{match['id']}", rf.size, rf.group)))
                served += 1
            self.from_cache += served
            self._emit(("log", f"Cached on TorBox: {label} - {served} of {len(items)} file(s) will download from it."))
        for idx, _rf in swarm:
            self._emit(("dl_row", idx, "Waiting for the swarm..."))
        return swarm, direct

    def run(self) -> None:
        summary, started = None, time.monotonic()
        try:
            swarm, direct = self._cache_phase()
            if direct:  # a file stopped meanwhile goes through its worker and winds down there
                with self._gate:
                    self._queue = deque(direct)
                    self._spawn(DL_MAX_PARALLEL)  # the limit, not the thread count, sets the parallelism
            if swarm and not self._stop.is_set():
                self._swarm(swarm)
            self._join_workers()
            if self._stop.is_set():
                return
            summary = self._summary(started)
            lost = f", {self.stopped} stopped" if self.stopped else ""
            self._emit(("log", f"--- Download complete: {self.done} downloaded ({fmt_size(self.bytes_received)}), "
                               f"{self.from_cache} file(s) served from TorBox's cache, {self.skipped} already present, "
                               f"{len(self.failures)} failed{lost} ---"))
        except Exception as exc:
            self._emit(("log", f"Fatal download error: {exc!r}"))
            self._emit(("notice", f"Download error: {exc}"))
        finally:
            self._emit(("dl_done", summary))
