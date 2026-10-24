from __future__ import annotations

import hashlib
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import squashui.downloads as dl
from squashui.downloads import (DownloadRunner, LinkError, RemoteFile, describe_error, list_open_directory,
                                parse_archive_url, site_for)
from tests.helpers import POSIX_ONLY, TempDirTest, web_server

DATA = bytes(range(256)) * 400  # 102,400 bytes


def run_downloads(jobs, dest: Path, **kw):
    events: list[tuple] = []
    pause = threading.Event()
    pause.set()
    runner = DownloadRunner(jobs, dest, kw.pop("threads", 2), kw.pop("verify", True), kw.pop("per_item", False),
                            events.append, pause)
    runner.run()
    return runner, events


class UrlTests(unittest.TestCase):
    def test_archive_item_links(self):
        self.assertEqual(parse_archive_url("https://archive.org/details/some-item"), ("some-item", ""))
        self.assertEqual(parse_archive_url("archive.org/download/item/sub%20dir/file.zip"), ("item", "sub dir/file.zip"))
        self.assertIsNone(parse_archive_url("https://example.com/details/x"))

    def test_an_archive_link_without_an_item_is_explained(self):
        with self.assertRaises(LinkError):
            parse_archive_url("https://archive.org/")

    def test_site_matching_is_by_host_not_by_substring(self):
        with mock.patch.object(dl, "LOGINS", {}):
            self.assertEqual(site_for("https://archive.org/download/x/y"), "archive.org")
            self.assertEqual(site_for("https://ia800.us.archive.org/f"), "archive.org")
            self.assertEqual(site_for("https://1fichier.com/?abc"), "1fichier")
            self.assertEqual(site_for("https://evil-archive.org.example.com/"), "")
            self.assertEqual(site_for("https://notarchive.org/"), "")

    def test_describe_error(self):
        err = urllib.error.HTTPError("http://x/y", 404, "nf", {}, None)
        self.assertEqual(describe_error(err), "HTTP 404 - not found")
        self.assertIn("rate limited", describe_error(urllib.error.HTTPError("http://x/y", 429, "", {}, None)))
        self.assertEqual(describe_error(TimeoutError()), "Timed out")
        self.assertTrue(describe_error(urllib.error.URLError("dns")).startswith("Network error"))


class ListingTests(unittest.TestCase):
    PAGE = ('<html><body><a href="?C=N;O=D">Name</a><a href="../">Parent</a><a href="game%20one.chd">one</a>'
            '<a href="sub/">sub</a><a href="#top">top</a><a href="mailto:a@b.c">m</a><a href="two.cue">two</a></body></html>')

    def test_open_directory_lists_only_files_beside_it(self):
        with web_server(pages={"/roms/": self.PAGE}) as (base, _h):
            files = list_open_directory(base + "/roms/")
        self.assertEqual(sorted(f.name for f in files), ["game one.chd", "two.cue"])
        self.assertTrue(all(f.url.startswith(base + "/roms/") for f in files))

    def test_a_direct_file_link_is_a_single_preselected_file(self):
        (only,) = list_open_directory("http://example.invalid/files/game.chd")
        self.assertEqual((only.name, only.preselect), ("game.chd", True))

    def test_not_a_web_link(self):
        with self.assertRaises(LinkError):
            list_open_directory("ftp://example.com/x/")


