from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest import mock

import squashui.organize as organize
from squashui.organize import (DUPE_POLICIES, apply_findings, find_duplicates, find_leftovers, find_multidisc,
                               find_tidy_renames, folder_name_for, parse_disc, scan_multidisc, tidy_name, undo_last)
from tests.helpers import TempDirTest

STOP = threading.Event()


class DiscNameTests(unittest.TestCase):
    def test_parse_disc(self):
        cases = {
            "Final Fantasy VII (USA) (Disc 2)": ("Final Fantasy VII (USA)", 2, None),
            "Game [Disc 1 of 3]": ("Game", 1, 3),
            "Game (CD3)": ("Game", 3, None),
            "Game Disk 4": ("Game", 4, None),
            "Game (Disc 2/4)": ("Game", 2, 4),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_disc(name), expected)

    def test_names_without_a_disc_marker(self):
        for name in ("Plain Game", "Discworld", "Disco Elysium", "Game (Disc 0)", "(Disc 1)"):
            with self.subTest(name=name):
                self.assertIsNone(parse_disc(name))

    def test_region_stays_part_of_the_name(self):
        usa, eur = parse_disc("Game (USA) (Disc 1)"), parse_disc("Game (Europe) (Disc 2)")
        self.assertNotEqual(usa[0], eur[0])  # a USA disc 1 is never paired with a Europe disc 2

    def test_folder_names_drop_illegal_characters(self):
        self.assertEqual(folder_name_for('Game: The "Sequel"?'), "Game The Sequel")
        self.assertEqual(folder_name_for("Name. "), "Name")


class TidyNameTests(unittest.TestCase):
    def test_spaces_and_underscores(self):
        self.assertEqual(tidy_name("My_Game__Name", {}), "My Game Name")

    def test_disc_markers_are_made_uniform(self):
        self.assertEqual(tidy_name("Game [disk 2 of 3]", {}), "Game (Disc 2 of 3)")
        self.assertEqual(tidy_name("Game (Disc 2)", {}), "Game (Disc 2)")

    def test_articles_move_to_the_front_only_when_asked(self):
        self.assertEqual(tidy_name("Legend of Zelda, The", {}), "Legend of Zelda, The")
        self.assertEqual(tidy_name("Legend of Zelda, The", {"articles": True}), "The Legend of Zelda")

    def test_tags_are_removed_but_disc_tags_stay(self):
        self.assertEqual(tidy_name("Game [!] [b1] (Disc 1)", {"tags": True}), "Game (Disc 1)")
        self.assertEqual(tidy_name("Game (USA) [!]", {"tags": False}), "Game (USA) [!]")  # off by default
        self.assertEqual(tidy_name("Game (USA) (Rev 1)", {"tags": True}), "Game (USA) (Rev 1)")  # only [..] dump tags

    def test_unchanged_name_is_returned_as_is(self):
        self.assertEqual(tidy_name("Clean Name", {}), "Clean Name")


class MultiDiscTests(TempDirTest):
    def test_groups_discs_into_a_folder(self):
        for n in (1, 2, 3):
            self.write(f"Game (USA) (Disc {n}).chd")
        self.write("Single (USA).chd")
        sets = scan_multidisc(self.tmp, False, STOP)
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0].name, "Game (USA)")
        self.assertEqual(sorted(sets[0].discs), [1, 2, 3])
        self.assertEqual(sets[0].target, self.tmp / "Game (USA)")
        self.assertEqual(sets[0].note, "Ready")

    def test_a_lone_disc_one_is_just_a_game(self):
        self.write("Solo (Disc 1).chd")
        self.assertEqual(scan_multidisc(self.tmp, False, STOP), [])

    def test_missing_disc_blocks_the_set(self):
        self.write("G (Disc 1 of 3).chd")
        self.write("G (Disc 3 of 3).chd")
        (dset,) = scan_multidisc(self.tmp, False, STOP)
        self.assertTrue(dset.problem)
        self.assertIn("2", dset.problem)

    def test_track_files_belong_to_their_cue(self):
        self.write("G (Disc 1).cue", 'FILE "G (Disc 1) (Track 1).bin" BINARY\nFILE "G (Disc 1) (Track 2).bin" BINARY\n')
        self.write("G (Disc 1) (Track 1).bin")
        self.write("G (Disc 1) (Track 2).bin")
        self.write("G (Disc 2).cue", 'FILE "G (Disc 2).bin" BINARY\n')
        self.write("G (Disc 2).bin")
        (dset,) = scan_multidisc(self.tmp, False, STOP)
        self.assertEqual(sorted(dset.discs), [1, 2])
        moved = {src.name for src, _dst in dset.moves}
        self.assertTrue({"G (Disc 1) (Track 1).bin", "G (Disc 2).bin"} <= moved)

    def test_apply_creates_the_playlist_and_undo_restores_everything(self):
        for n in (1, 2):
            self.write(f"Game (Disc {n}).chd", f"disc{n}")
        findings, _msg = find_multidisc(self.tmp, False, {}, STOP)
        self.assertEqual(len(findings), 1)
        undo = self.tmp / "undo.json"
        with mock.patch.object(organize, "undo_file", return_value=undo):
            ok, failures = apply_findings("multidisc", findings, lambda m: None, lambda a, b: None, STOP)
            self.assertEqual((ok, failures), (1, []))
            folder = self.tmp / "Game"
            self.assertTrue((folder / "Game (Disc 1).chd").is_file() and (folder / "Game (Disc 2).chd").is_file())
            playlist = (folder / "Game.m3u").read_text()
            self.assertIn("Game (Disc 1).chd", playlist)
            self.assertLess(playlist.index("Disc 1"), playlist.index("Disc 2"))
            restored, problems = undo_last("multidisc")
            self.assertEqual((restored, problems), (1, []))
        self.assertTrue((self.tmp / "Game (Disc 1).chd").is_file())
        self.assertEqual((self.tmp / "Game (Disc 2).chd").read_text(), "disc2")
        self.assertFalse((self.tmp / "Game" / "Game.m3u").exists())
        self.assertFalse(undo.exists())


