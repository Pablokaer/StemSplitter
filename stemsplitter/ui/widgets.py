"""The building blocks of the window: sidebar, drop zone, file queue, settings card, processing panel
and status bar. They only show state and emit signals; `stemsplitter.gui` owns the logic.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, Signal)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .icons import icon, logo, pixmap
from .theme import Colors, Sizes, Spacing

SUPPORTED = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".wma"}


def scan_paths(paths) -> list[Path]:
    """Supported audio files among `paths`; folders are scanned recursively."""
    found = []
    for p in map(Path, paths):
        if p.is_dir():
            found += sorted(x for x in p.rglob("*") if x.suffix.lower() in SUPPORTED)
        elif p.suffix.lower() in SUPPORTED:
            found.append(p)
    return found


def _dropped_paths(event) -> list[Path]:
    return scan_paths(url.toLocalFile() for url in event.mimeData().urls())


# -- small helpers ----------------------------------------------------------------------
def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def button(text: str, icon_name: str | None = None, variant: str | None = None, height: int = Sizes.BUTTON,
           icon_color: str = Colors.TEXT) -> QPushButton:
    b = QPushButton(f" {text}" if icon_name and text else text)  # a little air between icon and text
    if variant:
        b.setProperty("variant", variant)
    if icon_name:
        b.setIcon(icon(icon_name, icon_color, 18, disabled_color=Colors.TEXT_DISABLED))
        b.setIconSize(QSize(18, 18))
    b.setFixedHeight(height)
    b.setCursor(Qt.PointingHandCursor)
    return b


def combo() -> QComboBox:
    c = QComboBox()
    c.setItemDelegate(QStyledItemDelegate(c))  # lets the style sheet style the popup's items
    c.setFixedHeight(Sizes.CONTROL)
    c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    c.setMinimumContentsLength(12)
    c.setCursor(Qt.PointingHandCursor)
    return c


def card(name: str = "card") -> QFrame:
    f = QFrame()
    f.setObjectName(name)
    return f


def icon_label(icon_name: str, color: str, size: int = 18) -> QLabel:
    w = QLabel()
    w.setPixmap(pixmap(icon_name, color, size))
    w.setFixedSize(size, size)
    return w


def repolish(w: QWidget) -> None:
    """Re-apply the style sheet after a dynamic property changed."""
    w.style().unpolish(w)
    w.style().polish(w)


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m {secs:02d}s" if mins else f"{secs}s"


def format_size(n: int) -> str:
    return f"{n / 1024**2:.1f} MB" if n >= 1024**2 else f"{max(1, n // 1024)} KB"


class ElidedLabel(QLabel):
    """A one-line label that shows "…" instead of growing: a long file name never widens the window."""

    def __init__(self, text: str = "", name: str | None = None, mode=Qt.ElideRight):
        super().__init__()
        if name:
            self.setObjectName(name)
        self._mode = mode
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text
        super().setText(text)
        self.update()

    def fullText(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self._full, self._mode, rect.width())
        self.style().drawItemText(painter, rect, int(self.alignment() | Qt.AlignVCenter), self.palette(),
                                  self.isEnabled(), text, self.foregroundRole())


# -- sidebar ----------------------------------------------------------------------------
class Sidebar(QFrame):
    page_changed = Signal(int)

    PAGES = [("Split", "waveform"), ("Batch", "layers"), ("Settings", "settings"), ("About", "info")]

    def __init__(self, version: str):
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(Sizes.SIDEBAR)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.LG, Spacing.XL + 4, Spacing.LG, Spacing.XL)
        lay.setSpacing(0)

        brand = QHBoxLayout()
        brand.setContentsMargins(Spacing.SM, 0, 0, 0)
        brand.setSpacing(Spacing.MD)
        mark = QLabel()
        mark.setPixmap(logo(44))
        mark.setFixedSize(44, 44)
        brand.addWidget(mark)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("StemSplitter", "appTitle"))
        titles.addWidget(label("Split any song into stems", "appTagline"))
        brand.addLayout(titles, 1)
        lay.addLayout(brand)
        lay.addSpacing(Spacing.XXL + 4)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, (text, icon_name) in enumerate(self.PAGES):
            b = QPushButton(f"  {text}")
            b.setObjectName("navButton")
            b.setCheckable(True)
            b.setFixedHeight(Sizes.BUTTON)
            b.setCursor(Qt.PointingHandCursor)
            b.setIcon(icon(icon_name, Colors.TEXT_MUTED, 20, checked_color=Colors.PURPLE_PALE))
            b.setIconSize(QSize(20, 20))
            self.group.addButton(b, i)
            lay.addWidget(b)
            lay.addSpacing(Spacing.XS)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(self.page_changed)
        lay.addStretch(1)
        footer = label(f"Version {version}", "sidebarFooter")
        footer.setContentsMargins(Spacing.SM, 0, 0, 0)
        lay.addWidget(footer)

    def select(self, index: int) -> None:
        self.group.button(index).setChecked(True)
        self.page_changed.emit(index)


# -- page header ------------------------------------------------------------------------
def page_header(title: str, subtitle: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(Spacing.SM)
    lay.addWidget(label(title, "pageTitle"))
    lay.addWidget(label(subtitle, "pageSubtitle", wrap=True))
    return w


# -- drop zone --------------------------------------------------------------------------
class DropZone(QFrame):
    """The big drag & drop target with the Add Files / Add Folder buttons."""

    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(Sizes.DROP_ZONE)
        self._hover = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        lay.setSpacing(0)
        lay.addStretch(1)
        self.icon = icon_label("upload", Colors.PURPLE_LIGHT, 40)
        lay.addWidget(self.icon, 0, Qt.AlignHCenter)
        lay.addSpacing(Spacing.MD)
        lay.addWidget(label("Drag & drop audio files here", "dropTitle"), 0, Qt.AlignHCenter)
        lay.addSpacing(Spacing.XS)
        lay.addWidget(label("Supports MP3, WAV, FLAC, M4A and more. Folders are scanned too.", "hint"),
                      0, Qt.AlignHCenter)
        lay.addSpacing(Spacing.LG + 4)
        row = QHBoxLayout()
        row.setSpacing(Spacing.MD)
        row.addStretch(1)
        self.btn_add = button("Add Files", "file", "primary", Sizes.BUTTON_PRIMARY, icon_color="#FFFFFF")
        self.btn_add.setMinimumWidth(200)
        self.btn_folder = button("Add Folder", "folder", None, Sizes.BUTTON_PRIMARY)
        self.btn_folder.setMinimumWidth(160)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_folder)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self.update()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_hover(True)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._set_hover(False)

    def dropEvent(self, e):
        self._set_hover(False)
        paths = _dropped_paths(e)
        if paths:
            self.files_dropped.emit(paths)
        e.acceptProposedAction()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 16, 16)
        p.fillPath(path, QColor("#16143A" if self._hover else Colors.BG_2))
        pen = QPen(QColor(Colors.PURPLE_PALE if self._hover else "#5A46C8"), 1.6 if self._hover else 1.3)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawPath(path)


# -- file queue -------------------------------------------------------------------------
class StatusIcon(QWidget):
    """The round state marker of a queue row: clock, progress ring, green check, red alert or stop."""

    def __init__(self, size: int = 30):
        super().__init__()
        self.setFixedSize(size, size)
        self.kind = "queued"
        self.fraction = 0.0

    def set_state(self, kind: str, fraction: float = 0.0) -> None:
        self.kind, self.fraction = kind, fraction
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self.width()
        r = QRectF(2, 2, s - 4, s - 4)
        if self.kind == "running":
            p.setPen(QPen(QColor(Colors.TRACK), 3))
            p.drawEllipse(r)
            pen = QPen(QColor(Colors.PURPLE_LIGHT), 3)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(r, 90 * 16, -int(360 * 16 * max(0.02, min(1.0, self.fraction))))
        elif self.kind == "done":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(Colors.GREEN))
            p.drawEllipse(r)
            p.drawPixmap(int(s * 0.25), int(s * 0.25), pixmap("check-bold", Colors.BG, int(s * 0.5)))
        elif self.kind == "failed":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#3A1A22"))
            p.drawEllipse(r)
            p.drawPixmap(int(s * 0.2), int(s * 0.2), pixmap("alert", Colors.RED, int(s * 0.6)))
        else:  # queued / cancelled
            name = "stop" if self.kind == "cancelled" else "clock"
            m = int(s * 0.12)
            p.drawPixmap(m, m, pixmap(name, Colors.TEXT_MUTED, s - 2 * m))


class FileRow(QFrame):
    """One song in the queue: checkbox, thumbnail, name and details, state, remove button."""

    check_toggled = Signal(bool)
    remove_clicked = Signal()

    STATE_TITLES = {"queued": "Queued", "running": "Processing…", "done": "Completed", "failed": "Failed",
                    "cancelled": "Cancelled"}

    def __init__(self, path: Path):
        super().__init__()
        self.setObjectName("fileRow")
        self.setProperty("selected", False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.SM, Spacing.SM)
        lay.setSpacing(Spacing.MD + 2)

        self.check = QCheckBox()
        self.check.setCursor(Qt.PointingHandCursor)
        self.check.setToolTip("Select")
        self.check.toggled.connect(self.check_toggled)
        lay.addWidget(self.check)

        thumb = card("thumb")
        thumb.setFixedSize(44, 44)
        tl = QVBoxLayout(thumb)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(icon_label("music", Colors.PURPLE_LIGHT, 20), 0, Qt.AlignCenter)
        lay.addWidget(thumb)

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addStretch(1)
        self.name = ElidedLabel(path.name, "fileName")
        text.addWidget(self.name)
        try:
            size = format_size(path.stat().st_size)
        except OSError:
            size = "?"
        meta = f"{path.suffix.lstrip('.').upper()} · {size} · {path.parent}"
        self.meta = ElidedLabel(meta, "muted", Qt.ElideMiddle)
        text.addWidget(self.meta)
        text.addStretch(1)
        lay.addLayout(text, 1)

        self.marker = StatusIcon(30)
        lay.addWidget(self.marker)
        state = QVBoxLayout()
        state.setSpacing(2)
        state.addStretch(1)
        self.state_text = label("Queued", "stateText")
        self.state_text.setProperty("state", "queued")
        self.detail = ElidedLabel("", "muted")
        state.addWidget(self.state_text)
        state.addWidget(self.detail)
        state.addStretch(1)
        holder = QWidget()
        holder.setLayout(state)
        holder.setFixedWidth(200)
        lay.addWidget(holder)

        self.btn_remove = QPushButton()
        self.btn_remove.setObjectName("rowRemove")
        self.btn_remove.setIcon(icon("close", Colors.TEXT_MUTED, 18, disabled_color="#2A3245"))
        self.btn_remove.setIconSize(QSize(18, 18))
        self.btn_remove.setFixedSize(34, 34)
        self.btn_remove.setToolTip("Remove from the list")
        self.btn_remove.setCursor(Qt.PointingHandCursor)
        self.btn_remove.clicked.connect(self.remove_clicked)
        lay.addWidget(self.btn_remove)
        self.setToolTip(str(path))

    def set_selected(self, on: bool) -> None:
        if self.property("selected") != on:
            self.setProperty("selected", on)
            repolish(self)
        if self.check.isChecked() != on:
            self.check.blockSignals(True)
            self.check.setChecked(on)
            self.check.blockSignals(False)

    def set_state(self, kind: str, detail: str = "", fraction: float = 0.0, title: str | None = None) -> None:
        """kind: queued | running | done | failed | cancelled."""
        self.marker.set_state(kind, fraction)
        self.state_text.setText(title or self.STATE_TITLES[kind])
        if self.state_text.property("state") != kind:
            self.state_text.setProperty("state", kind)
            repolish(self.state_text)
        self.detail.setText(detail)
        self.detail.setToolTip(detail if len(detail) > 30 else "")


class FileList(QListWidget):
    """The queue. Each item keeps the song's path and state (the rows are the worker's job ids);
    its row widget only shows them."""

    ROLE_PATH = Qt.UserRole
    ROLE_STATE = Qt.UserRole + 1  # "queued" | "running" | "done" | "failed"

    files_dropped = Signal(list)
    remove_requested = Signal(object)  # QListWidgetItem

    def __init__(self):
        super().__init__()
        self.setObjectName("fileList")
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setSpacing(2)
        self.setFrameShape(QFrame.NoFrame)
        self._locked = False
        self.itemSelectionChanged.connect(self._sync_selection)

    def add_entry(self, path: str) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(self.ROLE_PATH, path)
        item.setData(self.ROLE_STATE, "queued")
        item.setSizeHint(QSize(max(1, self.viewport().width() - 4), Sizes.ROW))
        self.addItem(item)
        row = FileRow(Path(path))
        row.btn_remove.setEnabled(not self._locked)
        row.check_toggled.connect(lambda on, it=item: it.setSelected(on))
        row.remove_clicked.connect(lambda it=item: self.remove_requested.emit(it))
        self.setItemWidget(item, row)
        return item

    def row_widget(self, row: int) -> FileRow | None:
        item = self.item(row)
        return self.itemWidget(item) if item is not None else None

    def set_locked(self, locked: bool) -> None:
        """While a batch runs rows can't be removed (their indices are the running jobs' ids)."""
        self._locked = locked
        for i in range(self.count()):
            if (w := self.row_widget(i)) is not None:
                w.btn_remove.setEnabled(not locked)

    def _sync_selection(self) -> None:
        for i in range(self.count()):  # a row being removed may already have lost its widget
            if (w := self.row_widget(i)) is not None:
                w.set_selected(self.item(i).isSelected())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        width = max(1, self.viewport().width() - 4)
        for i in range(self.count()):
            self.item(i).setSizeHint(QSize(width, Sizes.ROW))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = _dropped_paths(e)
        if paths:
            self.files_dropped.emit(paths)
        e.acceptProposedAction()


class FileQueue(QFrame):
    """The "Files to process" card: header with the count and actions, the list, or an empty state."""

    VISIBLE_ROWS = 6  # the list scrolls beyond this

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.XL, Spacing.LG + 4, Spacing.XL, Spacing.LG + 4)
        lay.setSpacing(Spacing.MD)

        head = QHBoxLayout()
        head.setSpacing(Spacing.SM)
        head.addWidget(label("Files to process", "sectionTitle"))
        self.count_label = label("(0)", "sectionCount")
        head.addWidget(self.count_label)
        head.addStretch(1)
        self.btn_remove = button("Remove selected", "trash", "ghost", Sizes.BUTTON_SMALL, Colors.TEXT_2)
        self.btn_clear = button("Clear list", "close", "ghost", Sizes.BUTTON_SMALL, Colors.TEXT_2)
        head.addWidget(self.btn_remove)
        head.addWidget(self.btn_clear)
        lay.addLayout(head)

        self.stack = QStackedWidget()
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.setContentsMargins(0, Spacing.LG, 0, Spacing.LG)
        el.setSpacing(Spacing.XS)
        el.addWidget(icon_label("music", Colors.TEXT_MUTED, 26), 0, Qt.AlignHCenter)
        el.addSpacing(Spacing.SM)
        el.addWidget(label("No files added yet", "emptyTitle"), 0, Qt.AlignHCenter)
        el.addWidget(label("Add an audio file to start separating stems.", "hint"), 0, Qt.AlignHCenter)
        self.stack.addWidget(empty)
        self.list = FileList()
        self.stack.addWidget(self.list)
        lay.addWidget(self.stack)

        model = self.list.model()
        for sig in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            sig.connect(self._update)
        self._update()

    def _update(self, *args) -> None:
        n = self.list.count()
        self.count_label.setText(f"({n})")
        self.stack.setCurrentIndex(1 if n else 0)
        rows = min(max(n, 1), self.VISIBLE_ROWS)
        step = Sizes.ROW + 2 * self.list.spacing()
        self.list.setFixedHeight(rows * step + 2)
        self.stack.setFixedHeight(self.list.height() if n else 150)
        has = n > 0
        self.btn_remove.setVisible(has)
        self.btn_clear.setVisible(has)


# -- output settings --------------------------------------------------------------------
class OutputSettings(QFrame):
    """Output folder, quality, format and the two extra outputs; two columns when there is room."""

    GAP = Spacing.XL

    def __init__(self, qualities, formats):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.XL, Spacing.LG + 4, Spacing.XL, Spacing.XL)
        lay.setSpacing(Spacing.LG + 4)

        head = QHBoxLayout()
        head.setSpacing(Spacing.SM + 2)
        head.addWidget(icon_label("settings", Colors.PURPLE_LIGHT, 20))
        head.addWidget(label("Output Settings", "sectionTitle"))
        head.addStretch(1)
        lay.addLayout(head)

        # left column
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        ll.addWidget(label("Save stems to", "fieldLabel"))
        ll.addSpacing(Spacing.SM)
        row = QHBoxLayout()
        row.setSpacing(Spacing.SM)
        self.out_edit = QLineEdit()
        self.out_edit.setFixedHeight(Sizes.CONTROL)
        self.out_edit.setMinimumWidth(120)
        row.addWidget(self.out_edit, 1)
        self.btn_browse = button("Browse…", "folder", None, Sizes.CONTROL)
        row.addWidget(self.btn_browse)
        ll.addLayout(row)
        ll.addSpacing(Spacing.LG + 4)
        ll.addWidget(label("Quality", "fieldLabel"))
        ll.addSpacing(Spacing.SM)
        self.quality = combo()
        for text, key in qualities:
            self.quality.addItem(text, key)
        ll.addWidget(self.quality)
        ll.addSpacing(Spacing.SM)
        self.quality_hint = label("", "hint", wrap=True)
        ll.addWidget(self.quality_hint)
        ll.addStretch(1)

        # right column
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(label("Output format", "fieldLabel"))
        rl.addSpacing(Spacing.SM)
        self.fmt = combo()
        for text, fmt, br in formats:
            self.fmt.addItem(text, (fmt, br))
        rl.addWidget(self.fmt)
        rl.addSpacing(Spacing.SM)
        rl.addWidget(label("One file per stem, in a subfolder for each song.", "hint", wrap=True))
        rl.addSpacing(Spacing.LG + 4)
        self.chk_inst = QCheckBox("Also save an instrumental track (everything except vocals)")
        self.chk_gp = QCheckBox("Also split Guitar and Piano out of Other (6 stems)")
        for chk in (self.chk_inst, self.chk_gp):
            chk.setCursor(Qt.PointingHandCursor)
            chk.setMinimumHeight(28)
        rl.addWidget(self.chk_inst)
        rl.addSpacing(Spacing.MD)
        rl.addWidget(self.chk_gp)
        rl.addStretch(1)

        self.divider = QFrame()
        self.divider.setObjectName("vDivider")
        self.divider.setFixedWidth(1)
        self.columns = QBoxLayout(QBoxLayout.LeftToRight)
        self.columns.setSpacing(self.GAP)
        self.columns.addWidget(left, 1)
        self.columns.addWidget(self.divider)
        self.columns.addWidget(right, 1)
        lay.addLayout(self.columns)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        inner = self.contentsRect().width() - 2 * Spacing.XL
        need = max(self.chk_inst.sizeHint().width(), self.chk_gp.sizeHint().width(), 340)
        two = (inner - 2 * self.GAP - 1) / 2 >= need
        direction = QBoxLayout.LeftToRight if two else QBoxLayout.TopToBottom
        if self.columns.direction() != direction:
            self.columns.setDirection(direction)
            self.divider.setVisible(two)


# -- processing panel -------------------------------------------------------------------
class ProcessingPanel(QFrame):
    """The bottom card: what is running (name, stage, progress, time left) and the main actions."""

    COMPACT_WIDTH = 1000  # below this the log / folder buttons show only their icon

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(Spacing.LG + 4, Spacing.LG, Spacing.LG + 4, Spacing.LG)
        lay.setSpacing(Spacing.LG)

        tile = card("iconTile")
        tile.setFixedSize(52, 52)
        tl = QVBoxLayout(tile)
        tl.setContentsMargins(0, 0, 0, 0)
        self.tile_icon = QLabel()
        self.tile_icon.setFixedSize(24, 24)
        tl.addWidget(self.tile_icon, 0, Qt.AlignCenter)
        lay.addWidget(tile)

        mid = QVBoxLayout()
        mid.setSpacing(4)
        mid.addStretch(1)
        self.title = ElidedLabel("", "panelTitle")
        self.detail = ElidedLabel("", "secondary")
        mid.addWidget(self.title)
        mid.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(Sizes.PROGRESS)
        mid.addSpacing(6)
        mid.addWidget(self.progress)
        mid.addStretch(1)
        lay.addLayout(mid, 1)
        self._anim = QPropertyAnimation(self.progress, b"value", self)
        self._anim.setDuration(280)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        nums = QVBoxLayout()
        nums.setSpacing(0)
        nums.addStretch(1)
        self.percent = label("", "panelPercent")
        self.percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.eta = label("", "muted")
        self.eta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        nums.addWidget(self.percent)
        nums.addWidget(self.eta)
        nums.addStretch(1)
        self.nums = QWidget()
        self.nums.setLayout(nums)
        self.nums.setMinimumWidth(130)
        lay.addWidget(self.nums)

        self.btn_log = button("Show log", "log", None, Sizes.BUTTON_SMALL + 4, Colors.TEXT_2)
        self.btn_log.setCheckable(True)
        self.btn_log.setToolTip("Show or hide the log")
        self.btn_open = button("Open output folder", "folder-open", None, Sizes.BUTTON_SMALL + 4, Colors.TEXT_2)
        self.btn_open.setToolTip("Open the output folder")
        self.btn_cancel = button("Cancel", "stop", "danger", Sizes.BUTTON, Colors.RED)
        self.btn_cancel.setMinimumWidth(120)
        self.btn_start = button("Split Stems", "waveform", "primary", Sizes.BUTTON_PRIMARY, "#FFFFFF")
        self.btn_start.setMinimumWidth(170)
        for b in (self.btn_log, self.btn_open, self.btn_cancel, self.btn_start):
            lay.addWidget(b)
        self._labels = {self.btn_log: "Show log", self.btn_open: "Open output folder"}
        self.btn_log.toggled.connect(self._log_text)
        self.set_running(False)

    def _log_text(self, on: bool) -> None:
        self._labels[self.btn_log] = "Hide log" if on else "Show log"
        self._apply_compact()

    def _apply_compact(self) -> None:
        compact = self.width() < self.COMPACT_WIDTH
        for b, text in self._labels.items():
            b.setText("" if compact else f" {text}")
            b.setFixedWidth(Sizes.BUTTON_SMALL + 4 if compact else b.sizeHint().width())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_compact()

    def set_running(self, running: bool) -> None:
        """Running: Cancel replaces Split Stems and the numbers show; idle: the other way round."""
        self.btn_cancel.setVisible(running)
        self.btn_start.setVisible(not running)
        self.nums.setVisible(running)
        self.tile_icon.setPixmap(pixmap("music" if running else "waveform", Colors.PURPLE_LIGHT, 24))
        if running:
            self.btn_cancel.setEnabled(True)
            self.btn_cancel.setText(" Cancel")

    def set_message(self, title: str, detail: str, progress: int | None = None) -> None:
        """Idle / finished: a heading and a line of detail; `progress` (0..1000) shows the bar."""
        self.title.setText(title)
        self.detail.setText(detail)
        self.progress.setVisible(progress is not None)
        if progress is not None:
            self.set_progress(progress)

    def show_song(self, name: str, stage: str, fraction: float, eta: str) -> None:
        self.title.setText(name)
        self.detail.setText(stage)
        self.progress.setVisible(True)
        self.set_progress(int(fraction * 1000))
        self.percent.setText(f"{fraction:.0%}")
        self.eta.setText(eta)

    def set_cancelling(self) -> None:
        self.detail.setText("Cancelling after the current step…")
        self.btn_cancel.setText(" Cancelling…")
        self.btn_cancel.setEnabled(False)

    def set_progress(self, value: int) -> None:
        """Animated when it grows; a new song (lower value) jumps straight back."""
        self._anim.stop()
        if value <= self.progress.value():
            self.progress.setValue(value)
            return
        self._anim.setStartValue(self.progress.value())
        self._anim.setEndValue(value)
        self._anim.start()


# -- status bar -------------------------------------------------------------------------
class StatusBar(QFrame):
    """Device in use and memory on the left; the queue summary on the right."""

    def __init__(self):
        super().__init__()
        self.setObjectName("statusBar")
        self.setFixedHeight(40)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(Spacing.XXL, 0, Spacing.XXL, 0)
        lay.setSpacing(Spacing.SM)
        lay.addWidget(icon_label("chip", Colors.TEXT_MUTED, 16))
        self.device = ElidedLabel("Device: detected when songs are added", "statusText")
        lay.addWidget(self.device, 3)
        self.memory_icon = icon_label("memory", Colors.TEXT_MUTED, 16)
        lay.addWidget(self.memory_icon)
        self.memory = ElidedLabel("", "statusText")
        lay.addWidget(self.memory, 2)
        self.memory_icon.setVisible(False)
        self.summary = QHBoxLayout()
        self.summary.setSpacing(Spacing.MD)
        lay.addLayout(self.summary)
        self._items: list[QWidget] = []
        self._counts = None

    def set_device(self, text: str) -> None:
        self.device.setText(f"Processing on: {text}")
        self.device.setToolTip(text)

    def set_memory(self, text: str) -> None:
        self.memory.setText(text)
        self.memory_icon.setVisible(bool(text))

    def set_summary(self, total: int, done: int, running: int, failed: int) -> None:
        if self._counts == (total, done, running, failed):
            return  # called on every progress update
        self._counts = (total, done, running, failed)
        for w in self._items:
            self.summary.removeWidget(w)
            w.hide()  # deleteLater alone leaves the old text on screen until the next event loop pass
            w.deleteLater()
        self._items = []
        parts = [(f"{total} file{'s' if total != 1 else ''}", None)]
        if done:
            parts.append((f"{done} completed", Colors.GREEN))
        if running:
            parts.append((f"{running} processing", Colors.PURPLE_LIGHT))
        if failed:
            parts.append((f"{failed} failed", Colors.RED))
        for i, (text, dot) in enumerate(parts):
            if i:
                self._add(label("|", "statusText"))
            if dot:
                self._add(_Dot(dot))
            self._add(label(text, "statusStrong"))

    def _add(self, w: QWidget) -> None:
        self.summary.addWidget(w)
        self._items.append(w)


class _Dot(QWidget):
    def __init__(self, color: str):
        super().__init__()
        self.color = color
        self.setFixedSize(8, 8)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.color))
        p.drawEllipse(self.rect())
