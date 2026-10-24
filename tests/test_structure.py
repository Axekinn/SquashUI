"""Guards on the shape of the package, so the split can't quietly rot."""
from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401  (puts the project on sys.path)

PKG = Path(__file__).resolve().parent.parent / "squashui"
CORE = ["constants", "bootstrap", "system", "formats", "models", "batch", "organize", "downloads", "links",
        "torrents", "torbox", "torrent_runners", "tools"]


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(PKG.parent).with_suffix("").parts)


def internal_imports(path: Path, runtime_only: bool = True) -> set[str]:
    """squashui modules this file imports at module level (TYPE_CHECKING and in-function imports excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    here = module_name(path).split(".")[:-1]
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level:
            base = here[: len(here) - (node.level - 1)] if node.level > 1 else here
            target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            found.add(target)
    return found


class StructureTests(unittest.TestCase):
    def test_every_core_module_imports_without_qt(self):
        for name in CORE:
            with self.subTest(module=name):
                importlib.import_module(f"squashui.{name}")

    def test_core_never_touches_the_ui_or_qt(self):
        for name in CORE:
            src = (PKG / f"{name}.py").read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [("." * node.level) + (node.module or "")]
                else:
                    continue
                for m in mods:
                    with self.subTest(module=name, imports=m):
                        self.assertFalse(m.startswith("PySide6"), f"{name} imports Qt")
                        self.assertFalse(m.startswith(".ui") or m.startswith("..ui"), f"{name} imports the UI")

    def test_no_import_cycles(self):
        graph = {module_name(p): {m for m in internal_imports(p) if m.startswith("squashui")}
                 for p in PKG.rglob("*.py") if p.name != "__init__.py"}
        state: dict[str, int] = {}

        def visit(node: str, trail: list[str]) -> None:
            if state.get(node) == 1:
                self.fail("import cycle: " + " -> ".join([*trail[trail.index(node):], node]))
            if state.get(node) == 2:
                return
            state[node] = 1
            for dep in sorted(graph.get(node, ())):
                if dep in graph:
                    visit(dep, [*trail, node])
            state[node] = 2

        for mod in sorted(graph):
            visit(mod, [])

    def test_no_module_is_a_monolith_again(self):
        for path in PKG.rglob("*.py"):
            lines = len(path.read_text(encoding="utf-8").splitlines())
            with self.subTest(module=module_name(path)):
                self.assertLess(lines, 1800, f"{path.name} has {lines} lines: split it")

    def test_a_packaged_build_keeps_its_files_beside_the_exe_and_never_runs_pip(self):
        import sys
        from unittest import mock

        import squashui.bootstrap as bootstrap
        import squashui.constants as constants

        fake_exe = Path(__file__).resolve().parent / "dist" / "SquashUI.exe"
        try:
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(fake_exe)):
                importlib.reload(constants)
                self.assertEqual(constants.APP_DIR, fake_exe.parent)
                with mock.patch.object(bootstrap.subprocess, "check_call") as pip:
                    bootstrap.ensure_dependencies()
                    pip.assert_not_called()
        finally:
            importlib.reload(constants)
        self.assertEqual(constants.APP_DIR, PKG.parent)  # a source checkout: the folder holding the package

    def test_version_and_name(self):
        from squashui.constants import APP_NAME, APP_VERSION

        self.assertEqual(APP_NAME, "SquashUI")
        self.assertRegex(APP_VERSION, r"^v\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