class DownloadRunnerTests(TempDirTest):
    def rf(self, base: str, name: str = "file.bin", data: bytes = DATA, **kw) -> RemoteFile:
        return RemoteFile(name, f"{base}/{name}", len(data), md5=kw.pop("md5", hashlib.md5(data).hexdigest()), **kw)

    def test_downloads_and_verifies(self):
        with web_server({"/file.bin": DATA}) as (base, _h):
            runner, events = run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual((runner.done, runner.failures), (1, []))
        self.assertEqual((self.tmp / "dl" / "file.bin").read_bytes(), DATA)
        self.assertFalse(list((self.tmp / "dl").glob("*.part")))
        self.assertEqual(events[-1][0], "dl_done")
        self.assertEqual((events[-1][1].total, events[-1][1].done), (1, 1))

    def test_resumes_a_partial_file_with_a_range_request(self):
        part = self.tmp / "dl" / "file.bin.part"
        part.parent.mkdir()
        part.write_bytes(DATA[:30_000])
        with web_server({"/file.bin": DATA}) as (base, handler):
            runner, _events = run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual(runner.failures, [])
        self.assertEqual((self.tmp / "dl" / "file.bin").read_bytes(), DATA)
        self.assertIn(("/file.bin", "bytes=30000-"), handler.hits)
        self.assertLess(runner.bytes_received, len(DATA))  # only the missing part came over the wire

    def test_an_existing_complete_file_is_skipped(self):
        (self.tmp / "dl").mkdir()
        (self.tmp / "dl" / "file.bin").write_bytes(DATA)
        with web_server({"/file.bin": DATA}) as (base, handler):
            runner, _events = run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual((runner.skipped, runner.done), (1, 0))
        self.assertEqual(handler.hits, [])

    def test_a_wrong_checksum_is_rejected_and_never_left_behind(self):
        with web_server({"/file.bin": DATA}) as (base, _h), mock.patch.object(dl, "DL_ATTEMPTS", 1):
            runner, _events = run_downloads([(0, self.rf(base, md5="0" * 32))], self.tmp / "dl")
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("Checksum", runner.failures[0][2])
        self.assertFalse((self.tmp / "dl" / "file.bin").exists())
        self.assertFalse((self.tmp / "dl" / "file.bin.part").exists())

    def test_a_second_corrupt_copy_stops_the_retries(self):
        """Two copies with the wrong checksum mean the listing is wrong, not the network: give up, don't loop."""
        with web_server({"/file.bin": DATA}) as (base, handler):
            runner, _events = run_downloads([(0, self.rf(base, md5="0" * 32))], self.tmp / "dl")
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("Checksum", runner.failures[0][2])
        self.assertEqual(len(handler.hits), 2)
        self.assertFalse((self.tmp / "dl" / "file.bin").exists())

    def test_404_is_reported_without_retrying_forever(self):
        with web_server({}) as (base, handler):
            runner, _events = run_downloads([(0, self.rf(base, "missing.bin"))], self.tmp / "dl")
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("404", runner.failures[0][2])
        self.assertEqual(len(handler.hits), 1)

    def test_unsafe_names_never_leave_the_destination(self):
        with web_server({"/x": DATA}) as (base, handler):
            jobs = [(i, RemoteFile(name, f"{base}/x", len(DATA))) for i, name in
                    enumerate(["../escape.bin", "a/../../escape.bin", "/abs/../../x.bin"])]
            runner, _events = run_downloads(jobs, self.tmp / "dl", verify=False)
        self.assertEqual(len(runner.failures), 3)
        self.assertEqual(handler.hits, [])
        self.assertFalse((self.tmp / "escape.bin").exists())

    @POSIX_ONLY
    def test_a_symlink_inside_the_destination_cannot_be_used_to_escape(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.tmp / "dl").mkdir()
        (self.tmp / "dl" / "evil").symlink_to(outside, target_is_directory=True)
        with web_server({"/x": DATA}) as (_base, handler):
            jobs = [(0, RemoteFile("evil/pwned.bin", _base + "/x", len(DATA)))]
            runner, _events = run_downloads(jobs, self.tmp / "dl", verify=False)
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("Unsafe", runner.failures[0][2])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(handler.hits, [])

    def test_per_item_folders(self):
        with web_server({"/file.bin": DATA}) as (base, _h):
            runner, _events = run_downloads([(0, self.rf(base, group="Some: Item"))], self.tmp / "dl", per_item=True)
        self.assertEqual(runner.failures, [])
        self.assertTrue((self.tmp / "dl" / "Some_ Item" / "file.bin").is_file())

    def test_several_files_in_parallel(self):
        files = {f"/f{i}.bin": DATA[: 10_000 + i] for i in range(6)}
        with web_server(files) as (base, _h):
            jobs = [(i, self.rf(base, p[1:], d)) for i, (p, d) in enumerate(files.items())]
            runner, _events = run_downloads(jobs, self.tmp / "dl", threads=3)
        self.assertEqual((runner.done, runner.failures), (6, []))

    def test_stop_before_start_downloads_nothing_and_still_signals_done(self):
        with web_server({"/file.bin": DATA}) as (base, handler):
            events: list[tuple] = []
            pause = threading.Event()
            pause.set()
            runner = DownloadRunner([(0, self.rf(base))], self.tmp / "dl", 1, True, False, events.append, pause)
            runner.shutdown()
            runner.run()
        self.assertEqual(handler.hits, [])
        self.assertEqual(events[-1][0], "dl_done")


if __name__ == "__main__":
    unittest.main()
