from __future__ import annotations

import threading
import unittest
from pathlib import Path

from squashui.formats import (PS3_SECTOR, XDVDFS_MAGIC, XDVDFS_OFFSETS, PkgInfo, associated_files, check_pkg_folder,
                              compare_trees, extract_iso, is_gamecube_wii_image, is_ps3_image, is_ps4_game_folder,
                              is_ps4_pkg, is_ps5_pkg, is_psp_image, is_xbox_image, iso_wants_dvd_mode,
                              locate_pkg_root, parse_sfo, pkg_target, read_pkg_info, read_ps3_sfo)
from tests.helpers import TempDirTest, build_iso, build_pkg, build_sfo

GAME = "UP0001-CUSA12345_00-ABCDEFGHIJKLMNOP"


class SfoTests(unittest.TestCase):
    def test_round_trip(self):
        sfo = parse_sfo(build_sfo({"TITLE": "Ünïcode Game", "CATEGORY": "gd", "PARENTAL_LEVEL": 5}))
        self.assertEqual(sfo, {"TITLE": "Ünïcode Game", "CATEGORY": "gd", "PARENTAL_LEVEL": 5})

    def test_garbage_is_not_an_sfo(self):
        self.assertIsNone(parse_sfo(b""))
        self.assertIsNone(parse_sfo(b"not an sfo at all, just text"))
        self.assertIsNone(parse_sfo(b"\0PSF" + b"\xff" * 16))  # absurd entry count


class PkgTests(TempDirTest):
    def test_reads_game_update_and_dlc(self):
        for category, kind in (("gd", "game"), ("gp", "update"), ("ac", "DLC")):
            with self.subTest(category=category):
                info = read_pkg_info(build_pkg(self.tmp / f"{category}.pkg", GAME, category, "Great Game", "01.05"))
                self.assertEqual((info.title_id, info.category, info.kind), ("CUSA12345", category, kind))
                self.assertEqual((info.title, info.version, info.content_id), ("Great Game", "01.05", GAME))
                self.assertEqual(info.label, "ABCDEFGHIJKLMNOP")

    def test_unreadable_packages(self):
        self.assertIsNone(read_pkg_info(self.write("short.pkg", b"\x7fCNT")))
        self.assertIsNone(read_pkg_info(self.write("wrong.pkg", b"MZ" + b"\0" * 400)))
        self.assertIsNone(read_pkg_info(self.tmp / "missing.pkg"))

    def test_magic_numbers(self):
        ps4 = build_pkg(self.tmp / "a.pkg", GAME, "gd")
        ps5 = self.write("b.pkg", b"\x7fFIH" + b"\0" * 64)
        self.assertTrue(is_ps4_pkg(ps4) and not is_ps5_pkg(ps4))
        self.assertTrue(is_ps5_pkg(ps5) and not is_ps4_pkg(ps5))
        self.assertFalse(is_ps4_pkg(self.tmp / "nope.pkg"))


class PkgTargetTests(unittest.TestCase):
    out = Path("/library")

    def info(self, category: str, content_id: str = GAME) -> PkgInfo:
        return PkgInfo(content_id, content_id[7:16], category, "T", "01.00")

    def test_shadps4_layout(self):
        self.assertEqual(pkg_target(self.info("gd"), "x", self.out, "shadps4"), self.out / "CUSA12345")
        self.assertEqual(pkg_target(self.info("gp"), "x", self.out, "shadps4"), self.out / "CUSA12345-UPDATE")
        self.assertEqual(pkg_target(self.info("ac"), "x", self.out, "shadps4"),
                         self.out / "addcont" / "CUSA12345" / "ABCDEFGHIJKLMNOP")

    def test_folder_layout_uses_the_file_name(self):
        self.assertEqual(pkg_target(self.info("gd"), "My: Game?", self.out, "folder"), self.out / "My Game")

    def test_unrecognised_packages_fall_back_to_a_folder(self):
        self.assertEqual(pkg_target(None, "thing", self.out, "shadps4"), self.out / "thing")
        weird = self.info("gd", "UP0001-../..45_00-ABCDEFGHIJKLMNOP")
        target = pkg_target(weird, "thing", self.out, "shadps4")
        self.assertEqual(target, self.out / "thing")  # a hostile title id never becomes a path

    def test_empty_name_still_gives_a_folder(self):
        self.assertEqual(pkg_target(None, "???", self.out, "folder"), self.out / "package")


