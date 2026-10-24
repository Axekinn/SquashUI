"""Organize pages: scan a library for fixes, apply the ticked ones, undo the last run."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from ..constants import APP_NAME, APP_VERSION
from ..models import fmt_size
from ..organize import apply_findings, ORG_TOOLS, undo_file, undo_last
from .state import LABEL_W, OrgState
from .theme import T
from .widgets import Card, CheckTree, Expander, hint, PathRow


class OrganizeMixin:
    """Organize pages: scan a library for fixes, apply the ticked ones, undo the last run.

    Methods only; the state lives on SquashUIWindow, which mixes this in."""

    @staticmethod
    def _sync_group(widgets: list, getter: str, setter: str, signal: str) -> None:
        """Several pages show one shared setting: when one changes, the others follow."""
        def changed(value, source) -> None:
            for w in widgets:
                if w is not source and getattr(w, getter)() != value:
                    getattr(w, setter)(value)
        for w in widgets:
            getattr(w, signal).connect(lambda value, src=w: changed(value, src))

    def _build_org_page(self, tool: OrgTool) -> QWidget:
        st = OrgState(tool)
        self.org[tool.key] = st
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        folder = Expander("Games folder", expanded=True)
        source = PathRow("Source folder", label_width=LABEL_W)
        self._org_sources.append(source)
        if len(self._org_sources) > 1:
            source.setText(self._org_sources[0].text())
        folder.add(source)
        bar = QHBoxLayout()
        recursive = QCheckBox("Include subfolders")
        recursive.setChecked(True if not self._org_recursive else self._org_recursive[0].isChecked())
        self._org_recursive.append(recursive)
        st.scan_btn = QPushButton("Scan")
        st.scan_btn.clicked.connect(lambda _c=False, k=tool.key: self._org_scan(k))
        st.undo_btn = QPushButton("Undo last run")
        st.undo_btn.clicked.connect(lambda _c=False, k=tool.key: self._org_undo(k))
        bar.addWidget(recursive)
        bar.addSpacing(12)
        bar.addWidget(st.scan_btn)
        bar.addStretch(1)
        bar.addWidget(st.undo_btn)
        folder.add(bar)
        lay.addWidget(folder)

        if tool.options:
            card = Expander("Options", expanded=True)
            for name, kind, label, default, *extra in tool.options:
                if kind == "check":
                    box = QCheckBox(label)
                    box.setChecked(bool(default))
                    card.add(box)
                    st.opts[name] = box.isChecked
                else:
                    h = QHBoxLayout()
                    h.addWidget(QLabel(label))
                    cb = QComboBox()
                    cb.addItems(list(extra[0]))
                    cb.setCurrentText(default)
                    cb.setMinimumWidth(320)
                    h.addWidget(cb)
                    h.addStretch(1)
                    card.add(h)
                    st.opts[name] = cb.currentText
            lay.addWidget(card)

        results = Card("Scan results  (click a row to tick or untick it; the arrow shows its details)")
        tools_bar = QHBoxLayout()
        for text, fn in (("Select all", lambda _c=False, k=tool.key: self._org_select(k, True)),
                         ("Clear all", lambda _c=False, k=tool.key: self._org_select(k, False)),
                         ("Expand all", lambda _c=False, k=tool.key: self._org_expand(k, True)),
                         ("Collapse all", lambda _c=False, k=tool.key: self._org_expand(k, False))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            tools_bar.addWidget(b)
        tools_bar.addStretch(1)
        results.add(tools_bar)
        tree = st.tree = CheckTree()
        tree.setColumnCount(3)
        tree.setHeaderLabels([tool.headings[0], tool.headings[1], "Status"])
        tree.setRootIsDecorated(True)
        tree.setExpandsOnDoubleClick(False)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(False)
        tree.setMinimumHeight(180 if not tool.options else 140)
        hdr = tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        tree.setColumnWidth(1, 80)
        tree.setColumnWidth(2, 260)
        tree.itemClicked.connect(lambda item, _col, k=tool.key: self._org_toggle_item(k, item))
        tree.spacePressed.connect(lambda item, k=tool.key: self._org_toggle_item(k, item))
        results.add(tree)
        st.status = hint("Choose your games folder and press Scan.")
        results.add(st.status)
        lay.addWidget(results)

        if len(self._org_sources) == len(ORG_TOOLS):  # every Organize page exists now: link the shared settings
            self._sync_group([s.edit for s in self._org_sources], "text", "setText", "textChanged")
            self._sync_group(self._org_recursive, "isChecked", "setChecked", "toggled")
        return page

    def _org_set_busy(self, busy: bool) -> None:
        self.org_busy = busy
        self.stop_btn.setEnabled(busy)  # an Organize run can be stopped between items; each finished item stays undoable
        for st in self.org.values():
            st.scan_btn.setEnabled(not busy)
            st.undo_btn.setEnabled(not busy)
        if not self.is_processing and not self._pending_tools:
            self.start_btn.setEnabled(not busy)

    def _org_folder(self) -> str:
        return self._clean(self._org_sources[0].text())

    def _org_scan(self, key: str) -> None:
        if self.org_busy or self.is_processing:
            return
        raw = self._org_folder()
        if not raw or not Path(raw).is_dir():
            self._error("Error", f"Please select a valid games folder.\n\nPath checked: {raw}")
            return
        st = self.org[key]
        st.token += 1
        token, root, recursive, tool = st.token, Path(raw), self._org_recursive[0].isChecked(), st.tool
        opts = {name: getter() for name, getter in st.opts.items()}
        opts["_note"] = lambda text: self.ui_q.put(("org_note", key, token, text))  # progress for slow scans (checksums)
        self._org_set_busy(True)
        st.status.setText("Scanning...")
        self._org_stop.clear()

        def work() -> None:
            try:
                findings, message = tool.scan(root, recursive, opts, self._org_stop)
                error = ""
            except Exception as exc:
                findings, message, error = [], "", f"{type(exc).__name__}: {exc}"
            self.ui_q.put(("org_scan", key, token, findings, message, error))

        threading.Thread(target=work, daemon=True).start()

    def _org_show(self, key: str, findings: list[Finding], message: str, error: str) -> None:
        st = self.org[key]
        self._org_set_busy(False)
        st.findings, st.message = findings, message
        st.checked = {i for i, f in enumerate(findings) if not f.problem and f.checked}
        tree = st.tree
        tree.clear()
        row_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable  # ticking is done by the click handler
        for i, f in enumerate(findings):
            item = QTreeWidgetItem([f.title, str(f.count), f.status])
            item.setFlags(row_flags)
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            item.setToolTip(0, f.title)
            if f.problem:  # shown, but can't be ticked
                item.setForeground(2, T.bad)
            else:
                item.setCheckState(0, Qt.CheckState.Checked if i in st.checked else Qt.CheckState.Unchecked)
            tree.addTopLevelItem(item)
            for name, count, note in f.children:
                child = QTreeWidgetItem([name, str(count), note])
                child.setFlags(row_flags)
                item.addChild(child)
        self._set_badge(key, len(findings))
        if error:
            st.status.setText(f"Scan failed: {error}")
        elif not findings:
            st.status.setText(st.tool.empty + (f"  ({message})" if message else ""))
        else:
            self._org_update_status(key)

    def _org_update_status(self, key: str) -> None:
        st = self.org[key]
        ticked = [st.findings[i] for i in st.checked]
        text = f"{len(ticked)} of {len(st.findings)} ticked"
        if freed := sum(f.size for f in ticked):
            text += f"  |  frees {fmt_size(freed)}"
        if blocked := sum(1 for f in st.findings if f.problem):
            text += f"  |  {blocked} can't be applied (see Status)"
        if st.message:
            text += f"  |  {st.message}"
        st.status.setText(text + ".")

    def _org_toggle_item(self, key: str, item: QTreeWidgetItem) -> None:
        st = self.org[key]
        if self.org_busy or item.parent() is not None:  # detail rows under an item aren't ticked on their own
            return
        i = item.data(0, Qt.ItemDataRole.UserRole)
        if i is None or st.findings[i].problem:
            return
        st.checked ^= {i}
        item.setCheckState(0, Qt.CheckState.Checked if i in st.checked else Qt.CheckState.Unchecked)
        self._org_update_status(key)

    def _org_select(self, key: str, on: bool) -> None:
        st = self.org[key]
        if self.org_busy:
            return
        for i, f in enumerate(st.findings):
            if f.problem:
                continue
            (st.checked.add if on else st.checked.discard)(i)
            if (item := st.tree.topLevelItem(i)) is not None:
                item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self._org_update_status(key)

    def _org_expand(self, key: str, open_: bool) -> None:
        tree = self.org[key].tree
        for i in range(tree.topLevelItemCount()):
            tree.topLevelItem(i).setExpanded(open_)

    def _org_apply(self) -> None:
        if self.org_busy:
            return
        key = self.current_page
        st = self.org[key]
        tool = st.tool
        picks = [st.findings[i] for i in sorted(st.checked) if not st.findings[i].problem]
        if not picks:
            self._info("Nothing selected", "Scan a folder and tick at least one item first.")
            return
        if tool.confirm and not self._ask(tool.title, tool.confirm(picks, {n: g() for n, g in st.opts.items()})):
            return
        self._org_set_busy(True)
        st.status.setText(f"Working on {len(picks)} item(s)...")
        self._org_stop.clear()
        self._log_clear()
        pick_rows = [i for i in sorted(st.checked) if not st.findings[i].problem]  # tree row of each pick
        self.ui_q.put(("log", f"=== {APP_NAME} {APP_VERSION} ===\n--- {tool.title} started: {len(picks)} item(s) ---\n"))
        self._update_ui_state("Organizing", 0, len(picks))

        def work() -> None:
            ok, failures = 0, []
            try:
                ok, failures = apply_findings(
                    key, picks, lambda m: self.ui_q.put(("log", m)),
                    lambda cur, total: self.ui_q.put(("progress", "Organizing", cur, total)), self._org_stop,
                    lambda n, text: self.ui_q.put(("org_row", key, pick_rows[n], text)))
            except Exception as exc:
                failures = [("(run aborted)", f"{type(exc).__name__}: {exc}")]
            self.ui_q.put(("org_done", key, ok, failures))

        threading.Thread(target=work, daemon=True).start()

    def _org_finished(self, key: str, ok: int, failures: list[tuple[str, str]]) -> None:
        self._org_set_busy(False)
        stopped = self._org_stop.is_set()
        self.lbl_phase.setText("Phase: Stopped" if stopped else "Phase: Complete")
        self.ui_q.put(("log", f"\n--- {self.org[key].tool.title} {'stopped' if stopped else 'complete'}: "
                              f"{ok} done, {len(failures)} failed ---"))
        text = f"Done: {ok} item(s)." + ("\n\nStopped early: the remaining items were not touched." if stopped else "")
        if failures:
            text += "\n\nCouldn't be applied (put back where they were):\n" + "\n".join(
                f"  \u2022 {name}: {why}" for name, why in failures[:12])
        if ok:
            text += "\n\nYou can reverse this with 'Undo last run'."
        (self._warn if failures else self._info)(self.org[key].tool.title, text)
        self._org_scan(key)  # refresh the list: finished items drop off

    def _org_undo(self, key: str) -> None:
        if self.org_busy or self.is_processing:
            return
        tool = self.org[key].tool
        if not undo_file(key).is_file():
            self._info("Nothing to undo", f"There is no earlier '{tool.title}' run to undo.")
            return
        if not self._ask(f"Undo {tool.title}", "Reverse everything the last run changed (files moved back, "
                                               "playlists restored, folders it made removed)?"):
            return
        result = undo_last(key)
        if result is None:
            self._error("Undo failed", "The undo record couldn't be read.")
            return
        count, problems = result
        text = f"Restored {count} item(s)."
        if problems:
            text += "\n\nSome things couldn't be restored:\n" + "\n".join(f"  \u2022 {p}" for p in problems[:12])
        (self._warn if problems else self._info)(f"Undo {tool.title}", text)
        if self._org_folder():
            self._org_scan(key)
