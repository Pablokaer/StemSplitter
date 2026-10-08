"""The building blocks of the window: sidebar, drop zone, file queue, settings card, processing panel
and status bar. They only show state and emit signals; `stemsplitter.gui` owns the logic.
"""

from __future__ import annotations

from pathlib import Path

import math

from PySide6.QtCore import (QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
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

from ..i18n import N_, tr, tr_n
from .icons import icon, logo, pixmap
from .theme import Colors, Radii, Sizes, Spacing

SUPPORTED = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".wma"}


def supported_formats() -> str:
    """"MP3, WAV, FLAC..." taken from SUPPORTED (.aif and .aiff are one format, listed once)."""
    names = {e.lstrip(".").upper() for e in SUPPORTED} - {"AIF"}
    first = ["MP3", "WAV", "FLAC", "M4A"]
    return ", ".join(first + sorted(names - set(first)))


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


def centered_label(text: str, name: str) -> QLabel:
    """A centered text that wraps, so a long translation adds a line instead of widening the window."""
    w = label(text, name, wrap=True)
    w.setAlignment(Qt.AlignHCenter)
    return w


class GlowButton(QPushButton):
    """A primary button: it carries a soft red glow while it is enabled."""

    def __init__(self, text: str):
        super().__init__(text)
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(28)
        self._glow.setOffset(0, 2)
        self._glow.setColor(QColor(255, 24, 56, 90))
        self.setGraphicsEffect(self._glow)

    def changeEvent(self, e):
        if e.type() == QEvent.EnabledChange:
            self._glow.setEnabled(self.isEnabled())
        super().changeEvent(e)


def button(text: str, icon_name: str | None = None, variant: str | None = None, height: int = Sizes.BUTTON,
           icon_color: str = Colors.TEXT) -> QPushButton:
    caption = f" {text}" if icon_name and text else text
    b = GlowButton(caption) if variant == "primary" else QPushButton(caption)  # a little air between icon and text
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


class Watermark(QWidget):
    """The logo drawn faintly, for empty states."""

    def __init__(self, size: int = 64, opacity: float = 0.3):
        super().__init__()
        self._pm = logo(size, opacity)
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        QPainter(self).drawPixmap(0, 0, self._pm)


class Backdrop(QWidget):
    """The window's background: near-black with a faint red glow in the top-right corner."""

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(Colors.BACKGROUND))
        glow = QRadialGradient(QPointF(self.width() * 0.92, -self.height() * 0.1), max(self.width(), self.height()) * 0.75)
        glow.setColorAt(0.0, QColor(255, 24, 56, 38))
        glow.setColorAt(0.65, QColor(255, 24, 56, 0))
        p.fillRect(self.rect(), glow)