class ExtractionCheckTests(TempDirTest):
    def test_locate_pkg_root(self):
        self.write("wrapped/CONTENT-ID/sce_sys/param.sfo")
        self.assertEqual(locate_pkg_root(self.tmp / "wrapped"), self.tmp / "wrapped" / "CONTENT-ID")
        self.write("direct/eboot.bin")
        self.assertEqual(locate_pkg_root(self.tmp / "direct"), self.tmp / "direct")
        self.write("two/a/x")
        self.write("two/b/y")
        self.assertEqual(locate_pkg_root(self.tmp / "two"), self.tmp / "two")

    def test_check_pkg_folder(self):
        base = PkgInfo(GAME, "CUSA12345", "gd", "T", "1")
        update = PkgInfo(GAME, "CUSA12345", "gp", "T", "1")
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIn("nothing", check_pkg_folder(empty, base))
        self.write("nobin/data.bin")
        self.assertIn("eboot.bin", check_pkg_folder(self.tmp / "nobin", base))
        self.assertIsNone(check_pkg_folder(self.tmp / "nobin", update))  # updates have no eboot of their own
        self.write("good/eboot.bin", b"elf")
        self.assertIsNone(check_pkg_folder(self.tmp / "good", base))
        self.write("ps5/other.txt")
        self.assertIn("app0", check_pkg_folder(self.tmp / "ps5", None, ps5=True))
        self.write("ps5ok/app0/game.bin")
        self.assertIsNone(check_pkg_folder(self.tmp / "ps5ok", None, ps5=True))

    def test_game_folder_detection(self):
        self.write("g/sce_sys/param.sfo")
        self.write("h/readme.txt")
        self.assertTrue(is_ps4_game_folder(self.tmp / "g"))
        self.assertFalse(is_ps4_game_folder(self.tmp / "h"))

    def test_compare_trees(self):
        self.write("a/one.bin", b"1111")
        self.write("a/sub/two.bin", b"22")
        self.write("b/ONE.bin", b"1111")  # ZArchive looks names up case-insensitively
        self.write("b/sub/two.bin", b"22")
        self.assertIsNone(compare_trees(self.tmp / "a", self.tmp / "b"))
        self.write("b/extra.bin", b"x")
        self.assertIn("not in the folder", compare_trees(self.tmp / "a", self.tmp / "b"))
        (self.tmp / "b" / "extra.bin").unlink()
        self.write("b/sub/two.bin", b"2222")
        self.assertIn("different size", compare_trees(self.tmp / "a", self.tmp / "b"))
        (self.tmp / "b" / "sub" / "two.bin").unlink()
        self.assertIn("missing", compare_trees(self.tmp / "a", self.tmp / "b"))


