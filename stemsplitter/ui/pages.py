"""The secondary pages of the sidebar: Batch, Settings and About."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from ..i18n import N_, tr
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
    """A card with an icon and a title (`title` is translated here: pass it marked with N_)."""
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(Spacing.XL, Spacing.LG + 4, Spacing.XL, Spacing.XL)
    lay.setSpacing(Spacing.MD)
    head = QHBoxLayout()
    head.setSpacing(Spacing.SM + 2)
    head.addWidget(icon_label(icon_name, Colors.PURPLE_LIGHT, 20))
    head.addWidget(label(tr(title), "sectionTitle"))
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
    col.addWidget(label(tr(title), "fileName"))
    col.addWidget(label(tr(text), "body", wrap=True))
    lay.addLayout(col, 1)
    return w


class BatchPage(QScrollArea):
    """How the queue handles many songs; its buttons lead back to the Split page."""

    def __init__(self):
        super().__init__()
        card, lay = _card(N_("Working with many songs"), "layers")
        for args in [
            ("folder", N_("Add a whole folder"),
             N_("Every supported song inside it, including its subfolders, is added to the queue. Songs already "
                "in the queue are skipped.")),
            ("waveform", N_("One queue, one model load"),
             N_("The songs are split one after another with the models loaded once. The next song is already "
                "being separated while the previous one is written to disk.")),
            ("alert", N_("A broken file doesn't stop the batch"),
             N_("It is marked as failed and the queue keeps going. Press Split Stems again to retry only the songs "
                "that failed or were cancelled.")),
            ("check", N_("Nothing is lost"),
             N_("Every song keeps a checkpoint, so a cancelled or crashed split continues where it stopped.")),
        ]:
            lay.addWidget(_fact(*args))
        lay.addSpacing(Spacing.SM)
        row = QHBoxLayout()
        row.setSpacing(Spacing.MD)
        self.btn_folder = button(tr("Add Folder"), "folder", "primary", Sizes.BUTTON, "#FFFFFF")
        self.btn_split = button(tr("Go to Split"), "waveform", None, Sizes.BUTTON)
        row.addWidget(self.btn_folder)
        row.addWidget(self.btn_split)
        row.addStretch(1)
        lay.addLayout(row)
        fill_page(self, page_header(tr("Batch"), tr("Split a whole folder or a long list of songs in one go.")), card)


class SettingsPage(QScrollArea):
    """App-wide settings. The per-split options (folder, quality, format) are on the Split page."""

    def __init__(self, reserves, version: str, languages: dict[str, str]):
        super().__init__()
        card, lay = _card(N_("Memory"), "memory")
        lay.addSpacing(Spacing.XS)
        lay.addWidget(label(tr("Keep free for the system"), "fieldLabel"))
        self.reserve = combo()
        for text, mb in reserves:
            self.reserve.addItem(tr(text), mb)
        self.reserve.setMaximumWidth(420)
        lay.addWidget(self.reserve)
        lay.addWidget(label(tr(
            "StemSplitter never uses this much of the free memory, so the computer stays responsive. When memory "
            "runs short it slows down or waits, and never loses work. Automatic keeps 2 GB free, or a quarter of "
            "the RAM on machines with less than 8 GB."), "hint", wrap=True))
        lay.addSpacing(Spacing.SM)
        self.chk_unlimited = QCheckBox(tr("Ignore the memory limit"))
        self.chk_unlimited.toggled.connect(lambda on: self.reserve.setEnabled(not on))
        lay.addWidget(self.chk_unlimited)
        lay.addWidget(label(tr(
            "Uses all the memory it wants and never waits for free memory: the fastest option, but the computer "
            "can slow down or swap, and the system may end the app if it runs out of memory (the split then "
            "resumes from its checkpoint). Use it only when nothing else important is running."), "hint", wrap=True))
        lang, gl = _card(N_("Language"), "globe")
        gl.addSpacing(Spacing.XS)
        gl.addWidget(label(tr("Language of the app"), "fieldLabel"))
        self.language = combo()
        self.language.addItem(tr("System default"), "")
        for code, name in languages.items():
            self.language.addItem(name, code)
        self.language.setMaximumWidth(420)
        gl.addWidget(self.language)
        gl.addWidget(label(tr("StemSplitter restarts to show the new language."), "hint", wrap=True))
        updates, ul = _card(N_("Updates"), "refresh")
        ul.addWidget(label(tr("You have version {version}.", version=version), "body"))
        self.chk_updates = QCheckBox(tr("Check for updates when the app starts"))
        ul.addWidget(self.chk_updates)
        ul.addWidget(label(tr(
            "When a new version is published, StemSplitter shows what changed and asks before updating. It then "
            "downloads only the files that changed and restarts."), "hint", wrap=True))
        ul.addSpacing(Spacing.XS)
        row = QHBoxLayout()
        row.setSpacing(Spacing.MD)
        self.btn_check_updates = button(tr("Check for updates"), "refresh", None, Sizes.BUTTON_SMALL + 4)
        row.addWidget(self.btn_check_updates)
        self.update_status = label("", "hint", wrap=True)
        row.addWidget(self.update_status, 1)
        ul.addLayout(row)
        note = label(tr("The output folder, quality and format are set on the Split page and are remembered "
                        "between sessions."), "muted", wrap=True)
        fill_page(self, page_header(tr("Settings"), tr("Preferences that apply to every split.")), lang, card, updates,
                  note)

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)


class AboutPage(QScrollArea):
    def __init__(self, app_name: str, version: str):
        super().__init__()
        models, ml = _card(N_("Models"), "waveform")
        ml.addWidget(_fact("check", "BS-RoFormer SW (jarredou)", N_("Splits all the stems in one pass.")))
        ml.addWidget(_fact("check", "MelBand-RoFormer (Kimberley Jensen)",
                           N_("A second vocal model, used by the Maximum preset for the cleanest vocals.")))
        ml.addWidget(label(tr("Models are downloaded once on first use (~0.7 GB, +0.9 GB for Maximum)."),
                           "hint", wrap=True))
        credits, cl = _card(N_("Built with"), "info")
        cl.addWidget(label(tr("python-audio-separator (MIT), PyTorch (BSD), PySide6 / Qt (LGPL-3) and FFmpeg (GPL). "
                              "Check each model's license before any commercial use."), "body", wrap=True))
        fill_page(self, page_header(tr("About {app}", app=app_name), tr("Version {version}", version=version)), models,
                  credits)
