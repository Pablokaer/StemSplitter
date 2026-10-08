"""The results area: the original song's player and one channel per extracted stem, like a small DAW.

`ResultsPanel` owns a `Mixer` (stemsplitter.ui.audio) and keeps the rows in step with it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..i18n import N_, tr
from ..platform_utils import open_folder
from .audio import Mixer, TrackLoader
from .icons import icon, pixmap
from .theme import Colors, Spacing, Sizes
from .widgets import ElidedLabel, Watermark, button, card, centered_label, combo, icon_label, label, repolish

# engine output name -> (label, icon); the labels are translated where they are shown
STEM_STYLES = {
    "Vocals": (N_("Vocals"), "mic"),
    "Drums": (N_("Drums"), "drum"),
    "Bass": (N_("Bass"), "guitar"),
    "Other": (N_("Other"), "waveform"),
    "Guitar": (N_("Guitar"), "guitar"),
    "Piano": (N_("Piano"), "piano"),
    "Instrumental": (N_("Instrumental"), "music"),
    "No Drums": (N_("No Drums"), "music"),
}


def format_clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _mix(a: str, b: str, t: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t), round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t))


# -- waveform ---------------------------------------------------------------------------
class Waveform(QWidget):
    """Mirrored bars drawn from the track's peaks, brighter up to the playhead; click or drag to seek."""

    seek_requested = Signal(float)

    BAR = 3
    GAP = 2

    def __init__(self):
        super().__init__()
        self.setMinimumSize(80, 32)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._peaks: np.ndarray | None = None
        self._bars: np.ndarray | None = None
        self._bars_for = 0
        self._progress = 0.0
        self._dim = False
        self._hover_x: float | None = None
        self._hover = 0.0
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(180)
        self._fade.valueChanged.connect(self._set_hover_level)

    def set_peaks(self, peaks: np.ndarray | None) -> None:
        self._peaks = peaks
        self._bars_for = 0
        self.update()

    def set_progress(self, fraction: float) -> None:
        if fraction != self._progress:
            self._progress = fraction
            self.update()

    def set_dim(self, dim: bool) -> None:
        if dim != self._dim:
            self._dim = dim
            self.update()

    def _set_hover_level(self, value) -> None:
        self._hover = float(value)
        self.update()

    def _fade_to(self, target: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._hover)
        self._fade.setEndValue(target)
        self._fade.start()

    def _bar_heights(self, count: int) -> np.ndarray:
        """The peaks squeezed (max) or stretched to `count` bars; the loudest bar fills the height."""
        if self._bars is not None and self._bars_for == count:
            return self._bars
        peaks = self._peaks
        if peaks is None or count <= 0:
            bars = np.zeros(max(count, 0), dtype=np.float32)
        elif count < len(peaks):
            edges = np.linspace(0, len(peaks), count, endpoint=False).astype(np.int64)
            bars = np.maximum.reduceat(peaks, edges)
        else:
            bars = np.interp(np.linspace(0, len(peaks) - 1, count), np.arange(len(peaks)), peaks)
        top = float(bars.max()) if len(bars) else 0.0
        if top > 0:
            bars = (bars / top) ** 0.8
        self._bars, self._bars_for = bars.astype(np.float32), count
        return self._bars

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        step = self.BAR + self.GAP
        count = max(1, (self.width() + self.GAP) // step)
        offset = (self.width() - (count * step - self.GAP)) / 2
        bars = self._bar_heights(count)
        mid, half = self.height() / 2, self.height() / 2 - 2
        if self._dim:
            played = idle = QColor(Colors.WAVE_DIM)
        else:
            played = QColor(Colors.WAVE_ACTIVE)
            idle = _mix(Colors.WAVE_IDLE, Colors.PRIMARY_LIGHT, 0.35 * self._hover)
            if self._progress <= 0:  # nothing played yet: the whole waveform at the "to play" red
                played = idle
        flat = self._peaks is None
        p.setPen(Qt.NoPen)
        cut = self._progress * count
        for i in range(count):
            h = 1.0 if flat else max(1.5, float(bars[i]) * half)
            color = QColor(Colors.WAVE_DIM) if flat else (played if i < cut else idle)
            if flat:
                color.setAlphaF(0.5)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(offset + i * step, mid - h, self.BAR, 2 * h), 1.5, 1.5)
        if self._hover_x is not None and self._hover > 0 and not flat:
            hover = QColor("#FFFFFF")
            hover.setAlphaF(0.30 * self._hover)
            p.setBrush(hover)
            p.drawRect(QRectF(self._hover_x - 0.5, 2, 1, self.height() - 4))
        if self._progress > 0 and not self._dim and not flat:
            p.setBrush(QColor("#FFFFFF"))
            p.drawRoundedRect(QRectF(self._progress * self.width() - 1, 1, 2, self.height() - 2), 1, 1)

    def enterEvent(self, e):
        self._fade_to(1.0)

    def leaveEvent(self, e):
        self._hover_x = None
        self._fade_to(0.0)

    def mouseMoveEvent(self, e):
        self._hover_x = e.position().x()
        if e.buttons() & Qt.LeftButton:
            self._seek(e)
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._seek(e)

    def _seek(self, e) -> None:
        if self._peaks is not None:
            self.seek_requested.emit(max(0.0, min(1.0, e.position().x() / max(1, self.width()))))


