"""PySide6 desktop interface."""

from __future__ import annotations

import multiprocessing
import queue
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__
from .memory import memlog
from .platform_utils import default_output_dir, open_folder

SUPPORTED = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".wma"}

FORMATS = [
    ("MP3 · 320 kbps", "mp3", 320),
    ("MP3 · 256 kbps", "mp3", 256),
    ("MP3 · 192 kbps", "mp3", 192),
    ("WAV · 24-bit (lossless)", "wav", 0),
]
QUALITIES = [
    ("Balanced (recommended)", "balanced"),
    ("Maximum (cleanest vocals, about 1.8× slower)", "maximum"),
    ("Fast (about 1.8× faster, a little more bleed)", "fast"),
]

ACCENT = "#7C5CFF"


def resource_path(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


# --------------------------------------------------------------------------------------
class EngineProcess(QObject):
    """Runs the separation in a worker process (stemsplitter.worker), so the window never loads PyTorch.

    The process lives for a whole batch (models are loaded once) and a short while after it,
    so a song added right away reuses the loaded models; then it exits and its RAM goes back to
    the OS.
    """

    IDLE_SECONDS = 30
    POLL_MS = 50

    status = Signal(int, float, str)  # row, fraction, text
    file_done = Signal(int, dict, float)  # row, {stem: path}, seconds
    file_failed = Signal(int, str)
    log = Signal(str)
    device = Signal(str)
    finished = Signal(bool)  # cancelled?
    stopped = Signal()  # the worker process exited (its memory is released)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctx = multiprocessing.get_context("spawn")  # fork + Qt don't mix; spawn is the default on Win/macOS
        self._proc = None
        self._commands = self._events = self._cancel = None
        self._pending: list[int] = []  # rows of the running batch not yet reported done/failed
        self.busy = False
        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._drain)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(self.IDLE_SECONDS * 1000)
        self._idle.timeout.connect(self.shutdown)

    def run(self, jobs: list[tuple[int, Path]], opts) -> None:
        self._idle.stop()
        if self._proc is not None and not self._proc.is_alive():
            self._cleanup()  # it died while idle
        if self._proc is None:
            self._spawn()
        self._cancel.clear()
        self._pending = [row for row, _ in jobs]
        self.busy = True
        self._commands.put((jobs, opts))
        self._poll.start()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _spawn(self) -> None:
        from .worker import serve

        self._commands, self._events, self._cancel = self._ctx.Queue(), self._ctx.Queue(), self._ctx.Event()
        self._proc = self._ctx.Process(target=serve, args=(self._commands, self._events, self._cancel),
                                       name="StemSplitter worker", daemon=True)
        self._proc.start()

    def _drain(self) -> None:
        messages = []
        while True:
            try:
                messages.append(self._events.get_nowait())
            except queue.Empty:
                break
        for i, msg in enumerate(messages):
            kind = msg[0]
            if kind == "status":
                nxt = messages[i + 1] if i + 1 < len(messages) else None
                if nxt is not None and nxt[0] == "status" and nxt[1] == msg[1]:
                    continue  # superseded by a newer progress value in the same batch of messages
                self.status.emit(*msg[1:])
            elif kind == "done":
                self._report(msg[1])
                self.file_done.emit(*msg[1:])
            elif kind == "failed":
                self._report(msg[1])
                self.file_failed.emit(*msg[1:])
            elif kind == "log":
                self.log.emit(msg[1])
            elif kind == "device":
                self.device.emit(msg[1])
            elif kind == "finished":
                self._finish(msg[1])
                return
        if self.busy and not self._proc.is_alive():  # crashed (e.g. out of memory): fail what is left
            code = self._proc.exitcode
            for row in list(self._pending):
                self._report(row)
                self.file_failed.emit(row, f"The separation process stopped unexpectedly (exit code {code})")
            self._cleanup()
            self._finish(False)

    def _report(self, row: int) -> None:
        if row in self._pending:
            self._pending.remove(row)

    def _finish(self, cancelled: bool) -> None:
        self._poll.stop()
        self.busy = False
        if self._proc is not None:
            self._idle.start()
        self.finished.emit(cancelled)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Ask the worker to exit (it finishes the current step first); kill it after `timeout` s."""
        self._idle.stop()
        self._poll.stop()
        if self._proc is None:
            return
        if self._proc.is_alive():
            self._commands.put(None)
            deadline = time.monotonic() + timeout
            while self._proc.is_alive() and time.monotonic() < deadline:
                try:  # keep the pipe drained so the worker never blocks while exiting
                    while True:
                        self._events.get_nowait()
                except queue.Empty:
                    pass
                self._proc.join(0.05)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(2)
        self._cleanup()

    def _cleanup(self) -> None:
        for q in (self._commands, self._events):
            q.cancel_join_thread()
            q.close()
        self._proc = self._commands = self._events = self._cancel = None
        self.busy = False
        self.stopped.emit()


# --------------------------------------------------------------------------------------
class DropList(QListWidget):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setObjectName("dropList")
        self.setMinimumHeight(180)
        self.placeholder = "Drop MP3 files here\nor click “Add files…”"

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                paths += sorted(x for x in p.rglob("*") if x.suffix.lower() in SUPPORTED)
            elif p.suffix.lower() in SUPPORTED:
                paths.append(p)
        if paths:
            self.files_dropped.emit(paths)
        e.acceptProposedAction()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor("#8a8aa0"))
            f = QFont(self.font())
            f.setPixelSize(17)
            painter.setFont(f)
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, self.placeholder)


# --------------------------------------------------------------------------------------
class MainWindow(QMainWindow):
    ROLE_PATH = Qt.UserRole
    ROLE_STATE = Qt.UserRole + 1  # "queued" | "running" | "done" | "failed"

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}")
        self.resize(760, 680)
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.engine = EngineProcess(self)
        self.engine.status.connect(self._on_status)
        self.engine.file_done.connect(self._on_done)
        self.engine.file_failed.connect(self._on_failed)
        self.engine.log.connect(self._append_log)
        self.engine.device.connect(self._on_device)
        self.engine.finished.connect(self._on_finished)
        self.engine.stopped.connect(lambda: memlog("worker process stopped (GUI process)", self._append_log))
        self._build_ui()
        self._load_settings()
        memlog("window created (GUI process)", self._append_log)

    # -- UI -----------------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(14)

        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel("Split any song into Vocals · Drums · Bass · Other — one MP3 per stem")
        subtitle.setObjectName("subtitle")
        lay.addWidget(title)
        lay.addWidget(subtitle)

        # files
        self.list = DropList()
        self.list.files_dropped.connect(self.add_files)
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_add = QPushButton("Add files…")
        self.btn_add.clicked.connect(self._pick_files)
        self.btn_remove = QPushButton("Remove selected")
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear = QPushButton("Clear list")
        self.btn_clear.clicked.connect(self._clear)
        for b in (self.btn_add, self.btn_remove, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        # settings card
        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("Save stems to"), 0, 0)
        self.out_edit = QLineEdit()
        grid.addWidget(self.out_edit, 0, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self._pick_output)
        grid.addWidget(b, 0, 2)

        grid.addWidget(QLabel("Quality"), 1, 0)
        self.quality = QComboBox()
        for label, key in QUALITIES:
            self.quality.addItem(label, key)
        grid.addWidget(self.quality, 1, 1, 1, 2)

        grid.addWidget(QLabel("Output format"), 2, 0)
        self.fmt = QComboBox()
        for label, fmt, br in FORMATS:
            self.fmt.addItem(label, (fmt, br))
        grid.addWidget(self.fmt, 2, 1, 1, 2)

        self.chk_inst = QCheckBox("Also save an Instrumental track (everything except vocals)")
        grid.addWidget(self.chk_inst, 3, 1, 1, 2)
        self.chk_gp = QCheckBox("Also split Guitar and Piano out of Other (6 stems)")
        grid.addWidget(self.chk_gp, 4, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        lay.addWidget(card)

        # progress
        self.status = QLabel("Ready")
        self.status.setObjectName("status")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        lay.addWidget(self.status)
        lay.addWidget(self.progress)

        row = QHBoxLayout()
        self.device_label = QLabel("")
        self.device_label.setObjectName("device")
        row.addWidget(self.device_label, 1)
        self.btn_log = QPushButton("Show log")
        self.btn_log.setCheckable(True)
        self.btn_log.toggled.connect(
            lambda on: (self.log.setVisible(on), self.btn_log.setText("Hide log" if on else "Show log"))
        )
        self.btn_open = QPushButton("Open output folder")
        self.btn_open.clicked.connect(self._open_output)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setEnabled(False)
        self.btn_start = QPushButton("Split stems")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.start)
        for w in (self.btn_log, self.btn_open, self.btn_cancel, self.btn_start):
            row.addWidget(w)
        lay.addLayout(row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setVisible(False)
        self.log.setFixedHeight(140)
        f = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(9)
        self.log.setFont(f)
        lay.addWidget(self.log)

        about = QAction("About", self)
        about.triggered.connect(self._about)
        self.menuBar().addMenu("Help").addAction(about)

    # -- settings -----------------------------------------------------------------------
    def _load_settings(self):
        self.out_edit.setText(self.settings.value("output_dir", str(default_output_dir())))
        # saved by name (older versions saved an index into a different list, so it is ignored)
        idx = self.quality.findData(self.settings.value("quality", "balanced"))
        self.quality.setCurrentIndex(max(0, idx))
        self.fmt.setCurrentIndex(int(self.settings.value("format_idx", 0)))
        self.chk_inst.setChecked(self.settings.value("instrumental", "false") == "true")
        self.chk_gp.setChecked(self.settings.value("guitar_piano", "false") == "true")

    def _save_settings(self):
        self.settings.setValue("output_dir", self.out_edit.text())
        self.settings.setValue("quality", self.quality.currentData())
        self.settings.setValue("format_idx", self.fmt.currentIndex())
        self.settings.setValue("instrumental", "true" if self.chk_inst.isChecked() else "false")
        self.settings.setValue("guitar_piano", "true" if self.chk_gp.isChecked() else "false")

    # -- file list ----------------------------------------------------------------------
    def add_files(self, paths):
        existing = {self.list.item(i).data(self.ROLE_PATH) for i in range(self.list.count())}
        for p in paths:
            p = str(Path(p).resolve())
            if p in existing:
                continue
            item = QListWidgetItem(f"⏳  {Path(p).name}")
            item.setData(self.ROLE_PATH, p)
            item.setData(self.ROLE_STATE, "queued")
            item.setToolTip(p)
            self.list.addItem(item)
            existing.add(p)
        self.list.viewport().update()

    def _pick_files(self):
        start = self.settings.value("last_input_dir", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose songs",
            start,
            "Audio (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.aiff *.aif *.wma);;All files (*)",
        )
        if files:
            self.settings.setValue("last_input_dir", str(Path(files[0]).parent))
            self.add_files(files)

    def _remove_selected(self):
        if self._busy():
            return
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.list.viewport().update()

    def _clear(self):
        if not self._busy():
            self.list.clear()
            self.list.viewport().update()

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(self, "Save stems to", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _open_output(self):
        p = Path(self.out_edit.text()).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        open_folder(p)

    # -- run ----------------------------------------------------------------------------
    def _busy(self) -> bool:
        return self.engine.busy

    def start(self):
        if self._busy():
            return
        jobs = [
            (i, Path(self.list.item(i).data(self.ROLE_PATH)))
            for i in range(self.list.count())
            if self.list.item(i).data(self.ROLE_STATE) in ("queued", "failed")
        ]
        if not jobs:
            QMessageBox.information(self, APP_NAME, "Add one or more songs first (drag & drop or “Add files…”).")
            return
        out = Path(self.out_edit.text()).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Can't use that output folder:\n{exc}")
            return
        self._save_settings()

        from .engine import Options  # light import (numpy/soundfile only)

        fmt, br = self.fmt.currentData()
        opts = Options(
            output_dir=out,
            bitrate_kbps=br or 320,
            quality=self.quality.currentData(),
            also_instrumental=self.chk_inst.isChecked(),
            guitar_piano=self.chk_gp.isChecked(),
            output_format=fmt,
        )

        for row, _ in jobs:
            self._set_item(row, "queued", "⏳")
        self._set_running(True)
        self._append_log(f"Starting {len(jobs)} file(s) → {out}")
        self.engine.run(jobs, opts)

    def _cancel(self):
        self.engine.cancel()
        self.status.setText("Cancelling after the current step…")
        self.btn_cancel.setEnabled(False)

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        for w in (self.btn_add, self.btn_remove, self.btn_clear, self.quality, self.fmt, self.chk_inst, self.chk_gp,
                  self.out_edit):
            w.setEnabled(not running)

    def _set_item(self, row: int, state: str, icon: str, extra: str = ""):
        item = self.list.item(row)
        if item is None:
            return
        item.setData(self.ROLE_STATE, state)
        name = Path(item.data(self.ROLE_PATH)).name
        item.setText(f"{icon}  {name}" + (f"   —   {extra}" if extra else ""))

    @Slot(int, float, str)
    def _on_status(self, row: int, frac: float, text: str):
        item = self.list.item(row)
        name = Path(item.data(self.ROLE_PATH)).name if item else ""
        self._set_item(row, "running", "▶️", f"{frac:.0%}")
        self.progress.setValue(int(frac * 1000))
        self.status.setText(f"{name}: {text}")

    @Slot(str)
    def _on_device(self, device: str):
        self.device_label.setText(f"Processing on: {device}")

    @Slot(int, dict, float)
    def _on_done(self, row: int, stems: dict, seconds: float):
        mins, secs = divmod(int(seconds), 60)
        self._set_item(row, "done", "✅", f"done in {mins}m {secs:02d}s")
        self._append_log("Saved:\n  " + "\n  ".join(stems.values()))

    @Slot(int, str)
    def _on_failed(self, row: int, message: str):
        if message == "Cancelled":
            self._set_item(row, "queued", "⏹", "cancelled")
            return
        self._set_item(row, "failed", "⚠️", message.splitlines()[0][:120])
        self._append_log(f"ERROR: {message}")

    @Slot(bool)
    def _on_finished(self, cancelled: bool):
        self._set_running(False)
        done = sum(1 for i in range(self.list.count()) if self.list.item(i).data(self.ROLE_STATE) == "done")
        failed = sum(1 for i in range(self.list.count()) if self.list.item(i).data(self.ROLE_STATE) == "failed")
        if cancelled:
            self.status.setText("Cancelled")
            self.progress.setValue(0)
        elif failed:
            self.status.setText(f"Finished with {failed} error(s) — see the log")
            if not self.log.isVisible():
                self.btn_log.setChecked(True)
        else:
            self.status.setText(f"All done — {done} song(s) split")
            self.progress.setValue(1000)

    @Slot(str)
    def _append_log(self, text: str):
        self.log.appendPlainText(text.rstrip())

    def _about(self):
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME} {__version__}</b><br><br>"
            "All stems: BS-RoFormer SW (jarredou)<br>"
            "Maximum quality vocals: + MelBand-RoFormer (Kimberley Jensen)<br>"
            "Powered by python-audio-separator, PyTorch and FFmpeg.<br><br>"
            "Models are downloaded once on first use (~0.7 GB, +0.9 GB for Maximum).",
        )

    def closeEvent(self, e):
        if self._busy():
            if QMessageBox.question(self, APP_NAME, "A song is still being processed. Quit anyway?") != QMessageBox.Yes:
                e.ignore()
                return
            # Stop at the next chunk boundary and give the worker time to finish the file it is writing.
            self.hide()
            self.engine.cancel()
            self.engine.shutdown(timeout=30)
        else:
            self.engine.shutdown()
        self._save_settings()
        e.accept()


# --------------------------------------------------------------------------------------
STYLE = f"""
QWidget {{ font-size: 13px; }}
QLabel#title {{ font-size: 26px; font-weight: 700; }}
QLabel#subtitle {{ color: #8a8aa0; margin-bottom: 4px; }}
QLabel#status {{ font-weight: 600; }}
QLabel#device {{ color: #8a8aa0; font-size: 12px; }}
QListWidget#dropList {{
    border: 2px dashed {ACCENT}; border-radius: 12px; padding: 8px;
}}
QListWidget#dropList::item {{ padding: 6px 4px; }}
QFrame#card {{ border: 1px solid palette(midlight); border-radius: 10px; }}
QPushButton {{
    padding: 7px 14px; border-radius: 7px; border: 1px solid palette(mid); background: palette(button);
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:checked {{ background: palette(midlight); }}
QPushButton:disabled {{ color: palette(mid); border-color: palette(midlight); }}
QPushButton#primary {{
    background: {ACCENT}; color: white; font-weight: 700; padding: 8px 22px; border: none;
}}
QPushButton#primary:disabled {{ background: #b9aefc; }}
QPushButton#primary:hover {{ background: #6a48ff; }}
QProgressBar {{ border: none; border-radius: 5px; background: palette(midlight); }}
QProgressBar::chunk {{ border-radius: 5px; background: {ACCENT}; }}
"""


def run_gui() -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(APP_NAME)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icon = resource_path("assets/icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    return app.exec()
