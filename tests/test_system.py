from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest import mock

import squashui.system as system
from squashui.system import DiskGate, file_size, portable_path, resolve_dolphintool, resolve_tool_path
from tests.helpers import POSIX_ONLY, TempDirTest, make_tool


class PathTests(TempDirTest):
    def test_portable_paths_are_relative_to_the_app_folder(self):
        with mock.patch.object(system, "APP_DIR", self.tmp):
            self.assertEqual(portable_path(self.tmp / "tools" / "chdman" / "chdman.exe"), "./tools/chdman/chdman.exe")
            outside = Path(self._td.name).parent / "elsewhere.exe"
            self.assertEqual(portable_path(outside), str(outside))

    def test_relative_tool_paths_anchor_to_the_app_folder_not_the_cwd(self):
        exe = self.write("tools/x/tool.exe")
        with mock.patch.object(system, "APP_DIR", self.tmp):
            self.assertEqual(resolve_tool_path("./tools/x/tool.exe"), str(self.tmp / "tools/x/tool.exe"))
            self.assertEqual(resolve_tool_path("chdman"), "chdman")  # a bare name stays a PATH lookup
            self.assertEqual(resolve_tool_path(""), "")
            self.assertEqual(resolve_tool_path("./tools/missing.exe"), "./tools/missing.exe")
            self.assertEqual(resolve_tool_path(str(exe)), str(exe))

    def test_dolphintool_accepts_the_tool_its_neighbour_or_its_folder(self):
        tool = self.write("dolphin/DolphinTool.exe")
        self.write("dolphin/Dolphin.exe")
        with mock.patch.object(system, "APP_DIR", self.tmp):
            for given in (tool, self.tmp / "dolphin" / "Dolphin.exe", self.tmp / "dolphin"):
                with self.subTest(given=given):
                    self.assertEqual(resolve_dolphintool(str(given)), str(tool))
            self.assertEqual(resolve_dolphintool(""), "")
            self.assertEqual(resolve_dolphintool("./dolphin"), str(tool))

    def test_file_size(self):
        self.assertEqual(file_size(self.write("a.bin", b"12345")), 5)
        self.assertEqual(file_size(self.tmp / "nope"), 0)


class DiskGateTests(TempDirTest):
    def test_ssd_mode_never_serialises(self):
        self.assertIsNone(DiskGate("ssd").key(self.tmp))

    def test_hdd_mode_serialises_every_disk(self):
        gate = DiskGate("hdd")
        self.assertIsNotNone(gate.key(self.tmp))
        self.assertEqual(gate.key(self.tmp), gate.key(self.tmp / "sub" / "not" / "yet"))  # same disk, folder not created yet

    def test_exempt_disks_are_never_blocked(self):
        gate = DiskGate("hdd")
        gate.exempt.add(gate.key(self.tmp))
        self.assertIsNone(gate.key(self.tmp))

    def test_hold_gives_one_tool_at_a_time_per_disk(self):
        gate, stop = DiskGate("hdd"), threading.Event()
        inside, peak, lock = [0], [0], threading.Lock()

        def work():
            with gate.hold([self.tmp], stop):
                with lock:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                threading.Event().wait(0.02)
                with lock:
                    inside[0] -= 1

        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(peak[0], 1)

    def test_hold_on_two_disks_in_any_order_cannot_deadlock(self):
        gate, stop = DiskGate("hdd"), threading.Event()
        a, b = self.tmp, Path("/")
        done = []

        def work(paths):
            with gate.hold(paths, stop):
                done.append(1)

        threads = [threading.Thread(target=work, args=(p,)) for p in ([a, b], [b, a], [a, b], [b, a])]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(done), 4)

    def test_a_stopped_run_does_not_wait_for_the_disk(self):
        gate, stop = DiskGate("hdd"), threading.Event()
        released = threading.Event()
        holder_in = threading.Event()

        def holder():
            with gate.hold([self.tmp], threading.Event()):
                holder_in.set()
                released.wait(10)

        t = threading.Thread(target=holder)
        t.start()
        holder_in.wait(5)
        stop.set()
        result: list = []

        def waiter():
            try:
                with gate.hold([self.tmp], stop):
                    result.append("entered")
            except BaseException as exc:  # noqa: BLE001 - whichever way a stopped run bails out is fine
                result.append(type(exc).__name__)

        w = threading.Thread(target=waiter)
        w.start()
        w.join(5)
        released.set()
        t.join(5)
        self.assertFalse(w.is_alive(), "a stopped run must not block on a busy disk")


@POSIX_ONLY
class ProcessTests(TempDirTest):
    def test_tool_scripts_run(self):
        exe = make_tool(self.tmp / "t", "print('hi')")
        import subprocess

        self.assertEqual(subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip(), "hi")


if __name__ == "__main__":
    unittest.main()