# -- small controls ---------------------------------------------------------------------
class PlayButton(QPushButton):
    """The round red play/pause button."""

    def __init__(self, size: int = 32):
        super().__init__()
        self.setObjectName("playButton")
        self.setFixedSize(size, size)
        self.setIconSize(QSize(size // 2 + 2, size // 2 + 2))
        self.setCursor(Qt.PointingHandCursor)
        self._size = size
        self.set_playing(False)

    def set_playing(self, playing: bool) -> None:
        self.setIcon(icon("pause" if playing else "play", "#FFFFFF", self._size // 2 + 2))
        self.setToolTip(tr("Pause") if playing else tr("Play"))


def _channel_button(text: str, tip: str, solo: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("channelButton")
    b.setProperty("solo", solo)
    b.setCheckable(True)
    b.setFixedSize(28, 28)
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


class TimeLabel(ElidedLabel):
    def __init__(self):
        super().__init__("0:00 / 0:00", "timeText")
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setFixedWidth(96)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


# -- rows -------------------------------------------------------------------------------
class PlayerRow(QFrame):
    """The original song: its name, length, a play button, a waveform and the clock."""

    play_clicked = Signal()
    seek_requested = Signal(float)

    def __init__(self):
        super().__init__()
        self.setObjectName("playerRow")
        self.setFixedHeight(Sizes.STEM_ROW + Spacing.SM)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.LG, Spacing.SM)
        lay.setSpacing(Spacing.MD)
        tile = card("stemTile")
        tile.setFixedSize(48, 48)
        tl = QVBoxLayout(tile)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(icon_label("music", Colors.PRIMARY_LIGHT, 22), 0, Qt.AlignCenter)
        lay.addWidget(tile)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addStretch(1)
        self.name = ElidedLabel("", "fileName")
        self.meta = ElidedLabel("", "muted")
        text.addWidget(self.name)
        text.addWidget(self.meta)
        text.addStretch(1)
        holder = QWidget()
        holder.setLayout(text)
        holder.setFixedWidth(168)
        lay.addWidget(holder)
        self.play = PlayButton(36)
        self.play.clicked.connect(self.play_clicked)
        lay.addWidget(self.play)
        self.wave = Waveform()
        self.wave.seek_requested.connect(self.seek_requested)
        lay.addWidget(self.wave, 1)
        self.clock = TimeLabel()
        lay.addWidget(self.clock)

    def set_song(self, name: str, meta: str) -> None:
        self.name.setText(name)
        self.name.setToolTip(name)
        self.meta.setText(meta)

    def set_failed(self, reason: str) -> None:
        self.play.setEnabled(False)
        self.wave.set_peaks(None)
        self.meta.setText(tr("Can't play this file here"))
        self.meta.setToolTip(reason)


class StemRow(QFrame):
    """One extracted stem as a compact horizontal channel."""

    play_clicked = Signal()
    seek_requested = Signal(float)
    muted_toggled = Signal(bool)
    solo_toggled = Signal(bool)
    volume_changed = Signal(float)

    def __init__(self, key: str, path: str):
        super().__init__()
        self.key, self.path = key, path
        self.setObjectName("stemRow")
        self.setProperty("muted", False)
        self.setFixedHeight(Sizes.STEM_ROW)
        text, icon_name = STEM_STYLES.get(key, (key, "music"))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.SM, Spacing.SM)
        lay.setSpacing(Spacing.MD)

        self.tile = card("stemTile")
        self.tile.setProperty("muted", False)
        self.tile.setFixedSize(44, 44)
        tl = QVBoxLayout(self.tile)
        tl.setContentsMargins(0, 0, 0, 0)
        self.tile_icon = icon_label(icon_name, Colors.PRIMARY_LIGHT, 22)
        self._icon_name = icon_name
        tl.addWidget(self.tile_icon, 0, Qt.AlignCenter)
        lay.addWidget(self.tile)
        self.name = ElidedLabel(tr(text), "stemName")
        self.name.setFixedWidth(104)
        self.name.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.name.setToolTip(path)
        lay.addWidget(self.name)
        self.play = PlayButton(32)
        self.play.clicked.connect(self.play_clicked)
        lay.addWidget(self.play)
        self.wave = Waveform()
        self.wave.seek_requested.connect(self.seek_requested)
        lay.addWidget(self.wave, 1)

        self.btn_mute = _channel_button("M", tr("Mute"))
        self.btn_mute.toggled.connect(self.muted_toggled)
        self.btn_solo = _channel_button("S", tr("Solo"), solo=True)
        self.btn_solo.toggled.connect(self.solo_toggled)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setObjectName("volume")
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.volume.setFixedWidth(88)
        self.volume.setCursor(Qt.PointingHandCursor)
        self.volume.setToolTip(tr("Volume"))
        self.volume.valueChanged.connect(lambda v: self.volume_changed.emit(v / 100))
        self.btn_more = QPushButton()
        self.btn_more.setObjectName("iconButton")
        self.btn_more.setIcon(icon("more", Colors.TEXT_2, 20))
        self.btn_more.setIconSize(QSize(20, 20))
        self.btn_more.setFixedSize(28, 28)
        self.btn_more.setCursor(Qt.PointingHandCursor)
        self.btn_more.setToolTip(tr("More"))
        self.btn_more.clicked.connect(self._menu)
        for w in (self.btn_mute, self.btn_solo, self.volume, self.btn_more):
            lay.addWidget(w)

    def set_loading_failed(self, reason: str) -> None:
        self.play.setEnabled(False)
        for w in (self.btn_mute, self.btn_solo, self.volume):
            w.setEnabled(False)
        self.wave.set_peaks(None)
        self.name.setToolTip(f"{self.path}\n{reason}")

    def set_audible(self, audible: bool) -> None:
        """Dimmed when the mixer isn't playing this stem (muted, or another stem is solo)."""
        dim = not audible
        if self.property("muted") != dim:
            for w in (self, self.tile):
                w.setProperty("muted", dim)
                repolish(w)
            self.tile_icon.setPixmap(pixmap(self._icon_name, Colors.TEXT_MUTED if dim else Colors.PRIMARY_LIGHT, 22))
        self.wave.set_dim(dim)

    def _menu(self) -> None:
        menu = QMenu(self)
        menu.addAction(icon("folder-open", Colors.TEXT_2, 18), tr("Show in folder"),
                       lambda: open_folder(Path(self.path).parent))
        menu.addAction(icon("file", Colors.TEXT_2, 18), tr("Copy file path"),
                       lambda: QGuiApplication.clipboard().setText(self.path))
        menu.exec(self.btn_more.mapToGlobal(self.btn_more.rect().bottomLeft()))


# -- the panel --------------------------------------------------------------------------
class ResultsPanel(QFrame):
    """"Extracted Stems": the finished songs, the original's player and a channel per stem."""

    song_requested = Signal(str)  # source path picked in the song list

    ORDER = list(STEM_STYLES)

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.mixer = Mixer(self)
        self.mixer.state_changed.connect(self._sync_state)
        self.mixer.ticked.connect(self._on_tick)
        self.mixer.error.connect(self._on_error)
        self._token = 0
        self._loader: TrackLoader | None = None
        self._loaders: list[TrackLoader] = []  # kept alive until they finish
        self._pending: dict[str, tuple] = {}
        self._rows: dict[str, StemRow] = {}
        self._source = ""
        self._tracks: dict[str, np.ndarray] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(Spacing.XL, Spacing.LG, Spacing.XL, Spacing.XL)
        lay.setSpacing(Spacing.LG)

        head = QHBoxLayout()
        head.setSpacing(Spacing.MD)
        head.addWidget(icon_label("waveform", Colors.PRIMARY_LIGHT, 20))
        head.addWidget(label(tr("Extracted Stems"), "sectionTitle"))
        self.songs = combo()
        self.songs.setMinimumWidth(180)
        self.songs.setMaximumWidth(360)
        self.songs.setFixedHeight(Sizes.BUTTON_SMALL)
        self.songs.setVisible(False)
        self.songs.activated.connect(lambda i: self.song_requested.emit(self.songs.itemData(i)))
        head.addWidget(self.songs)
        head.addStretch(1)
        self.btn_play_all = button(tr("Play stems"), "play", None, Sizes.BUTTON_SMALL, Colors.TEXT)
        self.btn_play_all.clicked.connect(lambda: self.mixer.toggle("stems"))
        self.btn_folder = button(tr("Open folder"), "folder-open", "ghost", Sizes.BUTTON_SMALL, Colors.TEXT_2)
        self._folder: Path | None = None
        self.btn_folder.clicked.connect(lambda: self._folder and open_folder(self._folder))
        head.addWidget(self.btn_play_all)
        head.addWidget(self.btn_folder)
        lay.addLayout(head)

        self.stack = QStackedWidget()
        # 0: empty
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.setContentsMargins(0, Spacing.LG, 0, Spacing.LG)
        el.setSpacing(Spacing.XS)
        mark = Watermark(64, 0.30)
        el.addWidget(mark, 0, Qt.AlignHCenter)
        el.addSpacing(Spacing.SM)
        el.addWidget(centered_label(tr("Your stems will show up here"), "emptyTitle"))
        el.addWidget(centered_label(tr("Split a song, then play, solo or mute each stem."), "hint"))
        self.stack.addWidget(empty)
        # 1: loading
        loading = QWidget()
        ll = QVBoxLayout(loading)
        ll.setContentsMargins(0, Spacing.XL, 0, Spacing.XL)
        self.loading_text = centered_label(tr("Loading stems…"), "emptyTitle")
        ll.addWidget(self.loading_text)
        self.stack.addWidget(loading)
        # 2: channels
        content = QWidget()
        self.rows_layout = QVBoxLayout(content)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(Spacing.SM)
        self.player = PlayerRow()
        self.player.play_clicked.connect(lambda: self.mixer.toggle("original"))
        self.player.seek_requested.connect(self.mixer.seek)
        self.rows_layout.addWidget(self.player)
        self.rows_layout.addSpacing(Spacing.SM)
        self.stems_box = QVBoxLayout()
        self.stems_box.setSpacing(Spacing.SM)
        self.rows_layout.addLayout(self.stems_box)
        self.stack.addWidget(content)
        lay.addWidget(self.stack)
        self._sync_state()

    # -- the list of finished songs ------------------------------------------------------
    def set_songs(self, songs: list[tuple[str, str]], current: str | None = None) -> None:
        """[(source path, name)] of the songs that have stems to show."""
        self.songs.blockSignals(True)
        self.songs.clear()
        for path, name in songs:
            self.songs.addItem(name, path)
        if current is not None:
            self.songs.setCurrentIndex(max(0, self.songs.findData(current)))
        self.songs.blockSignals(False)
        self.songs.setVisible(len(songs) > 1)
        if not songs:
            self.clear()

    def clear(self) -> None:
        self._cancel_loading()
        self.mixer.clear()
        self._tracks.clear()
        self._source = ""
        self._remove_rows()
        self.stack.setCurrentIndex(0)
        self._sync_state()

    # -- loading a song ------------------------------------------------------------------
    @property
    def source(self) -> str:
        return self._source

    def load(self, source: str, stems: dict[str, str]) -> None:
        """Show `source`'s original and `stems` ({engine name: file}); decoding runs in a thread."""
        self._cancel_loading()
        self.mixer.clear()
        self._tracks.clear()
        self._remove_rows()
        self._source = source
        files = [(k, stems[k]) for k in self.ORDER if k in stems]
        files += [(k, v) for k, v in stems.items() if k not in self.ORDER]
        self.player.set_song(Path(source).name, Path(source).suffix.lstrip(".").upper())
        self.player.play.setEnabled(True)
        self.player.wave.set_peaks(None)
        self._rows = {}
        for key, path in files:
            row = StemRow(key, path)
            self._wire(row)
            self._rows[key] = row
            self.stems_box.addWidget(row)
        self._folder = Path(files[0][1]).parent if files else Path(source).parent
        self.loading_text.setText(tr("Loading stems…"))
        self.stack.setCurrentIndex(1)
        self._token += 1
        # the stems come first: they set the sample rate the original is converted to
        self._loader = TrackLoader(self._token, [*files, ("original", source)], None, self)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._loader.all_done.connect(self._on_all_done)
        self._loader.finished.connect(lambda ld=self._loader: self._forget(ld))
        self._loaders.append(self._loader)
        self._loader.start()
        self._sync_state()

    def _cancel_loading(self) -> None:
        if self._loader is not None:
            self._loader.cancelled = True
        self._token += 1
        self._tracks.clear()
        self._pending.clear()

    def _forget(self, loader: TrackLoader) -> None:
        if loader in self._loaders:
            self._loaders.remove(loader)
        loader.deleteLater()

    def _remove_rows(self) -> None:
        while self.stems_box.count():
            item = self.stems_box.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._rows = {}

    def _wire(self, row: StemRow) -> None:
        key = row.key
        row.play_clicked.connect(lambda k=key: self.mixer.toggle("stems", k))
        row.seek_requested.connect(self.mixer.seek)
        row.muted_toggled.connect(lambda on, k=key: self.mixer.set_muted(k, on))
        row.solo_toggled.connect(lambda on, k=key: self.mixer.set_solo(k, on))
        row.volume_changed.connect(lambda v, k=key: self.mixer.set_gain(k, v))

    def _on_loaded(self, token: int, key: str, data, peaks) -> None:
        if token != self._token:
            return
        self._tracks[key] = data
        wave = self.player.wave if key == "original" else self._rows[key].wave
        wave.set_peaks(peaks)
        if key == "original":
            meta = self.player.meta.fullText()
            self.player.meta.setText(f"{format_clock(len(data) / (self._rate() or 1))} · {meta}")

    def _rate(self) -> int:
        return (self._loader.rate if self._loader else None) or 44100

    def _on_failed(self, token: int, key: str, reason: str) -> None:
        if token != self._token:
            return
        if key == "original":
            self.player.set_failed(reason)
        elif key in self._rows:
            self._rows[key].set_loading_failed(reason)

    def _on_all_done(self, token: int) -> None:
        if token != self._token:
            return
        if not self._tracks:
            self.loading_text.setText(tr("These files can't be played here."))
            return
        self.mixer.load(self._tracks, self._rate())
        self.stack.setCurrentIndex(2)
        self._sync_state()
        self._on_tick(0.0, 0.0)

    def _on_error(self, message: str) -> None:
        self.btn_play_all.setToolTip(message)

    # -- keeping the rows in step with the mixer -----------------------------------------
    def _sync_state(self) -> None:
        m = self.mixer
        ready = bool(m.tracks) and m.available
        playing_stems = m.playing and m.mode == "stems" and m.focus is None
        self.btn_play_all.setEnabled(ready and any(k != "original" for k in m.tracks))
        self.btn_play_all.setText(f" {tr('Pause') if playing_stems else tr('Play stems')}")
        self.btn_play_all.setIcon(icon("pause" if playing_stems else "play", Colors.TEXT, 18))
        self.player.play.setEnabled(ready and "original" in m.tracks)
        self.player.play.set_playing(m.playing and m.mode == "original" and m.focus is None)
        for key, row in self._rows.items():
            row.play.setEnabled(ready and key in m.tracks)
            row.play.set_playing(m.playing and m.focus == key)
            row.set_audible(m.audible(key) if key in m.tracks else True)
        if not m.available and m.tracks:
            self.btn_play_all.setToolTip(tr("No audio output device was found."))

    def _on_tick(self, fraction: float, seconds: float) -> None:
        self.player.wave.set_progress(fraction)
        for row in self._rows.values():
            row.wave.set_progress(fraction)
        total = self.mixer.duration()
        self.player.clock.setText(f"{format_clock(seconds)} / {format_clock(total)}")

    def stop(self) -> None:
        self.mixer.pause()
