from __future__ import annotations

import io
import json
import re
import unittest
import zipfile
from unittest import mock

import squashui.tools as tools
from squashui.tools import PkgExtractorManager, mame_version
from tests.helpers import TempDirTest

EXE = b"MZ" + b"\0" * 70_000


def release(tag: str, *assets: str, prerelease: bool = False) -> dict:
    return {"tag_name": tag, "prerelease": prerelease, "draft": False,
            "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{tag}/{n}", "size": 123,
                        "digest": "sha256:" + "ab" * 32} for n in assets]}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AssetChoiceTests(unittest.TestCase):
    pick = staticmethod(PkgExtractorManager._windows_asset)

    @staticmethod
    def assets(*names):
        return [{"name": n} for n in names]

    def test_bare_exe_beats_everything_else(self):
        got = self.pick(self.assets("pkg_extractor.AppImage", "install_pkg.sh", "install_pkg.bat", "pkg_extractor.exe"))
        self.assertEqual(got["name"], "pkg_extractor.exe")

    def test_windows_zip_is_preferred_to_the_linux_one(self):
        got = self.pick(self.assets("pkg_extractor_linux.zip", "pkg_extractor_windows.zip"))
        self.assertEqual(got["name"], "pkg_extractor_windows.zip")

    def test_nothing_usable(self):
        self.assertIsNone(self.pick(self.assets("install_pkg.bat", "pkg_extractor.AppImage", "source.tar.gz")))
        self.assertIsNone(self.pick([]))


class PkgExtractorManagerTests(TempDirTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(tools, "APP_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict("os.environ", {"LOCALAPPDATA": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events: list[tuple] = []
        self.mgr = PkgExtractorManager(self.events.append)

    def test_lives_under_the_app_folder(self):
        self.assertEqual(self.mgr.root, self.tmp / "tools" / "pkg-extractor")
        self.assertTrue(self.mgr.root.is_dir())

    def test_labels_and_tags(self):
        self.assertEqual(self.mgr._label("PKG_EXTRACTOR_1_1"), "v1.1")
        tag = re.compile(self.mgr.TAG_RE)
        self.assertTrue(tag.fullmatch("PKG_EXTRACTOR_1_0") and tag.fullmatch("PKG_EXTRACTOR_2_10_3"))
        self.assertFalse(tag.fullmatch("ShadPS4Plus_0_12_0"))

    def test_picks_the_newest_extractor_release_not_an_emulator_build(self):
        listing = [release("SHADPS4PLUS_0_13", "shadps4plus-win.zip"),
                   release("PKG_EXTRACTOR_1_2", "pkg_extractor.exe", prerelease=True),
                   release("PKG_EXTRACTOR_1_1", "pkg_extractor.AppImage", "pkg_extractor.exe", "install_pkg.bat"),
                   release("PKG_EXTRACTOR_1_0", "pkg_extractor.exe")]
        with mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(listing).encode())):
            rel = self.mgr._latest_release()
        self.assertEqual((rel.tag, rel.name), ("PKG_EXTRACTOR_1_1", "pkg_extractor.exe"))
        self.assertEqual(rel.sha256, "ab" * 32)
        self.assertTrue(rel.url.endswith("PKG_EXTRACTOR_1_1/pkg_extractor.exe"))

    def test_no_windows_package_is_a_clear_error(self):
        listing = [release("PKG_EXTRACTOR_1_1", "pkg_extractor.AppImage", "install_pkg.sh")]
        with mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(listing).encode())):
            with self.assertRaises(RuntimeError):
                self.mgr._latest_release()

    def test_unpacks_a_bare_exe(self):
        archive = self.write("dl/pkg_extractor.exe", EXE)
        tmp, dest = self.tmp / "tmp", self.mgr.root / "PKG_EXTRACTOR_1_1"
        tmp.mkdir()
        self.mgr._unpack(archive, tmp, dest)
        self.assertEqual((dest / "pkg_extractor.exe").read_bytes(), EXE)

    def test_unpacks_a_zip_keeping_companion_files(self):
        archive = self.tmp / "dl.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("bundle/Pkg_Extractor.exe", EXE)
            z.writestr("bundle/helper.dll", b"MZ")
            z.writestr("bundle/install_pkg.bat", "@echo off")
        tmp, dest = self.tmp / "tmp", self.mgr.root / "PKG_EXTRACTOR_1_1"
        tmp.mkdir()
        self.mgr._unpack(archive, tmp, dest)
        self.assertTrue((dest / "pkg_extractor.exe").is_file())  # the app always looks for this one name
        self.assertTrue((dest / "helper.dll").is_file())

    def test_a_zip_without_the_program_is_an_error(self):
        archive = self.tmp / "bad.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("readme.txt", "nothing here")
        (self.tmp / "tmp").mkdir()
        with self.assertRaises(RuntimeError):
            self.mgr._unpack(archive, self.tmp / "tmp", self.mgr.root / "x")

    def test_probe_rejects_things_that_are_not_programs(self):
        good = self.write("good.exe", EXE)
        text = self.write("bad.exe", b"hello" * 20_000)
        tiny = self.write("tiny.exe", b"MZ" + b"\0" * 10)
        self.assertEqual(self.mgr._probe(good), "ok")
        for bad in (text, tiny):
            with self.assertRaises(RuntimeError):
                self.mgr._probe(bad)

    def test_a_hand_placed_copy_is_found(self):
        self.assertIsNone(self.mgr._local_copy())
        exe = self.write("tools/pkg-extractor/pkg_extractor.exe", EXE)
        self.assertEqual(self.mgr._local_copy(), exe)

    def test_cached_copy_needs_a_matching_tag_and_a_file(self):
        self.assertIsNone(self.mgr.cached())
        (self.mgr.root / "current.txt").write_text("PKG_EXTRACTOR_1_1")
        self.assertIsNone(self.mgr.cached())  # named, but not on disk
        exe = self.write("tools/pkg-extractor/PKG_EXTRACTOR_1_1/pkg_extractor.exe", EXE)
        self.assertEqual(self.mgr.cached(), ("PKG_EXTRACTOR_1_1", exe))
        (self.mgr.root / "current.txt").write_text("../../evil")
        self.assertIsNone(self.mgr.cached())  # a tag that doesn't match the pattern is never followed


class MameTests(unittest.TestCase):
    def test_version_label(self):
        self.assertEqual(mame_version("mame0278"), "0.278")


if __name__ == "__main__":
    unittest.main()
