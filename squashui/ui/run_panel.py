"""The run panel and a run's life cycle: start, pause, stop, progress, ETA, log and summary."""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPlainTextEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ..batch import BatchRunner
from ..constants import PKG_LAYOUTS, PS3_OUTPUTS, RVZ_MAX_LEVEL
from ..downloads import DownloadRunner
from ..models import Config, fmt_duration, IO_MODES, space_report
from ..system import resolve_tool_path
from .dialogs import SummaryDialog
from .state import TOOLS
from .theme import T


class RunMixin:
    """The run panel and a run's life cycle: start, pause, stop, progress, ETA, log and summary.

    Methods only; the state lives on SquashUIWindow, which mixes this in."""

    def _build_run_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 4, 8, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        self.lbl_phase = QLabel("Phase: Idle")
        self.lbl_phase.setObjectName("phase")
        self.lbl_run_info = QLabel("")  # what is running, shown while the settings are folded away
        self.lbl_run_info.setObjectName("muted")
        row.addWidget(self.lbl_phase)
        row.addStretch(1)
        row.addWidget(self.lbl_run_info)
        copy_btn, clear_btn = QPushButton("Copy log"), QPushButton("Clear")
        self._log_btns = (copy_btn, clear_btn)
        for btn in (copy_btn, clear_btn):
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(btn)
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.log.toPlainText()))
        clear_btn.clicked.connect(lambda: None if self.is_processing or self.org_busy else self._log_clear())
        lay.addLayout(row)
        prow = QHBoxLayout()
        prow.setSpacing(12)
        self.lbl_total_prog = QLabel("Batch Progress: 0 / 0")
        self.lbl_total_prog.setMinimumWidth(190)
        self.pb_total = QProgressBar()
        self.pb_total.setRange(0, 1000)
        self.pb_total.setTextVisible(True)
        self.pb_total.setFormat("%p%")
        self.pb_total.setAlignment(Qt.AlignmentFlag.AlignCenter)
        prow.addWidget(self.lbl_total_prog)
        prow.addWidget(self.pb_total, 1)
        lay.addLayout(prow)
        self.lbl_eta = QLabel("")
        self.lbl_eta.setObjectName("muted")
        lay.addWidget(self.lbl_eta)
        self.lbl_notice = QLabel("")  # the few messages that matter on pages that have no log (VPN dropped, errors)
        self.lbl_notice.setObjectName("notice")
        self.lbl_notice.setWordWrap(True)
        self.lbl_notice.hide()
        lay.addWidget(self.lbl_notice)
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setUndoRedoEnabled(False)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log.setMinimumHeight(120)
        self.log.setPlaceholderText("The live process log appears here.")
        lay.addWidget(self.log, 1)
        self._run_panel, self._log_shown, self._panel_mode = panel, True, "conv"
        return panel

    def _apply_panel_mode(self, mode: str) -> None:
        """Conversion pages show the whole run panel with its live log. Download keeps only the overall bar and the
        speed / ETA line (the table has per-file status). Organise has per-row status, so its panel is hidden."""
        kind = mode if mode in ("dl", "org") else "conv"
        if kind == self._panel_mode:
            return
        self._panel_mode = kind
        conv = kind == "conv"
        self._log_shown = conv
        self.log.setVisible(conv)
        for w in (self.lbl_phase, self.lbl_run_info, self.lbl_total_prog, self.settings_btn, *self._log_btns):
            w.setVisible(conv)
        self.lbl_notice.setVisible(not conv)
        self._run_panel.setVisible(kind != "org")
        if conv:
            self._run_panel.setMaximumHeight(16777215)
            self.splitter.setSizes([480, 320])
        else:
            self._set_settings_visible(True)
            self._run_panel.setMaximumHeight(self._run_panel.layout().totalSizeHint().height())
        self.splitter.handle(1).setEnabled(conv)

    def _poll_events(self) -> None:
        """Workers never touch Qt; they post events here. One batched redraw per tick."""
        events = []
        try:
            while True:
                events.append(self.ui_q.get_nowait())
        except queue.Empty:
            pass
        if events:
            self._apply_events(events)
        self._refresh_eta()

    # -- the live log: every line is a text block, and a task line is rewritten in place as it progresses
    def _log_write(self, text: str) -> None:
        cur = QTextCursor(self.log.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(text + "\n")

    def _task_new(self, tid: int, text: str) -> None:
        cur = QTextCursor(self.log.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        self._tasks[tid] = [cur.blockNumber(), text.count("\n") + 1]
        cur.insertText(text + "\n")

    def _task_update(self, tid: int, text: str) -> None:
        entry = self._tasks.get(tid)
        if entry is None:
            return
        start, lines = entry
        doc = self.log.document()
        first, last = doc.findBlockByNumber(start), doc.findBlockByNumber(start + lines - 1)
        if not first.isValid() or not last.isValid():
            return
        cur = QTextCursor(first)
        cur.setPosition(last.position() + last.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cur.insertText(text)
        if (now := text.count("\n") + 1) != lines:  # a task that grew or shrank pushes the later ones along
            entry[1] = now
            for other in self._tasks.values():
                if other[0] > start:
                    other[0] += now - lines

    def _log_clear(self) -> None:
        self.log.clear()
        self._tasks.clear()
        self.lbl_notice.setText("")

    def _apply_events(self, events: list[tuple]) -> None:
        # Only the newest progress update per task matters within one tick.
        newest = {ev[1]: i for i, ev in enumerate(events) if ev[0] == "update"}
        bar = self.log.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4  # stay on the newest line unless the person scrolled up
        finished, summary, dl_result, sizes_changed = False, None, None, False
        for i, ev in enumerate(events):
            match ev:
                case ("log", msg):
                    self._log_write(msg)
                case ("notice", text):
                    self.lbl_notice.setText(text)
                case ("org_note", key, token, text) if token == self.org[key].token:
                    self.org[key].status.setText(text)
                case ("org_row", key, i, text):
                    if (item := self.org[key].tree.topLevelItem(i)) is not None:
                        item.setText(2, text)
                        item.setForeground(2, T.bad if text.startswith("Failed") else T.fg)
                case ("new", tid, text):
                    self._task_new(tid, text)
                case ("update", tid, state, msg) if newest[tid] == i:
                    self._task_update(tid, f"{state} {msg}")
                case ("progress", phase, cur, total):
                    self._update_ui_state(phase, cur, total)
                case (name, text) if name.endswith("_status") and name[:-7] in TOOLS:
                    self._set_tool_status(name[:-7], text)
                case (name, path) if name.endswith("_path") and name[:-5] in TOOLS:
                    self._tool_path_found(name[:-5], path)
                case (name,) if name.endswith("_ready") and name[:-6] in TOOLS:
                    self._tool_ready(name[:-6])
                case ("work", done, total):
                    self._work = (done, total)
                    if not self._samples or self._samples[-1][1] != done:
                        self._samples.append((self._active_secs(), done))
                    self._refresh_eta(force=True)
                case ("done", result):
                    finished, summary = True, result
                case ("org_scan", key, token, findings, message, error) if token == self.org[key].token:
                    self._org_show(key, findings, message, error)
                case ("org_done", key, ok, failures):  # popups wait until this redraw is done
                    QTimer.singleShot(0, lambda k=key, o=ok, f=failures: self._org_finished(k, o, f))
                case ("dl_files", token, files, errors) if token == self._dl_token:
                    for err in errors:
                        self._log_write(f"Link problem: {err}")
                    self._dl_populate(files, errors)
                case ("dl_size", token, rf, size) if token == self._dl_token:  # found by file: its row may have moved
                    if rf.size is None and (row := self.dl_model.row_of(rf)) is not None:
                        rf.size = size
                        self.dl_model.size_changed(row)
                        sizes_changed = True
                case ("dl_note", token, text) if token == self._dl_token:
                    self.dl_status.setText(text)
                case ("dl_size_run", idx, size):
                    if idx < len(self.dl_files):
                        self.dl_files[idx].size = size
                        self.dl_model.size_changed(idx)
                case ("dl_row", idx, text):
                    self.dl_model.set_status(idx, text)
                case ("dl_scan_row", epoch, idx, text) if epoch == self._dl_epoch:
                    self.dl_model.set_status(idx, text)
                case ("dl_deleted", count, problems):  # popups wait until this redraw is done
                    QTimer.singleShot(0, lambda c=count, p=problems: self._dl_deleted(c, p))
                case ("dl_source", idx, text):
                    self.dl_model.set_source(idx, text)
                case ("dl_speed", idx, text):
                    self.dl_model.set_speed(idx, text)
                case ("dl_done", result):
                    finished, dl_result = True, result
        if follow:
            bar.setValue(bar.maximum())
        if sizes_changed:
            self._dl_update_sel()
        if finished:
            was_download = isinstance(self.runner, DownloadRunner)
            if was_download:
                self._refresh_dl(True)  # land the bar on its final value before the cleanup text
            self._cleanup_ui()
            if was_download and self._dl_after_run():
                dl_result = None  # files were lined up behind this run: it starts now, and reports when it ends
            if summary:
                QTimer.singleShot(0, lambda s=summary: self._show_summary(s))
            if dl_result is not None:
                QTimer.singleShot(0, lambda r=dl_result: self._show_dl_summary(r))

    def _active_secs(self) -> float:
        """Seconds the batch has actually been running (paused time doesn't count)."""
        if self._t0 is None:
            return 0.0
        now = time.monotonic()
        return now - self._t0 - self._paused_total - (now - self._pause_t if self._pause_t else 0.0)

    def _set_bar(self, done: float, total: float) -> None:
        """QProgressBar is 32-bit, and byte counts of a big download are not, so it always runs 0..1000."""
        self.pb_total.setValue(int(1000 * min(1.0, max(0.0, done / total))) if total > 0 else 0)

    def _refresh_eta(self, force: bool = False) -> None:
        if isinstance(self.runner, DownloadRunner):
            self._refresh_dl(force)
            return
        if not self.is_processing or self._t0 is None:
            return
        now = time.monotonic()
        if not force and now - self._eta_tick < 1.0:
            return
        self._eta_tick = now
        elapsed = self._active_secs()
        done, total = self._work
        head = f"Elapsed: {fmt_duration(elapsed)}"
        if self.is_paused:
            self.lbl_eta.setText(f"{head}   |   Paused")
            return
        # Pace over the last dozen finished titles, weighted by size; the open interval since the
        # last one counts too, so a long stall stretches the estimate instead of hiding behind it.
        t_first, b_first = self._samples[0] if self._samples else (0.0, 0)
        gained = done - b_first
        if gained <= 0 or elapsed <= t_first or total <= 0:
            self.lbl_eta.setText(f"{head}   |   Estimating time remaining (needs the first finished title)...")
            return
        left = max(0, total - done) / (gained / (elapsed - t_first))
        finish = (datetime.now() + timedelta(seconds=left)).strftime("%a %I:%M %p").replace(" 0", " ", 1)
        self.lbl_eta.setText(f"{head}   |   Est. remaining: ~{fmt_duration(left)}   |   Done around {finish}")

    def _update_ui_state(self, phase: str, current: int, total: int) -> None:
        self.lbl_phase.setText(f"Phase: {phase}")
        self._set_bar(current, total)
        kind = "Organize" if phase == "Organizing" else "Batch"
        self.lbl_total_prog.setText(f"{kind} Progress: {current} / {total}")

    def _cleanup_ui(self) -> None:
        self.is_processing = False
        self.runner = None
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.pause_btn.setText("Pause")
        self.lbl_phase.setText("Phase: Stopped" if self._stopping else "Phase: Complete")
        self.lbl_eta.setText(f"{'Stopped after' if self._stopping else 'Finished in'} {fmt_duration(self._active_secs())}")
        self._stopping = False
        self._t0 = None
        self.dl_fetch_btn.setEnabled(True)
        self.lbl_run_info.setText("")
        self._set_settings_visible(True)

    def _toggle_pause(self) -> None:
        if self.is_paused:
            self.is_paused = False
            if self._pause_t is not None:
                self._paused_total += time.monotonic() - self._pause_t
                self._pause_t = None
            self.pause_event.set()
            self.pause_btn.setText("Pause")
            self.ui_q.put(("log", "--- RESUMED: Pulling new jobs from queue ---"))
        else:
            self.is_paused = True
            self._pause_t = time.monotonic()
            self.pause_event.clear()
            self.pause_btn.setText("Resume")
            self.ui_q.put(("log", "--- PAUSED: Active conversions will finish, but no new jobs will start ---"))

    def _stop_run(self) -> None:
        if self.org_busy:
            if not self._org_stop.is_set():
                self._org_stop.set()  # checked between items: the current item finishes, the rest are left alone
                self.stop_btn.setEnabled(False)
                self.lbl_phase.setText("Phase: Stopping...")
                self.ui_q.put(("log", "--- STOP requested: finishing the current item ---"))
            return
        runner = self.runner
        if not self.is_processing or runner is None or self._stopping:
            return
        keeps = ("Partly downloaded files are kept and resume next time." if isinstance(runner, DownloadRunner)
                 else "Active tools are killed and their partial output is deleted. Finished items are kept.")
        if not self._ask("Stop", f"Stop the run now?\n\n{keeps}"):
            return
        self._stopping = True
        if isinstance(runner, DownloadRunner):
            self._dl_clear_next()  # stopping the run means files lined up behind it don't start either
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.lbl_phase.setText("Phase: Stopping...")
        self.ui_q.put(("log", "--- STOP requested ---"))
        threading.Thread(target=runner.shutdown, daemon=True).start()  # killing children can take a moment

    @staticmethod
    def _run_info(cfg: Config) -> str:
        text = f"{cfg.mode.upper()}  |  {cfg.source}  ->  {cfg.dest or 'alongside the sources'}"
        return text if len(text) <= 80 else text[:38] + "..." + text[-39:]

    def _start_processing(self) -> None:
        if self.is_processing:
            return
        mode = self._current_mode()
        if mode == "dl":
            self._start_download()
        elif mode == "org":
            self._org_apply()
        elif cfg := self._build_config(mode):
            self._launch(cfg)

    def _build_config(self, mode: str) -> Config | None:
        """Read the page's widgets into a Config, or tell the person what is wrong and return None."""
        clean = self._clean
        tv = self.tabs[mode]
        source_raw, dest_raw = clean(tv.source.text()), clean(tv.dest.text())
        source = Path(source_raw)
        tools = self._resolved_tools()
        ps3_keys = resolve_tool_path(clean(self.ps3_keys.text()))
        engine_mode = "pkg" if mode == "pkg5" else mode  # the PS5 page is the "pkg" engine mode with pkg-to-anyps5
        ps5_target = "kyty" if self.ps5_kyty.isChecked() else "anyps5"
        relink = mode == "pkg5" and ps5_target == "anyps5"
        packs_zar_pkg = self.pkg_zar.isChecked() if mode == "pkg" else mode == "pkg5" and ps5_target == "kyty"
        pkg_output = "zar" if packs_zar_pkg else "folder"
        pkg_tool = "anyps5" if mode == "pkg5" else "shadplus"  # PKG_TOOLS' keys are TOOLS' names

        if not source_raw or not source.is_dir():
            self._error("Error", f"Please select a valid source directory.\n\nPath checked: {source}")
            return None
        if not self._tools_ok(mode, engine_mode, tools, pkg_tool=pkg_tool, relink=relink,
                              packs_zar=mode == "zar" or pkg_output == "zar", ps3_keys=ps3_keys):
            return None
        temp_raw = clean(self.temp_dir.text())
        if temp_raw and not Path(temp_raw).is_dir():
            self._error("Error", f"The temp folder does not exist.\n\nPath checked: {temp_raw}")
            return None

        return Config(
            source=source,
            dest=Path(dest_raw) if dest_raw else None,
            chdman=tools["chdman"],
            extract=mode != "zar" and tv.extract.isChecked(),
            verify=tv.verify.isChecked() or engine_mode in ("pkg", "ps3"),  # the PKG check is cheap and always runs
            delete=tv.delete.isChecked(),
            thread_budget=max(1, self.thread_budget.value()),
            dolphintool=tools["dolphin"],
            maxcso=tools["maxcso"],
            pkgext=tools["shadplus"],
            anyps5=tools["anyps5"],
            zarchive=tools["zarchive"],
            xdvdfs=tools["xdvdfs"],
            ps3dec=tools["ps3qdd"],
            ps3_keys=ps3_keys,
            ps3_output=PS3_OUTPUTS.get(self.ps3_output.currentText(), "iso"),
            relinker=tools["relinker"] if relink else "",
            ps5_target=ps5_target,
            pkg_output=pkg_output,
            pkg_tool=pkg_tool,
            pkg_merge=mode == "pkg" and self.pkg_merge.isChecked(),
            pkg_layout=PKG_LAYOUTS.get(self.pkg_layout.currentText(), "folder"),
            mode=engine_mode,
            rvz_scrub=self.rvz_scrub.isChecked(),
            rvz_level=min(RVZ_MAX_LEVEL, max(1, self.rvz_level.value())),
            chd_compression=self.chd_compression.currentText().lower(),
            cso_speed=self.cso_speed.currentText().lower(),
            io_mode=IO_MODES.get(self.io_mode.currentText(), "auto"),
            temp_dir=temp_raw,
        )

    def _launch(self, cfg: Config, only: frozenset[Path] | None = None) -> None:
        self._last_cfg = cfg
        self._t0, self._paused_total, self._pause_t = time.monotonic(), 0.0, None
        self._work, self._eta_tick = (0, 0), 0.0
        self._samples.clear()
        self._samples.append((0.0, 0))
        self.lbl_eta.setText("")
        self.is_processing = True
        self.is_paused = False
        self.pause_event.set()
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setText("Pause")
        self._update_ui_state("Initializing", 0, 1)
        self.lbl_run_info.setText(self._run_info(cfg))
        self._set_settings_visible(False)  # the log gets the window while a batch runs
        if only is None:
            self._log_clear()
        else:  # a retry keeps the earlier log so the original errors stay readable
            self.ui_q.put(("log", f"\n=== RETRYING {len(only)} failed item(s) ===\n"))
        self.runner = BatchRunner(cfg, self.ui_q.put, self.pause_event, only)
        threading.Thread(target=self.runner.run, daemon=True).start()

    def _retry(self, paths: frozenset[Path]) -> None:
        if not self.is_processing and self._last_cfg:
            self._launch(self._last_cfg, only=paths)

    def _show_summary(self, summary: BatchSummary) -> None:
        report = space_report(summary)
        if not summary.failures:
            self._info("Batch Complete", f"All {summary.total} item(s) finished successfully."
                       + (f"\n\n{report}" if report else ""))
            return
        SummaryDialog(self, summary, report, self._retry).open()