def repolish(w: QWidget) -> None:
    """Re-apply the style sheet after a dynamic property changed."""
    w.style().unpolish(w)
    w.style().polish(w)


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return tr("{h}h {m:02d}m", h=hours, m=mins)
    return tr("{m}m {s:02d}s", m=mins, s=secs) if mins else tr("{s}s", s=secs)


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

    PAGES = [(N_("Split"), "waveform"), (N_("Batch"), "layers"), (N_("Settings"), "settings"), (N_("About"), "info")]

    def __init__(self, version: str):
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(Sizes.SIDEBAR)
        self._compact = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.LG, Spacing.XL, Spacing.LG, Spacing.XL)
        lay.setSpacing(0)

        self.brand = QHBoxLayout()
        self.brand.setContentsMargins(Spacing.XS, 0, 0, 0)
        self.brand.setSpacing(Spacing.MD)
        self.mark = QLabel()
        self.mark.setPixmap(logo(44))
        self.mark.setFixedSize(44, 44)
        self.brand.addWidget(self.mark)
        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(0)
        self.title = label(f'<span style="color:{Colors.PRIMARY}">Octo</span>Splitter', "appTitle")
        self.title.setTextFormat(Qt.RichText)
        self.tagline = label(tr("Split any song into stems"), "appTagline", wrap=True)  # long in some languages
        titles.addWidget(self.title)
        titles.addWidget(self.tagline)
        self.titles = QWidget()
        self.titles.setLayout(titles)
        self.brand.addWidget(self.titles, 1)
        lay.addLayout(self.brand)
        lay.addSpacing(Spacing.XXL)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._texts: list[str] = []
        for i, (text, icon_name) in enumerate(self.PAGES):
            b = QPushButton(f"  {tr(text)}")
            b.setObjectName("navButton")
            b.setCheckable(True)
            b.setFixedHeight(Sizes.BUTTON)
            b.setCursor(Qt.PointingHandCursor)
            b.setIcon(icon(icon_name, Colors.TEXT_MUTED, 20, checked_color=Colors.PRIMARY_LIGHT))
            b.setIconSize(QSize(20, 20))
            self._texts.append(f"  {tr(text)}")
            self.group.addButton(b, i)
            lay.addWidget(b)
            lay.addSpacing(Spacing.XS)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(self.page_changed)
        lay.addStretch(1)
        self.footer = label(tr("Version {version}", version=version), "sidebarFooter")
        self.footer.setContentsMargins(Spacing.SM, 0, 0, 0)
        lay.addWidget(self.footer)

    def select(self, index: int) -> None:
        self.group.button(index).setChecked(True)
        self.page_changed.emit(index)

    def set_compact(self, compact: bool) -> None:
        """Icons only: for narrow windows."""
        if compact == self._compact:
            return
        self._compact = compact
        self.setFixedWidth(Sizes.SIDEBAR_COMPACT if compact else Sizes.SIDEBAR)
        self.titles.setVisible(not compact)
        self.footer.setVisible(not compact)
        side = Spacing.MD if compact else Spacing.LG
        self.layout().setContentsMargins(side, Spacing.XL, side, Spacing.XL)
        self.brand.setContentsMargins(Spacing.SM if compact else Spacing.XS, 0, 0, 0)
        for i, b in enumerate(self.group.buttons()):
            b.setText("" if compact else self._texts[i])
            b.setToolTip(self._texts[i].strip() if compact else "")
            b.setProperty("compact", compact)
            repolish(b)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._compact:
            return
        p = QPainter(self)  # the octopus, almost invisible, rising from the bottom edge
        p.drawPixmap(self.width() - 200, self.height() - 190, logo(260, 0.07))


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
    """The big drag & drop target (click anywhere to browse) with the Add Files / Add Folder buttons."""

    files_dropped = Signal(list)
    browse_requested = Signal()

    HOVER_GLOW = 0.35  # how lit the zone is under the mouse; a dragged file lights it fully

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(Sizes.DROP_ZONE)
        self._glow = 0.0
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(180)
        self._fade.valueChanged.connect(self._set_glow)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.XL, Spacing.LG, Spacing.XL, Spacing.LG)
        lay.setSpacing(0)
        lay.addStretch(1)
        self.icon = icon_label("upload", Colors.PRIMARY, 44)
        lay.addWidget(self.icon, 0, Qt.AlignHCenter)
        lay.addSpacing(Spacing.SM)
        lay.addWidget(centered_label(tr("Drop your audio file here"), "dropTitle"))
        lay.addWidget(centered_label(tr("or click to browse"), "dropHint"))
        lay.addSpacing(Spacing.SM)
        chip = label(tr("{formats} · folders are scanned too", formats=supported_formats()), "formatChip")
        chip.setAlignment(Qt.AlignHCenter)
        lay.addWidget(chip, 0, Qt.AlignHCenter)
        lay.addSpacing(Spacing.MD)
        row = QHBoxLayout()
        row.setSpacing(Spacing.MD)
        row.addStretch(1)
        self.btn_add = button(tr("Add Files"), "file", "primary", Sizes.BUTTON, icon_color="#FFFFFF")
        self.btn_add.setMinimumWidth(176)
        self.btn_folder = button(tr("Add Folder"), "folder", None, Sizes.BUTTON)
        self.btn_folder.setMinimumWidth(152)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_folder)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def _set_glow(self, value) -> None:
        self._glow = float(value)
        self.update()

    def _glow_to(self, target: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._glow)
        self._fade.setEndValue(target)
        self._fade.start()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.browse_requested.emit()

    def enterEvent(self, e):
        self._glow_to(self.HOVER_GLOW)

    def leaveEvent(self, e):
        self._glow_to(0.0)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._glow_to(1.0)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._glow_to(0.0)

    def dropEvent(self, e):
        self._glow_to(0.0)
        paths = _dropped_paths(e)
        if paths:
            self.files_dropped.emit(paths)
        e.acceptProposedAction()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, Radii.DROP, Radii.DROP)
        p.fillPath(path, QColor(Colors.SURFACE))
        glow = QRadialGradient(rect.center(), rect.width() * 0.5)
        glow.setColorAt(0.0, QColor(255, 24, 56, round(20 + 55 * self._glow)))
        glow.setColorAt(1.0, QColor(255, 24, 56, 0))
        p.fillPath(path, glow)
        self._paint_bars(p, rect)
        pen = QPen(QColor(255, 24, 56, round(90 + 150 * self._glow)), 1.3 + 0.5 * self._glow)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawPath(path)

    def _paint_bars(self, p: QPainter, rect: QRectF) -> None:
        """Equalizer bars rising towards the middle from both sides, fading out: the drop zone's sound."""
        if rect.width() < 720:
            return
        heights = [0.22, 0.34, 0.5, 0.42, 0.66, 0.54, 0.78, 0.6, 0.9, 0.7]
        p.setPen(Qt.NoPen)
        step, bar = 14, 6
        for side in (-1, 1):
            for i, h in enumerate(heights):
                x = rect.center().x() + side * (rect.width() * 0.46 - i * step) - bar / 2
                if abs(x - rect.center().x()) < 270:
                    continue
                alpha = round((0.10 + 0.20 * (i / len(heights))) * 255 * (1 + 0.6 * self._glow))
                p.setBrush(QColor(255, 24, 56, min(255, alpha)))
                hh = h * rect.height() * 0.42
                p.drawRoundedRect(QRectF(x, rect.center().y() - hh / 2, bar, hh), 3, 3)


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
            pen = QPen(QColor(Colors.PRIMARY_LIGHT), 3)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(r, 90 * 16, -int(360 * 16 * max(0.02, min(1.0, self.fraction))))
        elif self.kind == "done":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(Colors.SUCCESS))
            p.drawEllipse(r)
            p.drawPixmap(int(s * 0.25), int(s * 0.25), pixmap("check-bold", Colors.BACKGROUND, int(s * 0.5)))
        elif self.kind == "failed":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 24, 56, 46))
            p.drawEllipse(r)
            p.drawPixmap(int(s * 0.2), int(s * 0.2), pixmap("alert", Colors.DANGER, int(s * 0.6)))
        else:  # queued / cancelled
            name = "stop" if self.kind == "cancelled" else "clock"
            m = int(s * 0.12)
            p.drawPixmap(m, m, pixmap(name, Colors.TEXT_MUTED, s - 2 * m))


