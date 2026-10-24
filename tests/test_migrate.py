from __future__ import annotations

import unittest
from unittest import mock

import squashui.migrate as migrate
from tests.helpers import TempDirTest


class MigrationTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.new = {"session": self.tmp / "squashui_download_session.json", "logins": self.tmp / "squashui_logins.json",
                    "torrent": self.tmp / "squashui_torrent_settings.json", "dir": self.tmp / "squashui_torrents"}
        for target, value in (("APP_DIR", self.tmp), ("SESSION_FILE", self.new["session"]),
                              ("LOGIN_FILE", self.new["logins"]), ("TORRENT_FILE", self.new["torrent"]),
                              ("TORRENT_DIR", self.new["dir"])):
            patcher = mock.patch.object(migrate, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.tmp / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_old_files_move_to_their_new_names(self):
        self.write("qbsquash_download_session.json", "S")
        self.write("qbsquash_logins.json", "L")
        self.write("qbsquash_torrent_settings.json", "T")
        self.write("qbsquash_torrents/a.torrent", "A")
        self.write("qbsquash_undo_tidy.json", "U")
        self.write("local/qbSquash/tools/chdman/chdman.exe", "C")
        migrate.migrate_legacy_data()
        self.assertEqual(self.new["session"].read_text(), "S")
        self.assertEqual(self.new["logins"].read_text(), "L")
        self.assertEqual(self.new["torrent"].read_text(), "T")
        self.assertEqual((self.new["dir"] / "a.torrent").read_text(), "A")
        self.assertEqual((self.tmp / "squashui_undo_tidy.json").read_text(), "U")
        self.assertEqual((self.tmp / "local" / "SquashUI" / "tools" / "chdman" / "chdman.exe").read_text(), "C")
        self.assertFalse((self.tmp / "qbsquash_logins.json").exists())

    def test_nothing_new_is_ever_overwritten(self):
        self.write("qbsquash_logins.json", "old")
        self.write("squashui_logins.json", "new")
        migrate.migrate_legacy_data()
        self.assertEqual(self.new["logins"].read_text(), "new")
        self.assertEqual((self.tmp / "qbsquash_logins.json").read_text(), "old")

    def test_running_twice_and_with_nothing_to_do_is_harmless(self):
        migrate.migrate_legacy_data()
        self.write("qbsquash_logins.json", "L")
        migrate.migrate_legacy_data()
        migrate.migrate_legacy_data()
        self.assertEqual(self.new["logins"].read_text(), "L")


if __name__ == "__main__":
    unittest.main()
