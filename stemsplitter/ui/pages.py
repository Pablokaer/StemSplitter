"""The secondary pages of the sidebar: Batch, Settings and About."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .icons import pixmap
from .theme import Colors, Sizes, Spacing
from .widgets import button, combo, icon_label, label, page_header


def fill_page(area: QScrollArea, *widgets: QWidget) -> None:
    """Stack `widgets` in `area` with the standard page margins; the page scrolls when the window is short."""
    content = QWidget()
    content.setObjectName("scrollContent")
    lay = QVBoxLayout(content)
    lay.setContentsMargins(Spacing.XXL, Spacing.XXL - 4, Spacing.XXL, Spacing.XL)
    lay.setSpacing(Spacing.XL)
    for w in widgets:
        lay.addWidget(w)
    lay.addStretch(1)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setWidget(content)


def _card(title: str, icon_name: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(Spacing.XL, Spacing.LG + 4, Spacing.XL, Spacing.XL)
    lay.setSpacing(Spacing.MD)
    head = QHBoxLayout()
    head.setSpacing(Spacing.SM + 2)
    head.addWidget(icon_label(icon_name, Colors.PURPLE_LIGHT, 20))
    head.addWidget(label(title, "sectionTitle"))
    head.addStretch(1)
    lay.addLayout(head)
    return f, lay


def _fact(icon_name: str, title: str, text: str) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, Spacing.XS, 0, Spacing.XS)
    lay.setSpacing(Spacing.MD + 2)
    ic = QLabel()
    ic.setPixmap(pixmap(icon_name, Colors.PURPLE_LIGHT, 20))
    ic.setFixedSize(20, 20)
    lay.addWidget(ic, 0, Qt.AlignTop)
    col = QVBoxLayout()
    col.setSpacing(2)
    col.addWidget(label(title, "fileName"))
    col.addWidget(label(text, "body", wrap=True))
    lay.addLayout(col, 1)
    return w


class BatchPage(QScrollArea):
    """How the queue handles many songs; its buttons lead back to the Split page."""

    def __init__(self):
        super().__init__()
        card, lay = _card("Working with many songs", "layers")
        for args in [
            ("folder", "Add a whole folder",
             "Every supported song inside it, including its subfolders, is added to the queue. Songs already "
             "in the queue are skipped."),
            ("waveform", "One queue, one model load",
             "The songs are split one after another with the models loaded once. The next song is already being "
             "separated while the previous one is written to disk."),
            ("alert", "A broken file doesn't stop the batch",
             "It is marked as failed and the queue keeps going. Press Split Stems again to retry only the songs "
             "that failed or were cancelled."),
            ("check", "Nothing is lost",
             "Every song keeps a checkpoint, so a cancelled or crashed split continues where it stopped."),
        ]:
            lay.addWidget(_fact(*args))
        lay.addSpacing(Spacing.SM)
        row = QHBoxLayout()
        row.setSpacing(Spacing.MD)
        self.btn_folder = button("Add Folder", "folder", "primary", Sizes.BUTTON, "#FFFFFF")
        self.btn_split = button("Go to Split", "waveform", None, Sizes.BUTTON)
        row.addWidget(self.btn_folder)
        row.addWidget(self.btn_split)
        row.addStretch(1)
        lay.addLayout(row)
        fill_page(self, page_header("Batch", "Split a whole folder or a long list of songs in one go."), card)


class SettingsPage(QScrollArea):
    """App-wide settings. The per-split options (folder, quality, format) are on the Split page."""

    def __init__(self, reserves):
        super().__init__()
        card, lay = _card("Memory", "memory")
        lay.addSpacing(Spacing.XS)
        lay.addWidget(label("Keep free for the system", "fieldLabel"))
        self.reserve = combo()
        for text, mb in reserves:
            self.reserve.addItem(text, mb)
        self.reserve.setMaximumWidth(420)
        lay.addWidget(self.reserve)
        lay.addWidget(label(
            "StemSplitter never uses this much of the free memory, so the computer stays responsive. When memory "
            "runs short it slows down or waits, and never loses work. Automatic keeps 2 GB free, or a quarter of "
            "the RAM on machines with less than 8 GB.", "hint", wrap=True))
        note = label("The output folder, quality and format are set on the Split page and are remembered "
                     "between sessions.", "muted", wrap=True)
        fill_page(self, page_header("Settings", "Preferences that apply to every split."), card, note)


class AboutPage(QScrollArea):
    def __init__(self, app_name: str, version: str):
        super().__init__()
        models, ml = _card("Models", "waveform")
        ml.addWidget(_fact("check", "BS-RoFormer SW (jarredou)", "Splits all the stems in one pass."))
        ml.addWidget(_fact("check", "MelBand-RoFormer (Kimberley Jensen)",
                           "A second vocal model, used by the Maximum preset for the cleanest vocals."))
        ml.addWidget(label("Models are downloaded once on first use (~0.7 GB, +0.9 GB for Maximum).",
                           "hint", wrap=True))
        credits, cl = _card("Built with", "info")
        cl.addWidget(label("python-audio-separator (MIT), PyTorch (BSD), PySide6 / Qt (LGPL-3) and FFmpeg (GPL). "
                           "Check each model's license before any commercial use.", "body", wrap=True))
        fill_page(self, page_header(f"About {app_name}", f"Version {version}"), models, credits)
