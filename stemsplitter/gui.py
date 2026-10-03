"""PySide6 desktop interface: the window (built from stemsplitter.ui) and the EngineProcess that runs the worker."""

from __future__ import annotations

import multiprocessing
import queue
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__
from .memory import memlog
from .platform_utils import default_output_dir, open_folder
from .ui.pages import AboutPage, BatchPage, SettingsPage
from .ui.theme import Sizes, Spacing, Type, dark_palette, pick_font_family, stylesheet
from .ui.widgets import (
    DropZone,
    FileList,
    FileQueue,
    OutputSettings,
    ProcessingPanel,
    Sidebar,
    StatusBar,
    format_duration,
    page_header,
    scan_paths,
)

FORMATS = [
    ("MP3 · 320 kbps", "mp3", 320),
    ("MP3 · 256 kbps", "mp3", 256),
    ("MP3 · 192 kbps", "mp3", 192),
    ("WAV · 24-bit (lossless)", "wav", 0),
]
RESERVES = [  # memory the system keeps free (see memgov.py); 0 = automatic
    ("Automatic (recommended)", 0),
    ("1 GB", 1024),
    ("2 GB", 2048),
    ("3 GB", 3072),
    ("4 GB", 4096),
    ("6 GB", 6144),
    ("8 GB", 8192),
]
QUALITIES = [
    ("Balanced (recommended)", "balanced"),
    ("Maximum (cleanest vocals, about 1.8× slower)", "maximum"),
    ("Fast (about 1.8× faster, a little more bleed)", "fast"),
]
# (engine output name, checkbox text, tooltip), in the engine's OUTPUTS order
STEMS = [
    ("Vocals", "Vocals", "Lead and backing vocals"),
    ("Drums", "Drums", "Drum kit and percussion"),
    ("Bass", "Bass", "Bass guitar, synth bass, 808"),
    ("Other", "Other (melody)", "Everything else: guitars, keys, synths, strings… "
                                "(without Guitar and Piano when those are ticked too)"),
    ("Guitar", "Guitar", "Split out of Other"),
    ("Piano", "Piano (keys)", "Split out of Other"),
    ("Instrumental", "Instrumental (no vocals)", "The whole song without the vocals"),
    ("No Drums", "No Drums (song without drums)", "The whole song with only the drums taken out"),
]
DEFAULT_STEMS = ["Vocals", "Drums", "Bass", "Other"]
QUALITY_HINTS = {
    "balanced": "The best balance between separation quality and speed.",
    "maximum": "Adds a second vocal model for the cleanest vocals. Takes longer.",
    "fast": "The quickest preset, with a little more bleed between the stems.",
}


