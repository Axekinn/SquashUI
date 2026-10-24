"""Looser matching in the Organize tools: disc labels, similar names, other game files, and contents (checksums)."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from squashui import fingerprint
from squashui.organize import (
    find_duplicates, find_multidisc, is_standard_tag, MATCH_MODES, parse_disc, scan_multidisc, similar_titles, title_core,
)

NEVER = threading.Event()


def chd_header(raw_sha1: bytes, version: int = 5, compressors: bytes = b"\0" * 16) -> bytes:
    head = bytearray(124)
    head[:8] = b"MComprHD"
    head[8:12] = (124).to_bytes(4, "big")
    head[12:16] = version.to_bytes(4, "big")
    head[16:32] = compressors
    head[64:84] = raw_sha1
    head[84:104] = hashlib.sha1(b"with metadata" + raw_sha1).digest()
    return bytes(head)


class Tree(unittest.TestCase):
    _clock = 0

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="squashui_org_")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def make(self, rel: str, data: bytes = b"x" * 2048) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        Tree._clock += 10  # each file is newer than the last, even where the filesystem clock is coarse (Windows)
        os.utime(p, (1_700_000_000 + Tree._clock, 1_700_000_000 + Tree._clock))
        return p

    def dupes(self, **opts):
        return find_duplicates(self.root, True, opts, NEVER)

    def discs(self, loose=False):
        return scan_multidisc(self.root, True, NEVER, loose)


class DiscLabelTests(unittest.TestCase):
    def test_a_label_after_the_marker_is_not_part_of_the_name(self):
        self.assertEqual(parse_disc("Resident Evil 2 (Disc 1)(Leon)"), ("Resident Evil 2", 1, None))
        self.assertEqual(parse_disc("Resident Evil 2 (Disc 2) (Claire)"), ("Resident Evil 2", 2, None))
        self.assertEqual(parse_disc("Resident Evil 2 (Disc 1 - Leon)"), ("Resident Evil 2", 1, None))
        self.assertEqual(parse_disc("Resident Evil 2 (USA) (Disc 1) (Leon)"), ("Resident Evil 2 (USA)", 1, None))

    def test_region_and_revision_tags_are_kept(self):  # a USA disc 1 must never pair with a Europe disc 2
        self.assertEqual(parse_disc("Game (Disc 1) (USA)"), ("Game (USA)", 1, None))
        self.assertEqual(parse_disc("Game (Disc 2) (Rev 1)"), ("Game (Rev 1)", 2, None))
        self.assertEqual(parse_disc("Game (Disc 1) (Greatest Hits) (Europe)"), ("Game (Europe)", 1, None))
        self.assertEqual(parse_disc("Game [CD2] [!]"), ("Game [!]", 2, None))  # square brackets are never labels
        self.assertEqual(parse_disc("Game (Disc 1) (En,Fr,De)"), ("Game (En,Fr,De)", 1, None))

    def test_plain_names_parse_as_before(self):
        self.assertEqual(parse_disc("Game (USA) (Disc 2 of 3)"), ("Game (USA)", 2, 3))
        self.assertEqual(parse_disc("Game Disc 1"), ("Game", 1, None))
        self.assertIsNone(parse_disc("Game (USA)"))

    def test_standard_tags(self):
        for tag in ("USA", "Europe", "USA, Europe", "En,Fr,De", "Rev 1", "v1.1", "Beta 2", "Proto"):
            self.assertTrue(is_standard_tag(tag), tag)
        for tag in ("Leon", "Greatest Hits", "Special Edition", "", "Disc Image"):
            self.assertFalse(is_standard_tag(tag), tag)

    def test_title_core_ignores_tags_case_and_articles(self):
        self.assertEqual(title_core("Legend of Zelda, The (USA) [!]")[0], "the legend of zelda".removeprefix("the "))
        self.assertEqual(title_core("The Legend of Zelda")[0], title_core("Legend of Zelda, The (Europe)")[0])
        self.assertEqual(title_core("Game (USA) (Disc 2)"), ("game", 2))

    def test_similar_titles_never_confuses_a_sequel_with_the_game(self):
        self.assertTrue(similar_titles("resident evil 2 leon", "resident evil 2 claire"))
        self.assertTrue(similar_titles("resident evil 2", "resident evil 2 dual shock"))
        self.assertFalse(similar_titles("final fantasy vii", "final fantasy viii"))  # 0.97 similar, still not the same game
        self.assertFalse(similar_titles("tales of destiny", "tales of destiny 2"))
        self.assertFalse(similar_titles("chrono cross", "chrono trigger"))
        self.assertTrue(similar_titles("big story hero", "big story villain"))
        self.assertFalse(similar_titles("star ocean second story", "tales of eternia"))


class MultiDiscTests(Tree):
    def test_resident_evil_2_with_labelled_discs_is_one_game(self):
        self.make("Resident Evil 2 (Disc 1)(Leon).chd")
        self.make("Resident Evil 2 (Disc 2)(Claire).chd")
        (found,) = self.discs()
        self.assertEqual((found.name, sorted(found.discs), found.problem, found.loose), ("Resident Evil 2", [1, 2], "", False))
        self.assertTrue(find_multidisc(self.root, True, {}, NEVER)[0][0].checked)

    def test_strict_mode_leaves_differently_named_discs_alone(self):
        self.make("Resident Evil 2 - Leon (Disc 1).chd")
        self.make("Resident Evil 2 - Claire (Disc 2).chd")
        self.assertEqual(self.discs(), [])

    def test_loose_mode_pairs_them_but_leaves_them_unticked(self):
        self.make("Resident Evil 2 - Leon (Disc 1).chd")
        self.make("Resident Evil 2 - Claire (Disc 2).chd")
        (found,) = self.discs(loose=True)
        self.assertTrue(found.loose)
        self.assertEqual((found.name, sorted(found.discs), found.problem), ("Resident Evil 2", [1, 2], ""))
        findings, message = find_multidisc(self.root, True, {"loose": True}, NEVER)
        self.assertFalse(findings[0].checked)
        self.assertIn("similar names", findings[0].note)
        self.assertIn("unticked", message)

    def test_loose_mode_does_not_pair_sequels_or_other_regions(self):
        self.make("Final Fantasy VII (Disc 1).chd")
        self.make("Final Fantasy VIII (Disc 2).chd")
        self.make("Tales of Destiny (Disc 1).chd")
        self.make("Tales of Destiny 2 (Disc 2).chd")
        self.make("Some Game (USA) (Disc 1).chd")
        self.make("Some Game (Europe) (Disc 2).chd")
        self.assertEqual(self.discs(loose=True), [])

    def test_loose_mode_leaves_complete_sets_and_other_folders_alone(self):
        self.make("Game A (Disc 1).chd")
        self.make("Game A (Disc 2).chd")
        self.make("sub/Game A copy (Disc 3).chd")  # a different folder: never joined
        sets = self.discs(loose=True)
        self.assertEqual([(d.name, sorted(d.discs), d.loose) for d in sets if d.parent == self.root], [("Game A", [1, 2], False)])

    def test_three_discs_three_labels(self):
        for n, label in enumerate(("Hero", "Villain", "Epilogue"), 1):
            self.make(f"Big Story - {label} (Disc {n}).chd")
        (found,) = self.discs(loose=True)
        self.assertEqual(sorted(found.discs), [1, 2, 3])


class DuplicateTests(Tree):
    def test_other_game_files_are_invisible_unless_asked_for(self):
        self.make("Mother 3.exe")
        self.make("Mother 3.gba")
        self.assertEqual(self.dupes()[0], [])
        (f,), message = self.dupes(other_files=True)
        self.assertFalse(f.checked)  # same name is not proof: the person decides
        self.assertEqual(f.count, "2")
        self.assertIn("check", f.note)
        self.assertIn("ROMs", message)

    def test_same_name_in_different_folders_across_types(self):
        self.make("brother/Mother 3.gba")
        self.make("me/Mother 3.gba")
        (f,), _ = self.dupes(other_files=True)
        self.assertFalse(f.checked)

    def test_default_mode_still_ticks_plain_disc_duplicates(self):
        self.make("a/Game.iso")
        self.make("b/Game.iso")
        (f,), _ = self.dupes()
        self.assertTrue(f.checked)
        self.assertEqual(self.dupes()[0][0].count, "2")

    def test_ignoring_tags_finds_regional_copies_but_not_other_discs(self):
        self.make("a/Game (USA).chd")
        self.make("b/Game (Europe) (Rev 1).chd")
        self.make("a/Other (USA) (Disc 1).chd")
        self.make("b/Other (USA) (Disc 2).chd")
        findings, _ = self.dupes(match=MATCH_MODES[1])
        self.assertEqual([f.title for f in findings], ["Game (Europe) (Rev 1)"])  # the two discs of "Other" aren't copies
        self.assertFalse(findings[0].checked)  # a different release may be wanted: left to the person

    def test_same_contents_under_any_name(self):
        data = os.urandom(200_000)
        self.make("a/Zelda.iso", data)
        self.make("b/totally different name.iso", data)
        self.make("b/decoy.iso", os.urandom(200_000))  # same size, different bytes
        (f,), message = self.dupes(match=MATCH_MODES[2])
        self.assertTrue(f.checked)
        self.assertEqual(f.count, "2")
        self.assertIn("identical contents", f.note)
        self.assertEqual(len([op for op in f.ops if op[0] == "move"]), 1)
        self.assertIn("contents", message)

    def test_same_contents_in_one_folder_is_found_too(self):
        data = os.urandom(100_000)
        self.make("Game.iso", data)
        self.make("Game copy.iso", data)
        self.assertEqual(len(self.dupes(match=MATCH_MODES[2])[0]), 1)

    def test_files_that_differ_only_in_the_middle_are_not_duplicates(self):
        a = bytearray(os.urandom(3 << 20))
        b = bytearray(a)
        b[len(b) // 2] ^= 0xFF  # identical size, start and end: only a full checksum can tell
        self.make("a/one.iso", bytes(a))
        self.make("b/two.iso", bytes(b))
        self.assertEqual(self.dupes(match=MATCH_MODES[2])[0], [])
        self.make("c/three.iso", bytes(a))
        (f,), _ = self.dupes(match=MATCH_MODES[2])
        self.assertEqual(sorted(k[0].split(os.sep)[0] for k in f.children), ["a", "c"])

    def test_cue_sheets_are_skipped_in_contents_mode(self):
        self.make("a/Game.cue", b"FILE \"Game.bin\" BINARY\n")
        self.make("b/Game.cue", b"FILE \"Game.bin\" BINARY\n")
        findings, message = self.dupes(match=MATCH_MODES[2])
        self.assertEqual(findings, [])
        self.assertIn("2 .cue / .gdi sheet(s) skipped", message)

    def test_chd_files_of_one_disc_match_whatever_the_compression(self):
        raw = hashlib.sha1(b"the disc").digest()
        self.make("a/Game.chd", chd_header(raw, compressors=b"cdlz" + b"\0" * 12) + b"A" * 10_000)
        self.make("b/Renamed.chd", chd_header(raw, compressors=b"zstd" + b"\0" * 12) + b"B" * 70_000)  # other size, too
        self.make("c/Other.chd", chd_header(hashlib.sha1(b"another disc").digest()) + b"A" * 10_000)
        (f,), _ = self.dupes(match=MATCH_MODES[2])
        self.assertEqual(f.count, "2")
        self.assertFalse(any("Other" in k[0] for k in f.children))

    def test_old_chd_versions_fall_back_to_ordinary_checks(self):
        raw = hashlib.sha1(b"disc").digest()
        self.assertIsNone(fingerprint.chd_raw_sha1(self.make("v4.chd", chd_header(raw, version=4))))
        self.assertIsNone(fingerprint.chd_raw_sha1(self.make("short.chd", b"MComprHD")))
        self.assertIsNone(fingerprint.chd_raw_sha1(self.make("zero.chd", chd_header(b"\0" * 20))))
        self.assertEqual(fingerprint.chd_raw_sha1(self.make("ok.chd", chd_header(raw))), raw.hex())

    def test_a_scan_can_be_stopped(self):
        data = os.urandom(3 << 20)
        self.make("a/x.iso", data)
        self.make("b/y.iso", data)
        stop = threading.Event()
        stop.set()
        self.assertEqual(find_duplicates(self.root, True, {"match": MATCH_MODES[2]}, stop)[0], [])

    def test_progress_is_reported_while_checksumming(self):
        data = os.urandom(300_000)
        self.make("a/x.iso", data)
        self.make("b/y.iso", data)
        notes: list[str] = []
        self.dupes(match=MATCH_MODES[2], _note=notes.append)
        self.assertTrue(any(n.startswith("Checksumming 1 of 2") for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
