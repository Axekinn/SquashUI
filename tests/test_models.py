from __future__ import annotations

import unittest
from pathlib import Path

from squashui.batch import plan_workers
from squashui.models import (BatchSummary, Config, Failure, exit_reason, fmt_duration, fmt_size, path_bytes,
                             remove_path, size_change, space_report)
from tests.helpers import TempDirTest


def cfg(**kw) -> Config:
    base = dict(source=Path("."), dest=None, chdman="chdman", extract=False, verify=False, delete=False, thread_budget=8)
    return Config(**{**base, **kw})


class FormattingTests(unittest.TestCase):
    def test_fmt_size(self):
        self.assertEqual(fmt_size(0), "0 B")
        self.assertEqual(fmt_size(1023), "1,023 B")
        self.assertEqual(fmt_size(1024), "1.00 KB")
        self.assertEqual(fmt_size(5 * 1024 ** 2), "5.00 MB")
        self.assertEqual(fmt_size(3 * 1024 ** 3 + 512 * 1024 ** 2), "3.50 GB")
        self.assertEqual(fmt_size(5000 * 1024 ** 3), "5,000.00 GB")  # no TB unit: stays in GB

    def test_fmt_duration(self):
        self.assertEqual(fmt_duration(0), "<1m")
        self.assertEqual(fmt_duration(59), "<1m")
        self.assertEqual(fmt_duration(60), "1m")
        self.assertEqual(fmt_duration(3600 + 5 * 60), "1h 05m")
        self.assertEqual(fmt_duration(26 * 3600), "1d 2h")
        self.assertEqual(fmt_duration(-30), "<1m")

    def test_exit_reason_uses_the_last_line(self):
        self.assertEqual(exit_reason("chdman", 1, "a\n\nfinal error  \n"), "chdman exit code 1 - final error")
        self.assertEqual(exit_reason("chdman", 3, ""), "chdman exit code 3")


class ConfigTests(unittest.TestCase):
    def test_mode_flags(self):
        self.assertTrue(cfg(mode="rvz").is_rvz)
        self.assertTrue(cfg(mode="cso").is_cso)
        self.assertTrue(cfg(mode="pkg").is_pkg)
        self.assertTrue(cfg(mode="zar").is_zar and cfg(mode="zar").packs_zar)
        self.assertFalse(cfg(mode="chd").packs_zar)

    def test_output_extensions(self):
        self.assertEqual(cfg(mode="chd").out_ext, ".chd")
        self.assertEqual(cfg(mode="rvz").out_ext, ".rvz")
        self.assertEqual(cfg(mode="cso").out_ext, ".cso")
        self.assertEqual(cfg(mode="zar").out_ext, ".zar")
        self.assertEqual(cfg(mode="pkg", pkg_output="zar").out_ext, ".zar")

    def test_pkg_tool_selection(self):
        self.assertEqual(cfg(mode="pkg", pkgext="X", pkg_tool="shadplus").pkg_exe, "X")
        self.assertEqual(cfg(mode="pkg").tool_name, "ShadPS4Plus PKG_Extractor")
        self.assertTrue(cfg(mode="pkg", pkg_tool="anyps5").is_ps5_pkg)
        self.assertFalse(cfg(mode="pkg", pkg_tool="orbis").is_ps5_pkg)

    def test_ps5_targets(self):
        self.assertTrue(cfg(mode="pkg", pkg_tool="anyps5", ps5_target="kyty").ps5_kyty)
        self.assertTrue(cfg(mode="pkg", pkg_tool="anyps5", ps5_target="anyps5").ps5_relink)

    def test_ps3_outputs(self):
        self.assertTrue(cfg(mode="ps3", ps3_output="iso").ps3_iso and not cfg(mode="ps3", ps3_output="iso").ps3_folder)
        both = cfg(mode="ps3", ps3_output="both")
        self.assertTrue(both.ps3_iso and both.ps3_folder)

    def test_config_is_immutable(self):
        with self.assertRaises(Exception):
            cfg().mode = "rvz"  # type: ignore[misc]


class SpaceReportTests(TempDirTest):
    def test_path_bytes_and_remove(self):
        self.write("d/a", b"12345")
        self.write("d/s/b", b"123")
        self.assertEqual(path_bytes(self.tmp / "d"), 8)
        self.assertEqual(path_bytes(self.tmp / "d" / "a"), 5)
        self.assertEqual(path_bytes(self.tmp / "missing"), 0)
        remove_path(self.tmp / "d")
        self.assertFalse((self.tmp / "d").exists())
        remove_path(self.tmp / "d")  # removing what is already gone is fine

    def test_size_change(self):
        out = self.write("o.chd", b"x" * 250)
        self.assertEqual(size_change(1000, out), "75% smaller (1,000 B -> 250 B)")
        self.assertEqual(size_change(100, out), "150% larger (100 B -> 250 B)")
        self.assertEqual(size_change(0, out), "")
        self.assertEqual(size_change(100, self.tmp / "gone"), "")

    def test_report_for_savings_and_growth(self):
        smaller = space_report(BatchSummary(2, (), "CHD", 2, 1000 * 1024, 400 * 1024, False))
        self.assertIn("Space saved", smaller)
        self.assertIn("60% smaller", smaller)
        larger = space_report(BatchSummary(1, (), "CSO", 1, 100, 150, False))
        self.assertIn("LARGER", larger)

    def test_report_is_empty_when_nothing_converted(self):
        self.assertEqual(space_report(BatchSummary(3, (Failure("convert", "x", "boom", Path("x")),), "CHD")), "")


class PlanWorkersTests(unittest.TestCase):
    def test_serial_modes_give_one_worker_every_core(self):
        self.assertEqual(plan_workers(16, 10, True, False), (1, 16))
        self.assertEqual(plan_workers(16, 10, False, True), (1, 16))

    def test_budget_is_shared(self):
        self.assertEqual(plan_workers(16, 10, False, False), (4, 4))
        self.assertEqual(plan_workers(16, 2, False, False), (2, 8))  # few inputs: each gets more cores
        self.assertEqual(plan_workers(3, 5, False, False), (1, 3))

    def test_never_zero(self):
        self.assertEqual(plan_workers(0, 0, False, False), (1, 1))

    def test_never_more_than_the_budget(self):
        for budget in range(1, 40):
            for sources in range(1, 12):
                workers, cores = plan_workers(budget, sources, False, False)
                self.assertGreaterEqual(workers, 1)
                self.assertLessEqual(workers * cores, max(budget, workers))


if __name__ == "__main__":
    unittest.main()