class DiscImageTests(TempDirTest):
    def test_gamecube_and_wii_magic(self):
        gc = bytearray(0x20)
        gc[0x1C:0x20] = b"\xc2\x33\x9f\x3d"
        wii = bytearray(0x20)
        wii[0x18:0x1C] = b"\x5d\x1c\x9e\xa3"
        self.assertTrue(is_gamecube_wii_image(self.write("gc.iso", bytes(gc))))
        self.assertTrue(is_gamecube_wii_image(self.write("wii.iso", bytes(wii))))
        self.assertFalse(is_gamecube_wii_image(self.write("other.iso", b"\0" * 0x20)))
        self.assertFalse(is_gamecube_wii_image(self.write("tiny.iso", b"\0" * 4)))

    def test_xbox_volume_descriptor_at_every_known_offset(self):
        for offset in XDVDFS_OFFSETS:
            with self.subTest(offset=hex(offset)):
                data = bytearray(offset + 33 * PS3_SECTOR)
                data[offset + 32 * PS3_SECTOR:offset + 32 * PS3_SECTOR + len(XDVDFS_MAGIC)] = XDVDFS_MAGIC
                self.assertTrue(is_xbox_image(self.write(f"x{offset}.iso", bytes(data))))
        self.assertFalse(is_xbox_image(self.write("plain.iso", b"\0" * 100_000)))

    def test_psp_disc(self):
        psp = build_iso(self.tmp / "psp.iso", {"PSP_GAME": {"PARAM.SFO": b"x"}, "UMD_DATA.BIN": b"y"})
        other = build_iso(self.tmp / "other.iso", {"README.TXT": b"hi"})
        self.assertTrue(is_psp_image(psp))
        self.assertFalse(is_psp_image(other))
        self.assertTrue(iso_wants_dvd_mode(psp))  # PSP discs go to createdvd
        self.assertFalse(iso_wants_dvd_mode(other))  # a tiny data disc is a CD

    def test_ps1_and_ps2_system_cnf(self):
        ps2 = build_iso(self.tmp / "ps2.iso", {"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_000.00;1\n"})
        ps1 = build_iso(self.tmp / "ps1.iso", {"SYSTEM.CNF": b"BOOT = cdrom:\\SLUS_000.00;1\n"})
        self.assertTrue(iso_wants_dvd_mode(ps2))
        self.assertFalse(iso_wants_dvd_mode(ps1))

    def test_ps3_disc_and_its_name(self):
        sfo = build_sfo({"TITLE": "Some PS3 Game", "TITLE_ID": "BLUS30000"})
        iso = build_iso(self.tmp / "ps3.iso", {"PS3_GAME": {"PARAM.SFO": sfo, "USRDIR": {"EBOOT.BIN": b"e" * 3000}}},
                        volume="PS3VOLUME")
        self.assertTrue(is_ps3_image(iso))
        self.assertEqual(read_ps3_sfo(iso)["TITLE"], "Some PS3 Game")
        self.assertFalse(is_ps3_image(build_iso(self.tmp / "no.iso", {"A.TXT": b"a"})))

    def test_extract_iso_round_trip(self):
        files = {"PS3_GAME": {"PARAM.SFO": build_sfo({"TITLE": "T"}), "USRDIR": {"EBOOT.BIN": bytes(range(256)) * 20}},
                 "PS3_DISC.SFB": b"sfb"}
        iso = build_iso(self.tmp / "disc.iso", files, volume="PS3VOLUME")
        dest = self.tmp / "out"
        progress: list[float] = []
        self.assertIsNone(extract_iso(iso, dest, threading.Event(), progress.append))
        self.assertEqual((dest / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").read_bytes(), bytes(range(256)) * 20)
        self.assertEqual((dest / "PS3_DISC.SFB").read_bytes(), b"sfb")
        self.assertTrue(progress and progress[-1] >= 0.99)

    def test_extract_iso_can_be_stopped(self):
        iso = build_iso(self.tmp / "d.iso", {"A.BIN": b"a" * 5000}, volume="PS3VOLUME")
        stop = threading.Event()
        stop.set()
        result = extract_iso(iso, self.tmp / "out", stop)
        self.assertTrue(result is None or isinstance(result, str))  # stops cleanly, never raises


class CueTests(TempDirTest):
    def test_associated_files_follow_the_cue(self):
        self.write("game.cue", 'FILE "game (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\nFILE "game (Track 2).bin" BINARY\n')
        t1, t2 = self.write("game (Track 1).bin"), self.write("game (Track 2).bin")
        self.write("unrelated.bin")
        self.assertEqual(associated_files(self.tmp / "game.cue"), {self.tmp / "game.cue", t1, t2})

    def test_a_cue_can_never_reach_outside_its_folder(self):
        outside = self.write("outside.bin")
        inner = self.tmp / "inner"
        inner.mkdir()
        (inner / "g.cue").write_text('FILE "../outside.bin" BINARY\n')
        self.assertEqual(associated_files(inner / "g.cue"), {inner / "g.cue"})
        self.assertTrue(outside.exists())

    def test_gdi_tracks(self):
        self.write("d.gdi", '2\n1 0 4 2352 "track01.bin" 0\n2 450 0 2352 track02.raw 0\n')
        a, b = self.write("track01.bin"), self.write("track02.raw")
        self.assertEqual(associated_files(self.tmp / "d.gdi"), {self.tmp / "d.gdi", a, b})


if __name__ == "__main__":
    unittest.main()