class FileRow(QFrame):
    """One song in the queue: checkbox, thumbnail, name and details, state, remove button."""

    check_toggled = Signal(bool)
    remove_clicked = Signal()

    STATE_TITLES = {"queued": N_("Queued"), "running": N_("Processing…"), "done": N_("Completed"),
                    "failed": N_("Failed"), "cancelled": N_("Cancelled")}

    def __init__(self, path: Path):
        super().__init__()
        self.setObjectName("fileRow")
        self.setProperty("selected", False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.SM, Spacing.SM)
        lay.setSpacing(Spacing.MD + 2)

        self.check = QCheckBox()
        self.check.setCursor(Qt.PointingHandCursor)
        self.check.setToolTip(tr("Select"))
        self.check.toggled.connect(self.check_toggled)
        lay.addWidget(self.check)

        thumb = card("thumb")
        thumb.setFixedSize(44, 44)
        tl = QVBoxLayout(thumb)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(icon_label("music", Colors.PRIMARY_LIGHT, 20), 0, Qt.AlignCenter)
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
        self.state_text = label(tr("Queued"), "stateText")
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
        self.btn_remove.setIcon(icon("close", Colors.TEXT_MUTED, 18, disabled_color=Colors.TEXT_DISABLED))
        self.btn_remove.setIconSize(QSize(18, 18))
        self.btn_remove.setFixedSize(34, 34)
        self.btn_remove.setToolTip(tr("Remove from the list"))
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
        self.state_text.setText(title or tr(self.STATE_TITLES[kind]))
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
        head.addWidget(label(tr("Files to process"), "sectionTitle"))
        self.count_label = label("(0)", "sectionCount")
        head.addWidget(self.count_label)
        head.addStretch(1)
        self.btn_remove = button(tr("Remove selected"), "trash", "ghost", Sizes.BUTTON_SMALL, Colors.TEXT_2)
        self.btn_clear = button(tr("Clear list"), "close", "ghost", Sizes.BUTTON_SMALL, Colors.TEXT_2)
        head.addWidget(self.btn_remove)
        head.addWidget(self.btn_clear)
        lay.addLayout(head)

        self.stack = QStackedWidget()
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.setContentsMargins(0, Spacing.LG, 0, Spacing.LG)
        el.setSpacing(Spacing.XS)
        el.addWidget(Watermark(56, 0.28), 0, Qt.AlignHCenter)
        el.addSpacing(Spacing.SM)
        el.addWidget(centered_label(tr("No files added yet"), "emptyTitle"))
        el.addWidget(centered_label(tr("Add an audio file to start separating stems."), "hint"))
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
    """Output folder, quality, format and the stems to extract; two columns when there is room."""

    GAP = Spacing.XL

    def __init__(self, qualities, formats, stems):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.XL, Spacing.LG + 4, Spacing.XL, Spacing.XL)
        lay.setSpacing(Spacing.LG + 4)

        head = QHBoxLayout()
        head.setSpacing(Spacing.SM + 2)
        head.addWidget(icon_label("settings", Colors.PRIMARY_LIGHT, 20))
        head.addWidget(label(tr("Output Settings"), "sectionTitle"))
        head.addStretch(1)
        lay.addLayout(head)

        # left column
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        ll.addWidget(label(tr("Save stems to"), "fieldLabel"))
        ll.addSpacing(Spacing.SM)
        row = QHBoxLayout()
        row.setSpacing(Spacing.SM)
        self.out_edit = QLineEdit()
        self.out_edit.setFixedHeight(Sizes.CONTROL)
        self.out_edit.setMinimumWidth(120)
        row.addWidget(self.out_edit, 1)
        self.btn_browse = button(tr("Browse…"), "folder", None, Sizes.CONTROL)
        row.addWidget(self.btn_browse)
        ll.addLayout(row)
        ll.addSpacing(Spacing.LG + 4)
        ll.addWidget(label(tr("Quality"), "fieldLabel"))
        ll.addSpacing(Spacing.SM)
        self.quality = combo()
        for text, key in qualities:
            self.quality.addItem(tr(text), key)
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
        rl.addWidget(label(tr("Output format"), "fieldLabel"))
        rl.addSpacing(Spacing.SM)
        self.fmt = combo()
        for text, fmt, br in formats:
            self.fmt.addItem(tr(text), (fmt, br))
        rl.addWidget(self.fmt)
        rl.addSpacing(Spacing.SM)
        rl.addWidget(label(tr("One file per stem, in a subfolder for each song."), "hint", wrap=True))
        rl.addSpacing(Spacing.LG + 4)
        rl.addWidget(label(tr("Stems to extract"), "fieldLabel"))
        rl.addSpacing(Spacing.SM)
        self.stem_grid = QWidget()
        grid = QGridLayout(self.stem_grid)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(Spacing.LG)
        grid.setVerticalSpacing(Spacing.SM)
        self.stem_checks: dict[str, QCheckBox] = {}
        for i, (name, text, tip) in enumerate(stems):
            chk = QCheckBox(tr(text))
            chk.setToolTip(tr(tip))
            chk.setCursor(Qt.PointingHandCursor)
            chk.setMinimumHeight(28)
            grid.addWidget(chk, i // 2, i % 2)
            self.stem_checks[name] = chk
        rl.addWidget(self.stem_grid)
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
        need = max(self.stem_grid.sizeHint().width(), 340)
        two = (inner - 2 * self.GAP - 1) / 2 >= need
        direction = QBoxLayout.LeftToRight if two else QBoxLayout.TopToBottom
        if self.columns.direction() != direction:
            self.columns.setDirection(direction)
            self.divider.setVisible(two)


# -- processing animation -----------------------------------------------------------------
class SplitAnimation(QWidget):
    """While a song is split: one waveform fans out into four, and the four light up with the progress.
    Idle: the logo."""

    LANES = 4

    def __init__(self):
        super().__init__()
        self.setFixedSize(112, 52)
        self._running = False
        self._phase = 0.0
        self._fraction = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)

    def set_running(self, on: bool) -> None:
        self._running = on
        if on and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def set_fraction(self, fraction: float) -> None:
        self._fraction = fraction

    def showEvent(self, e):
        if self._running:
            self._timer.start()

    def hideEvent(self, e):
        self._timer.stop()

    def _advance(self) -> None:
        self._phase += 0.16
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        if not self._running:
            p.drawPixmap((self.width() - 48) // 2, (h - 48) // 2, logo(48))
            return
        # the source waveform on the left
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 24, 56, 230))
        for i in range(6):
            amp = 0.35 + 0.65 * abs(math.sin(self._phase * 1.3 + i * 0.9))
            p.drawRoundedRect(QRectF(2 + i * 6, h / 2 - amp * 17, 3.5, amp * 34), 1.7, 1.7)
        # the stems on the right, each lit a little more as the split advances
        lane_x = 74
        for lane in range(self.LANES):
            y = h * (lane + 0.5) / self.LANES
            lit = max(0.0, min(1.0, self._fraction * self.LANES - lane + 0.35))
            alpha = 0.30 + 0.70 * lit
            curve = QPainterPath(QPointF(40, h / 2))
            curve.cubicTo(QPointF(58, h / 2), QPointF(56, y), QPointF(lane_x - 4, y))
            p.setPen(QPen(QColor(255, 24, 56, round(255 * 0.45 * alpha)), 1.4))
            p.setBrush(Qt.NoBrush)
            p.drawPath(curve)
            dot = curve.pointAtPercent((self._phase * 0.35 + lane * 0.25) % 1.0)  # a pulse travelling the curve
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 74, 95, round(255 * alpha)))
            p.drawEllipse(dot, 1.8, 1.8)
            p.setBrush(QColor(255, 24, 56, round(255 * alpha)))
            for i in range(5):
                amp = 0.3 + 0.7 * abs(math.sin(self._phase * (1.0 + 0.2 * lane) + i * 1.1 + lane))
                p.drawRoundedRect(QRectF(lane_x + i * 7, y - amp * 4.5, 3, amp * 9), 1.5, 1.5)


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

        self.anim = SplitAnimation()
        lay.addWidget(self.anim)

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

        self.btn_log = button(tr("Show log"), "log", None, Sizes.BUTTON_SMALL + 4, Colors.TEXT_2)
        self.btn_log.setCheckable(True)
        self.btn_log.setToolTip(tr("Show or hide the log"))
        self.btn_open = button(tr("Open output folder"), "folder-open", None, Sizes.BUTTON_SMALL + 4, Colors.TEXT_2)
        self.btn_open.setToolTip(tr("Open the output folder"))
        self.btn_cancel = button(tr("Cancel"), "stop", "danger", Sizes.BUTTON, Colors.DANGER)
        self.btn_cancel.setMinimumWidth(120)
        self.btn_start = button(tr("Split Stems"), "waveform", "primary", Sizes.BUTTON_PRIMARY, "#FFFFFF")
        self.btn_start.setMinimumWidth(170)
        for b in (self.btn_log, self.btn_open, self.btn_cancel, self.btn_start):
            lay.addWidget(b)
        self._labels = {self.btn_log: tr("Show log"), self.btn_open: tr("Open output folder")}
        self.btn_log.toggled.connect(self._log_text)
        self.set_running(False)

    def _log_text(self, on: bool) -> None:
        self._labels[self.btn_log] = tr("Hide log") if on else tr("Show log")
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
        self.anim.set_running(running)
        if running:
            self.btn_cancel.setEnabled(True)
            self.btn_cancel.setText(f" {tr('Cancel')}")

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
        self.anim.set_fraction(fraction)
        self.percent.setText(f"{fraction:.0%}")
        self.eta.setText(eta)
        margins = self.nums.layout().contentsMargins()  # "Estimating time…" is longer in some languages
        self.nums.setMinimumWidth(max(130, self.eta.sizeHint().width() + margins.left() + margins.right()))

    def set_cancelling(self) -> None:
        self.detail.setText(tr("Cancelling after the current step…"))
        self.btn_cancel.setText(f" {tr('Cancelling…')}")
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
        self.device = ElidedLabel(tr("Device: detected when songs are added"), "statusText")
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
        self.device.setText(tr("Processing on: {device}", device=text))
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
        parts = [(tr_n("{n} file", "{n} files", total), None)]
        if done:
            parts.append((tr("{n} completed", n=done), Colors.SUCCESS))
        if running:
            parts.append((tr("{n} processing", n=running), Colors.PRIMARY_LIGHT))
        if failed:
            parts.append((tr("{n} failed", n=failed), Colors.DANGER))
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