class LeftoverTests(TempDirTest):
    def big(self, rel: str, magic: bytes = b"") -> Path:
        return self.write(rel, magic + b"\0" * 2000)

    def test_source_with_a_converted_twin(self):
        self.write("Game.cue", 'FILE "Game.bin" BINARY\n')
        self.write("Game.bin", b"b" * 3000)
        self.big("Game.chd", b"MComprHD")
        (finding,) = find_leftovers(self.tmp, False, {"permanent": True}, STOP)[0]
        self.assertEqual(finding.title, "Game.cue")
        self.assertEqual(finding.count, "2")
        self.assertIn("Converted copy", finding.note)

    def test_a_truncated_twin_is_not_trusted(self):
        self.write("Game.iso", b"i" * 3000)
        self.write("Game.chd", b"tiny")  # not a plausible CHD
        self.assertEqual(find_leftovers(self.tmp, False, {}, STOP)[0], [])

    def test_part_files_and_orphan_tracks(self):
        self.write("x.chd.part", b"half")
        self.write("Lone (Track 2).bin")
        titles = {f.title for f in find_leftovers(self.tmp, False, {}, STOP)[0]}
        self.assertEqual(titles, {"x.chd.part", "Lone (Track 2).bin"})

    def test_apply_moves_to_the_holding_folder_and_undo_brings_it_back(self):
        part = self.write("x.chd.part", b"half")
        findings = find_leftovers(self.tmp, False, {}, STOP)[0]  # not permanent: moved, not deleted
        with mock.patch.object(organize, "undo_file", return_value=self.tmp / "u.json"):
            apply_findings("leftovers", findings, lambda m: None, lambda a, b: None, STOP)
            self.assertFalse(part.exists())
            self.assertTrue((self.tmp / organize.HOLDING_DIR).is_dir())
            undo_last("leftovers")
        self.assertEqual(part.read_bytes(), b"half")

    def test_the_holding_folder_is_never_scanned(self):
        self.write(f"{organize.HOLDING_DIR}/old.chd.part")
        self.write(f"{organize.LEGACY_HOLDING_DIR}/older.chd.part")
        self.assertEqual(find_leftovers(self.tmp, True, {}, STOP)[0], [])


class DuplicateTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.write("a/Game.chd", b"c" * 100)
        self.write("b/Game.chd", b"d" * 400)
        self.write("b/Other.chd", b"e")

    def keeper(self, policy: str) -> str:
        (finding,) = find_duplicates(self.tmp, True, {"keep": policy}, STOP)[0]
        return next(name for name, _size, note in finding.children if note == "KEEP").replace("\\", "/")

    def test_policies(self):
        self.assertEqual(self.keeper("Keep the largest"), "b/Game.chd")
        self.assertEqual(self.keeper("Keep the smallest"), "a/Game.chd")

    def test_default_policy_prefers_compressed(self):
        self.write("c/Game.iso", b"i" * 9000)  # newer and bigger, but not compressed
        (finding,) = find_duplicates(self.tmp, True, {}, STOP)[0]
        keep = next(name for name, _s, note in finding.children if note == "KEEP")
        self.assertTrue(keep.endswith(".chd"))
        self.assertEqual(finding.count, "3")

    def test_same_folder_copies_are_not_duplicates_here(self):
        for name in ("only/X.chd", "only/X.iso"):
            self.write(name)
        self.assertEqual(find_duplicates(self.tmp / "only", True, {}, STOP)[0], [])

    def test_policies_listed(self):
        self.assertEqual(len(DUPE_POLICIES), 4)


class TidyRenameTests(TempDirTest):
    def test_rename_updates_the_playlist_line_and_undo_reverts_both(self):
        self.write("My_Game.chd")
        self.write("My_Game.m3u", "My_Game.chd\n")
        findings = find_tidy_renames(self.tmp, False, {}, STOP)[0]
        self.assertEqual([f.title for f in findings], ["My_Game.chd"])
        with mock.patch.object(organize, "undo_file", return_value=self.tmp / "u.json"):
            apply_findings("tidy", findings, lambda m: None, lambda a, b: None, STOP)
            self.assertTrue((self.tmp / "My Game.chd").is_file())
            self.assertEqual((self.tmp / "My_Game.m3u").read_text().strip(), "My Game.chd")  # its line follows the rename
            undo_last("tidy")
        self.assertTrue((self.tmp / "My_Game.chd").is_file())
        self.assertEqual((self.tmp / "My_Game.m3u").read_text().strip(), "My_Game.chd")

    def test_a_name_collision_blocks_the_row(self):
        self.write("A_B.chd")
        self.write("A B.chd")
        (finding,) = find_tidy_renames(self.tmp, False, {}, STOP)[0]
        self.assertIn("already exists", finding.problem)

    def test_stop_ends_a_scan_early(self):
        self.write("A_B.chd")
        stop = threading.Event()
        stop.set()
        self.assertEqual(find_tidy_renames(self.tmp, False, {}, stop)[0], [])


if __name__ == "__main__":
    unittest.main()
