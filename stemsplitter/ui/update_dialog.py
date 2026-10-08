"""The update dialog (what's new, then the download) and the window of the helper that installs an update.

The logic is in `stemsplitter.updater`; this module only runs it on a thread and shows its progress.
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, DISPLAY_NAME, __version__
from .. import updater as U
from ..i18n import tr
from .theme import Sizes, Spacing
from .widgets import button, label


class Task(QObject):
    """Runs `fn(progress, cancel)` on a thread. Connect the signals with Qt.QueuedConnection to methods of a
    QObject that lives on the GUI thread, so they run there."""

    progress = Signal(float, str)
    done = Signal(object)
    failed = Signal(str)  # "" when cancelled

    def __init__(self, fn: Callable, parent: QObject | None = None):
        super().__init__(parent)
        self.cancel = threading.Event()
        self._fn = fn
        self.running = False

    def start(self) -> None:
        self.running = True
        threading.Thread(target=self._run, name=f"{APP_NAME} update", daemon=True).start()

    def _run(self) -> None:
        try:
            result = self._fn(self.progress.emit, self.cancel)
        except U.Cancelled:  # (its message is never shown)
            self.running = False
            self.failed.emit("")
        except U.UpdateError as exc:
            self.running = False
            self.failed.emit(str(exc))
        except Exception as exc:  # a bug or an unexpected OS error: show it instead of hanging
            traceback.print_exc()
            self.running = False
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.running = False
            self.done.emit(result)


def _center(widget: QWidget) -> None:
    screen = (widget.parentWidget().screen() if widget.parentWidget() else None) or QGuiApplication.primaryScreen()
    if screen is not None:
        frame = widget.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        widget.move(frame.topLeft())


class UpdateDialog(QDialog):
    """Offers a new version. "Update now" downloads only what changed, starts the installer and quits the app."""

    skip_requested = Signal(str)  # the version the user doesn't want to hear about again

    def __init__(self, release: U.Release, parent: QWidget, busy: Callable[[], bool], quit_app: Callable[[], None]):
        super().__init__(parent)
        self.release = release
        self._busy, self._quit_app = busy, quit_app
        self.inst = U.Installation.current()
        self.problem = U.self_update_problem(self.inst, release)
        self._task: Task | None = None
        self._closing = False
        self.setWindowTitle(tr("Update available"))
        self.setModal(True)
        self.setMinimumSize(560, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.LG)
        lay.setSpacing(Spacing.XS)
        lay.addWidget(label(tr("OctoSplitter {version} is available", version=release.version), "sectionTitle"))
        lay.addWidget(label(tr("You have version {version}.", version=__version__), "secondary"))
        self.pages = QStackedWidget()
        self.pages.addWidget(self._offer_page())
        self.pages.addWidget(self._progress_page())
        lay.addWidget(self.pages, 1)

    def _offer_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, Spacing.LG, 0, 0)
        lay.setSpacing(Spacing.SM)
        lay.addWidget(label(tr("What's new"), "fieldLabel"))
        notes = QTextBrowser()
        notes.setObjectName("releaseNotes")
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(self.release.notes.strip() or "_" + tr("No release notes.") + "_")
        lay.addWidget(notes, 1)
        if self.problem:
            info = f"{self.problem} " + tr("The download page opens in your browser.")
        else:
            info = tr("Only the files that changed are downloaded. OctoSplitter then closes, installs the update, "
                      "checks it and opens again; if the check fails, the current version is kept.")
        lay.addWidget(label(info, "hint", wrap=True))
        lay.addSpacing(Spacing.SM)
        row = QHBoxLayout()
        row.setSpacing(Spacing.SM)
        self.btn_skip = button(tr("Skip this version"), None, "ghost", Sizes.BUTTON_SMALL)
        self.btn_later = button(tr("Later"), None, None, Sizes.BUTTON_SMALL)
        self.btn_update = button(tr("Open download page") if self.problem else tr("Update now"), "download", "primary",
                                 Sizes.BUTTON_SMALL, "#FFFFFF")
        self.btn_update.setDefault(True)
        row.addWidget(self.btn_skip)
        row.addStretch(1)
        row.addWidget(self.btn_later)
        row.addWidget(self.btn_update)
        lay.addLayout(row)
        self.btn_skip.clicked.connect(self._skip)
        self.btn_later.clicked.connect(self.reject)
        self.btn_update.clicked.connect(self._update)
        return w

    def _progress_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, Spacing.LG, 0, 0)
        lay.setSpacing(Spacing.SM)
        lay.addStretch(1)
        self.status = label("", "fileName", wrap=True)
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(Sizes.PROGRESS)
        lay.addWidget(self.bar)
        self.detail = label("", "hint", wrap=True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.detail)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(Spacing.SM)
        row.addStretch(1)
        self.btn_page = button(tr("Open download page"), None, None, Sizes.BUTTON_SMALL)
        self.btn_page.setVisible(False)
        self.btn_page.clicked.connect(self._open_page)
        self.btn_cancel = button(tr("Cancel"), None, None, Sizes.BUTTON_SMALL)
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_page)
        row.addWidget(self.btn_cancel)
        lay.addLayout(row)
        return w

    def showEvent(self, event):
        super().showEvent(event)
        _center(self)

    # -- actions ------------------------------------------------------------------------
    def _skip(self) -> None:
        self.skip_requested.emit(self.release.version)
        self.reject()

    def _open_page(self) -> None:
        QDesktopServices.openUrl(QUrl(self.release.page))

    def _update(self) -> None:
        if self.problem:
            self._open_page()
            self.accept()
            return
        if self._busy():
            QMessageBox.information(self, DISPLAY_NAME, tr("Songs are being split. Update when the queue has finished "
                                                       "(or after cancelling it)."))
            return
        self.pages.setCurrentIndex(1)
        self.status.setText(tr("Preparing…"))
        self.detail.setText("")
        inst, release = self.inst, self.release

        def work(progress, cancel):
            plan = U.prepare(inst, release, progress, cancel)
            if not plan.empty:
                U.start_install(inst, plan, progress)  # the helper waits for this app to quit
            return plan

        self._task = Task(work, self)
        self._task.progress.connect(self._on_progress, Qt.QueuedConnection)
        self._task.done.connect(self._on_prepared, Qt.QueuedConnection)
        self._task.failed.connect(self._on_failed, Qt.QueuedConnection)
        self._task.start()

    def reject(self) -> None:
        if self._task is not None and self._task.running:  # stop the download first: its files stay consistent
            self._closing = True
            self._task.cancel.set()
            self.status.setText(tr("Cancelling…"))
            self.btn_cancel.setEnabled(False)
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None and self._task.running:
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)

    @Slot(float, str)
    def _on_progress(self, fraction: float, text: str) -> None:
        if not self._closing:
            self.status.setText(text)
        self.bar.setValue(int(fraction * 1000))

    @Slot(object)
    def _on_prepared(self, plan: U.Plan) -> None:
        if plan.empty:
            if self._closing:
                super().reject()
                return
            self.status.setText(tr("Nothing to download"))
            self.detail.setText(tr("The installed files already match version {version}.", version=plan.version))
            self.btn_cancel.setText(tr("Close"))
            return
        self.accept()
        self._quit_app()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        if self._closing:
            super().reject()
            return
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText(tr("Close"))
        if not message:
            self.status.setText(tr("Update cancelled"))
            self.detail.setText(tr("What was already downloaded is kept: the next try continues from there."))
            return
        self.status.setText(tr("The update failed"))
        self.detail.setText(f"{message}\n\n" + tr("Your current version is unchanged. You can try again later or "
                                                   "download the new version from the release page."))
        self.btn_page.setVisible(True)


class InstallWindow(QWidget):
    """The helper (`--apply-update`): a small window while the files are replaced and the new version is checked."""

    def __init__(self, work: Path):
        super().__init__()
        self.work = work
        self.plan = U._read_json(work / U.PLAN)
        self._done = False
        self.setWindowTitle(tr("Updating OctoSplitter"))
        self.setObjectName("root")
        self.setFixedSize(480, 170)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        lay.setSpacing(Spacing.SM)
        lay.addWidget(label(tr("Updating OctoSplitter to version {version}", version=self.plan.get("version", "?")),
                            "sectionTitle"))
        self.status = label(tr("Starting…"), "hint", wrap=True)
        lay.addWidget(self.status)
        bar = QProgressBar()
        bar.setRange(0, 0)  # busy: the steps can't be measured
        bar.setTextVisible(False)
        bar.setFixedHeight(Sizes.PROGRESS)
        lay.addWidget(bar)
        lay.addStretch(1)
        self._task = Task(lambda progress, cancel: U.install(work, lambda text: progress(0.0, text)), self)
        self._task.progress.connect(self._on_progress, Qt.QueuedConnection)
        self._task.done.connect(self._on_done, Qt.QueuedConnection)
        self._task.failed.connect(self._on_failed, Qt.QueuedConnection)

    def start(self) -> None:
        self.show()
        _center(self)
        self._task.start()

    def closeEvent(self, event):
        if not self._done:  # never leave the app half replaced
            event.ignore()
            return
        super().closeEvent(event)

    @Slot(float, str)
    def _on_progress(self, _fraction: float, text: str) -> None:
        self.status.setText(text)

    @Slot(object)
    def _on_done(self, result: dict) -> None:
        self._finish()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        QMessageBox.critical(self, DISPLAY_NAME, tr("The update could not be installed:") + f"\n\n{message}")
        self._finish()

    def _finish(self) -> None:
        self._done = True
        self.status.setText(tr("Starting OctoSplitter…"))
        try:
            U.launch(Path(self.plan["root"]), self.plan["exe"])
        except (OSError, KeyError) as exc:
            QMessageBox.critical(self, DISPLAY_NAME, tr("OctoSplitter could not be started again ({reason}). Start it "
                                                    "yourself.", reason=exc))
        QApplication.quit()


def run_update_helper(work: Path) -> int:
    """`StemSplitter --apply-update <work folder>`, started by the app from its hard-linked copy."""
    from ..gui import make_app

    app = make_app()
    win = InstallWindow(work)
    win.start()
    return app.exec()