def resource_path(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


# --------------------------------------------------------------------------------------
class EngineProcess(QObject):
    """Runs the separation in a worker process (stemsplitter.worker), so the window never loads PyTorch.

    The process lives for a whole batch (models are loaded once) and a short while after it,
    so a song added right away reuses the loaded models; then it exits and its RAM goes back to
    the OS. It is also started (prewarm) as soon as songs are added, so PyTorch and the model
    are usually loaded by the time Split is pressed.

    If the worker dies in the middle of a batch (for example, the OS ends it when memory runs
    out), it is started again and continues with the songs that were left, each one from its
    last checkpoint, so no finished work is lost.
    """

    IDLE_SECONDS = 30
    PREWARM_IDLE_SECONDS = 90  # time to pick the options after adding songs
    POLL_MS = 50
    MAX_RESTARTS = 3  # per batch

    status = Signal(int, float, str)  # row, fraction, text
    file_done = Signal(int, dict, float)  # row, {stem: path}, seconds
    file_failed = Signal(int, str)
    log = Signal(str)
    device = Signal(str)
    finished = Signal(bool)  # cancelled?
    memory = Signal(dict)  # the memory governor's snapshot, about once a second while busy
    stopped = Signal()  # the worker process exited (its memory is released)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctx = multiprocessing.get_context("spawn")  # fork + Qt don't mix; spawn is the default on Win/macOS
        self._proc = None
        self._commands = self._events = self._cancel = None
        self._pending: list[int] = []  # rows of the running batch not yet reported done/failed
        self._jobs: list[tuple[int, Path]] = []
        self._opts = None
        self._restarts = 0
        self.busy = False
        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._drain)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self.shutdown)

    def run(self, jobs: list[tuple[int, Path]], opts) -> None:
        self._idle.stop()
        if self._proc is not None and not self._proc.is_alive():
            self._cleanup()  # it died while idle
        if self._proc is None:
            self._spawn()
        self._cancel.clear()
        self._jobs, self._opts, self._restarts = list(jobs), opts, 0
        self._pending = [row for row, _ in jobs]
        self.busy = True
        self._commands.put(("run", jobs, opts))
        self._poll.start()

    def prewarm(self, quality: str, reserve_mb: int = 0, unlimited: bool = False) -> None:
        """Start the worker and load the preset's models in the background (no-op while busy)."""
        if self.busy:
            return
        if self._proc is not None and not self._proc.is_alive():
            self._cleanup()
        if self._proc is None:
            self._spawn()
        self._commands.put(("prepare", quality, reserve_mb, unlimited))
        self._idle.start(max(self._idle.remainingTime(), self.PREWARM_IDLE_SECONDS * 1000))
        self._poll.start()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _spawn(self) -> None:
        from .worker import serve

        # Held on self on purpose: Process.start() drops its args, and on macOS/Linux a collected Event
        # unlinks its named semaphore before the worker can open it.
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
            elif kind == "memory":
                self.memory.emit(msg[1])
            elif kind == "prepared" and not self.busy:
                self._poll.stop()
            elif kind == "finished":
                self._finish(msg[1])
                return
        if self.busy and not self._proc.is_alive():  # crashed (e.g. ended by the OS when out of memory)
            code = self._proc.exitcode
            if self._pending and self._restarts < self.MAX_RESTARTS:
                # start again with the songs that are left; each continues from its checkpoint
                self._restarts += 1
                left = [(row, path) for row, path in self._jobs if row in self._pending]
                cancel_requested = self._cancel.is_set()
                self.log.emit(f"The separation process stopped (exit code {code}); restarting it and "
                              f"continuing where it left off ({self._restarts}/{self.MAX_RESTARTS})")
                self._cleanup()
                self._spawn()
                if cancel_requested:
                    self._cancel.set()
                self.busy = True
                self._commands.put(("run", left, self._opts))
                return
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
            self._idle.start(self.IDLE_SECONDS * 1000)
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
class MainWindow(QMainWindow):
    """The window: wires the widgets of `stemsplitter.ui` to the queue and to the EngineProcess."""

    ROLE_PATH = FileList.ROLE_PATH
    ROLE_STATE = FileList.ROLE_STATE  # "queued" | "running" | "done" | "failed"
    PAGE_SPLIT = 0
    # progress texts whose time says nothing about the separation: the time-left estimate restarts after them
    UNTIMED = ("Starting", "Loading", "Downloading", "Waiting")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}")
        self._fit_to_screen()
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.engine = EngineProcess(self)
        self.engine.status.connect(self._on_status)
        self.engine.file_done.connect(self._on_done)
        self.engine.file_failed.connect(self._on_failed)
        self.engine.log.connect(self._append_log)
        self.engine.device.connect(self._on_device)
        self.engine.memory.connect(self._on_memory)
        self.engine.finished.connect(self._on_finished)
        self.engine.stopped.connect(self._on_worker_stopped)
        self._current_row: int | None = None  # the song the processing panel follows
        self._timing: dict[int, tuple[float, float]] = {}  # row -> (time, fraction) the time left is measured from
        self._panel_ready = True  # the panel shows the queue summary (not a finished batch's result)
        self._build_ui()
        self._load_settings()
        memlog("window created (GUI process)", self._append_log)

    def _fit_to_screen(self):
        """Open at the minimum size; `center_on_screen` places the window once the UI is built."""
        min_w, min_h = Sizes.WINDOW_MIN
        screen = QGuiApplication.primaryScreen()
        if screen is not None:  # small or scaled-up screens: never open larger than the screen
            avail = screen.availableGeometry()
            min_w, min_h = min(min_w, avail.width()), min(min_h, avail.height() - 40)
        self.setMinimumSize(min_w, min_h)
        self.resize(min_w, min_h)

    def center_on_screen(self):
        """Center the whole window, title bar included, on the primary screen's work area."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        self.winId()  # creates the native window, so frameGeometry() includes the title bar and borders
        frame = self.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        self.move(frame.topLeft())

    # -- UI -----------------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = Sidebar(__version__)
        outer.addWidget(self.sidebar)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_split_page())
        self.batch_page = BatchPage()
        self.batch_page.btn_folder.clicked.connect(self._pick_folder)
        self.batch_page.btn_split.clicked.connect(lambda: self.sidebar.select(self.PAGE_SPLIT))
        self.pages.addWidget(self.batch_page)
        self.settings_page = SettingsPage(RESERVES)
        self.reserve = self.settings_page.reserve
        self.chk_unlimited = self.settings_page.chk_unlimited
        self.pages.addWidget(self.settings_page)
        self.pages.addWidget(AboutPage(APP_NAME, __version__))
        self.sidebar.page_changed.connect(self.pages.setCurrentIndex)
        right.addWidget(self.pages, 1)
        self.status_bar = StatusBar()
        right.addWidget(self.status_bar)
        outer.addLayout(right, 1)

        model = self.list.model()
        for sig in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            sig.connect(self._on_queue_changed)
        self.quality.currentIndexChanged.connect(self._on_options_changed)
        self.fmt.currentIndexChanged.connect(self._on_options_changed)
        self._on_queue_changed()

    def _build_split_page(self) -> QWidget:
        content = QWidget()
        content.setObjectName("scrollContent")
        lay = QVBoxLayout(content)
        lay.setContentsMargins(Spacing.XXL, Spacing.XXL - 4, Spacing.XXL, Spacing.XL)
        lay.setSpacing(Spacing.XL)
        lay.addWidget(page_header("Split Audio into Stems",
                                  "Separate any song into the stems you pick: vocals, drums, bass, melody, guitar, piano, "
                                  "or the whole song without vocals or without drums."))

        self.drop = DropZone()
        self.drop.files_dropped.connect(self.add_files)
        self.btn_add = self.drop.btn_add
        self.btn_add.clicked.connect(self._pick_files)
        self.btn_add_folder = self.drop.btn_folder
        self.btn_add_folder.clicked.connect(self._pick_folder)
        lay.addWidget(self.drop)

        self.queue = FileQueue()
        self.list = self.queue.list
        self.list.files_dropped.connect(self.add_files)
        self.list.remove_requested.connect(self._remove_item)
        self.btn_remove = self.queue.btn_remove
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear = self.queue.btn_clear
        self.btn_clear.clicked.connect(self._clear)
        lay.addWidget(self.queue)

        self.output = OutputSettings(QUALITIES, FORMATS, STEMS)
        self.out_edit = self.output.out_edit
        self.output.btn_browse.clicked.connect(self._pick_output)
        self.quality = self.output.quality
        self.fmt = self.output.fmt
        self.stem_checks = self.output.stem_checks
        for chk in self.stem_checks.values():
            chk.toggled.connect(self._on_options_changed)
        lay.addWidget(self.output)

        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setVisible(False)
        self.log.setFixedHeight(200)
        f = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(9)
        self.log.setFont(f)
        lay.addWidget(self.log)
        lay.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(content)

        self.panel = ProcessingPanel()
        self.btn_start = self.panel.btn_start
        self.btn_start.clicked.connect(self.start)
        self.btn_cancel = self.panel.btn_cancel
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_log = self.panel.btn_log
        self.btn_log.toggled.connect(self._show_log)
        self.btn_open = self.panel.btn_open
        self.btn_open.clicked.connect(self._open_output)

        page = QWidget()
        page.setObjectName("page")
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(self.scroll, 1)
        bottom = QVBoxLayout()
        bottom.setContentsMargins(Spacing.XXL, Spacing.MD, Spacing.XXL, Spacing.LG + 4)
        bottom.addWidget(self.panel)
        pl.addLayout(bottom)
        return page

    def _show_log(self, on: bool):
        self.log.setVisible(on)
        if on:
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(self.log))

    # -- settings -----------------------------------------------------------------------
    def _load_settings(self):
        self.out_edit.setText(self.settings.value("output_dir", str(default_output_dir())))
        # saved by name (older versions saved an index into a different list, so it is ignored)
        idx = self.quality.findData(self.settings.value("quality", "balanced"))
        self.quality.setCurrentIndex(max(0, idx))
        self.fmt.setCurrentIndex(int(self.settings.value("format_idx", 0)))
        saved = str(self.settings.value("stems", "") or "")
        if saved:
            stems = saved.split(",")
        else:  # versions before the stem picker: 4 stems plus the two extra switches
            stems = [*DEFAULT_STEMS]
            if self.settings.value("instrumental", "false") == "true":
                stems.append("Instrumental")
            if self.settings.value("guitar_piano", "false") == "true":
                stems += ["Guitar", "Piano"]
        for name, chk in self.stem_checks.items():
            chk.setChecked(name in stems)
        self.reserve.setCurrentIndex(max(0, self.reserve.findData(int(self.settings.value("memory_reserve_mb", 0)))))
        self.chk_unlimited.setChecked(self.settings.value("ignore_memory_limit", "true") == "true")  # on by default
        self._on_options_changed()

    def _save_settings(self):
        self.settings.setValue("output_dir", self.out_edit.text())
        self.settings.setValue("quality", self.quality.currentData())
        self.settings.setValue("format_idx", self.fmt.currentIndex())
        if self._selected_stems():  # with none ticked, the last choice is kept
            self.settings.setValue("stems", ",".join(self._selected_stems()))
        self.settings.setValue("memory_reserve_mb", self.reserve.currentData())
        self.settings.setValue("ignore_memory_limit", "true" if self.chk_unlimited.isChecked() else "false")

    def _selected_stems(self) -> list[str]:
        return [name for name, chk in self.stem_checks.items() if chk.isChecked()]

    def _on_options_changed(self, *args):
        self.output.quality_hint.setText(QUALITY_HINTS.get(self.quality.currentData(), ""))
        self._update_ready_panel()

    # -- file list ----------------------------------------------------------------------
    def add_files(self, paths):
        existing = {self.list.item(i).data(self.ROLE_PATH) for i in range(self.list.count())}
        for p in paths:
            p = str(Path(p).resolve())
            if p in existing:
                continue
            self.list.add_entry(p)
            existing.add(p)
        if self.list.count():
            self.engine.prewarm(self.quality.currentData(), self.reserve.currentData(),
                                self.chk_unlimited.isChecked())

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

    def _pick_folder(self):
        """Like dropping a folder: every supported file inside it (recursively) is added."""
        start = self.settings.value("last_input_dir", str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder of songs", start)
        if not folder:
            return
        self.settings.setValue("last_input_dir", folder)
        self.sidebar.select(self.PAGE_SPLIT)
        paths = scan_paths([folder])
        if not paths:
            QMessageBox.information(self, APP_NAME, "No supported audio files were found in that folder.")
            return
        self.add_files(paths)

    def _remove_selected(self):
        if self._busy():
            return
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def _remove_item(self, item):
        if not self._busy():
            self.list.takeItem(self.list.row(item))

    def _clear(self):
        if not self._busy():
            self.list.clear()

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(self, "Save stems to", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _open_output(self):
        p = Path(self.out_edit.text()).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        open_folder(p)

    def _on_queue_changed(self, *args):
        self._panel_ready = True
        self._refresh_summary()

    def _refresh_summary(self):
        states = [self.list.item(i).data(self.ROLE_STATE) for i in range(self.list.count())]
        self.status_bar.set_summary(len(states), states.count("done"), states.count("running"), states.count("failed"))
        self._update_ready_panel()

    def _update_ready_panel(self):
        """While idle (and no result to show) the panel sums up what Split Stems will do."""
        if self._busy() or not self._panel_ready:
            return
        waiting = sum(1 for i in range(self.list.count())
                      if self.list.item(i).data(self.ROLE_STATE) in ("queued", "failed"))
        if waiting:
            n = len(self._selected_stems())
            detail = f"{waiting} song{'s' if waiting != 1 else ''} to split · {n} file{'s' if n != 1 else ''} " \
                     f"per song · {self.quality.currentText()} · {self.fmt.currentText()}"
        else:
            detail = "Add songs, choose the output settings and press Split Stems."
        self.panel.set_message("Ready to split", detail)

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
            QMessageBox.information(self, APP_NAME, "Add one or more songs first (drag & drop or “Add Files”).")
            return
        if not self._selected_stems():
            QMessageBox.information(self, APP_NAME, "Tick at least one stem to extract in Output Settings.")
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
            stems=tuple(self._selected_stems()),
            output_format=fmt,
            memory_reserve_mb=self.reserve.currentData(),
            ignore_memory_limit=self.chk_unlimited.isChecked(),
        )

        for row, _ in jobs:
            self._set_item(row, "queued", "queued")
        self._timing.clear()
        self._current_row = None
        self._set_running(True)
        self.panel.show_song(jobs[0][1].name, "Starting…", 0.0, "")
        self._append_log(f"Starting {len(jobs)} file(s) → {out}")
        self.engine.run(jobs, opts)

    def _cancel(self):
        self.engine.cancel()
        self.panel.set_cancelling()

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        for w in (self.btn_add, self.btn_add_folder, self.batch_page.btn_folder, self.btn_remove, self.btn_clear,
                  self.quality, self.fmt, *self.stem_checks.values(), self.out_edit, self.chk_unlimited):
            w.setEnabled(not running)
        self.reserve.setEnabled(not running and not self.chk_unlimited.isChecked())
        self.list.set_locked(running)
        self.panel.set_running(running)

    def _set_item(self, row: int, state: str, kind: str, detail: str = "", fraction: float = 0.0):
        """`state` is the queue's (ROLE_STATE); `kind` is what the row shows (a cancelled song is "queued")."""
        item = self.list.item(row)
        if item is None:
            return
        item.setData(self.ROLE_STATE, state)
        widget = self.list.itemWidget(item)
        if widget is not None:
            widget.set_state(kind, detail, fraction)
        self._refresh_summary()

    def _time_left(self, row: int, frac: float, text: str) -> str:
        """Estimated from this song's own progress rate; "" until there is enough to go on."""
        now = time.monotonic()
        if row not in self._timing or text.startswith(self.UNTIMED):
            self._timing[row] = (now, frac)
            return ""
        t0, f0 = self._timing[row]
        elapsed, progressed = now - t0, frac - f0
        if elapsed < 5 or progressed < 0.02:
            return ""
        return format_duration(elapsed * (1 - frac) / progressed)

    @Slot(int, float, str)
    def _on_status(self, row: int, frac: float, text: str):
        item = self.list.item(row)
        name = Path(item.data(self.ROLE_PATH)).name if item else ""
        left = self._time_left(row, frac, text)
        self._set_item(row, "running", "running", f"{frac:.0%}" + (f" · {left} remaining" if left else ""), frac)
        # while song N is written, song N+1 is already separated: the panel follows the newest one
        if self._current_row is None or row >= self._current_row:
            self._current_row = row
            eta = f"{left} remaining" if left else ("" if text.startswith(self.UNTIMED) else "Estimating time…")
            self.panel.show_song(name, text, frac, eta)
            if not self.btn_cancel.isEnabled():
                self.panel.set_cancelling()

    @Slot(str)
    def _on_device(self, device: str):
        self.status_bar.set_device(device)

    @Slot(dict)
    def _on_memory(self, snap: dict):
        gb = 1024**3
        if snap.get("unlimited"):
            text = f"Memory: {snap['used'] / gb:.1f} GB · no limit"
        else:
            text = f"Memory: {snap['used'] / gb:.1f} GB · limit {snap['budget'] / gb:.1f} GB"
        if snap.get("waiting"):
            text += " · waiting for free memory"
        self.status_bar.set_memory(text)

    def _on_worker_stopped(self):
        self.status_bar.set_memory("")
        memlog("worker process stopped (GUI process)", self._append_log)

    @Slot(int, dict, float)
    def _on_done(self, row: int, stems: dict, seconds: float):
        mins, secs = divmod(int(seconds), 60)
        self._set_item(row, "done", "done", f"done in {mins}m {secs:02d}s", 1.0)
        self._append_log("Saved:\n  " + "\n  ".join(stems.values()))

    @Slot(int, str)
    def _on_failed(self, row: int, message: str):
        if message == "Cancelled":
            self._set_item(row, "queued", "cancelled", "resumes where it stopped")
            return
        self._set_item(row, "failed", "failed", message.splitlines()[0][:120])
        self._append_log(f"ERROR: {message}")

    @Slot(bool)
    def _on_finished(self, cancelled: bool):
        self._set_running(False)
        self._current_row = None
        self._panel_ready = False
        done = sum(1 for i in range(self.list.count()) if self.list.item(i).data(self.ROLE_STATE) == "done")
        failed = sum(1 for i in range(self.list.count()) if self.list.item(i).data(self.ROLE_STATE) == "failed")
        if cancelled:
            self.panel.set_message("Cancelled", "Press Split Stems to continue: each song resumes where it stopped.", 0)
        elif failed:
            self.panel.set_message(f"Finished with {failed} error(s) — see the log",
                                   "Press Split Stems to retry only the songs that failed.")
            if not self.btn_log.isChecked():
                self.btn_log.setChecked(True)
        else:
            self.panel.set_message(f"All done — {done} song(s) split", f"Saved to {self.out_edit.text()}", 1000)

    @Slot(str)
    def _append_log(self, text: str):
        self.log.appendPlainText(text.rstrip())

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
def run_gui() -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(APP_NAME)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    try:  # dark title bar on Windows 11 and dark native dialogs on macOS (Qt >= 6.8)
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except AttributeError:
        pass
    pick_font_family()
    app.setPalette(dark_palette())
    font = QFont(Type.family)
    font.setPixelSize(Type.BODY)
    app.setFont(font)
    icon = resource_path("assets/icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyleSheet(stylesheet())
    win = MainWindow()
    win.center_on_screen()  # after the whole UI is built, so the size is final
    win.show()
    return app.exec()
