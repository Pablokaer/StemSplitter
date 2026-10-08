"""Listening to a finished split: decoding, waveform peaks and a small multi-track mixer.

The mixer plays every track of a song (the original and each stem) from one position, so they stay
sample-accurate with each other, and mutes, solos and volumes take effect within a few milliseconds.
It uses Qt's own audio output (QtMultimedia, part of PySide6): no extra dependency.

Everything is held as 16-bit stereo at one sample rate (the stems' own), about 10 MB per minute per track.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QIODevice, QObject, QThread, QTimer, Signal

try:
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

    AUDIO_OUTPUT = True
except ImportError:  # a build without QtMultimedia: the results still show, they just can't be played
    AUDIO_OUTPUT = False

DEFAULT_RATE = 44100
BYTES_PER_FRAME = 4  # 16-bit stereo
PEAK_BINS = 1600


# -- decoding ---------------------------------------------------------------------------
def _to_stereo(data: np.ndarray) -> np.ndarray:
    if data.shape[1] == 1:
        return np.repeat(data, 2, axis=1)
    return data[:, :2]


def _resample(data: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear interpolation: plenty for listening to a preview."""
    n = max(1, int(len(data) * dst / src))
    x = np.linspace(0, len(data) - 1, n)
    xp = np.arange(len(data))
    out = np.empty((n, 2), dtype=np.int16)
    for ch in range(2):
        out[:, ch] = np.interp(x, xp, data[:, ch])
    return out


def _decode_with_ffmpeg(path: str, rate: int) -> np.ndarray:
    from ..platform_utils import find_ffmpeg, subprocess_flags

    cmd = [find_ffmpeg(), "-v", "error", "-nostdin", "-i", path, "-vn", "-f", "s16le", "-acodec", "pcm_s16le",
           "-ac", "2", "-ar", str(rate), "-"]
    done = subprocess.run(cmd, capture_output=True, **subprocess_flags())
    if done.returncode or not done.stdout:
        lines = done.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "ffmpeg failed")
    return np.frombuffer(done.stdout, dtype=np.int16).reshape(-1, 2).copy()


def decode_audio(path: str, rate: int | None = None) -> tuple[np.ndarray, int]:
    """The file as int16 stereo and its sample rate (converted to `rate` when given). libsndfile first (WAV,
    FLAC, MP3, OGG...), ffmpeg for anything else."""
    try:
        import soundfile as sf

        data, sr = sf.read(path, dtype="int16", always_2d=True)
        data = _to_stereo(data)
        if rate and sr != rate:
            data, sr = _resample(data, sr, rate), rate
        return np.ascontiguousarray(data), sr
    except Exception:
        rate = rate or DEFAULT_RATE
        return _decode_with_ffmpeg(path, rate), rate


def peaks_of(data: np.ndarray, bins: int = PEAK_BINS) -> np.ndarray:
    """`bins` loudness peaks (0..1) of a waveform, enough to draw it at any width."""
    n = len(data)
    if n == 0:
        return np.zeros(bins, dtype=np.float32)
    starts = np.linspace(0, n, bins, endpoint=False).astype(np.int64)
    hi = np.maximum.reduceat(data, starts, axis=0).max(axis=1).astype(np.int32)
    lo = -np.minimum.reduceat(data, starts, axis=0).min(axis=1).astype(np.int32)
    return (np.maximum(hi, lo) / 32768.0).astype(np.float32)


class TrackLoader(QThread):
    """Decodes a song's files one after another off the interface thread."""

    loaded = Signal(int, str, object, object)  # token, key, int16 data, peaks
    failed = Signal(int, str, str)  # token, key, reason
    all_done = Signal(int)

    def __init__(self, token: int, files: list[tuple[str, str]], rate: int | None, parent=None):
        super().__init__(parent)
        self.token = token
        self.files = files  # [(key, path)]: the first one that decodes sets the sample rate when `rate` is None
        self.rate = rate
        self.cancelled = False

    def run(self) -> None:
        for key, path in self.files:
            if self.cancelled:
                return
            try:
                data, self.rate = decode_audio(path, self.rate)
            except Exception as exc:
                self.failed.emit(self.token, key, str(exc))
                continue
            self.loaded.emit(self.token, key, data, peaks_of(data))
        self.all_done.emit(self.token)


# -- mixer ------------------------------------------------------------------------------
@dataclass
class Track:
    key: str
    data: np.ndarray
    gain: float = 1.0
    muted: bool = False
    solo: bool = False
    applied: float = 0.0  # the gain the last chunk ended at, so changes ramp instead of clicking


class _MixDevice(QIODevice):
    """The sink pulls mixed audio from here."""

    def __init__(self, mixer: Mixer):
        super().__init__()
        self.mixer = mixer
        self.cursor = 0
        self.open(QIODevice.ReadOnly)

    def isSequential(self) -> bool:
        return True

    def bytesAvailable(self) -> int:
        return max(0, self.mixer.length - self.cursor) * BYTES_PER_FRAME + super().bytesAvailable()

    def readData(self, maxlen: int) -> bytes:
        return self.mixer.render(self, maxlen // BYTES_PER_FRAME)

    def writeData(self, data) -> int:
        return 0


class Mixer(QObject):
    """Plays the tracks of one song. Which tracks are heard:

    * `focus` set: only that track (the play button of a single stem);
    * `mode == "original"`: only the original song;
    * otherwise the stems, honoring each one's mute, solo and volume.
    """

    state_changed = Signal()  # playing / mode / focus / mute / solo changed
    ticked = Signal(float, float)  # fraction, seconds: while playing
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracks: dict[str, Track] = {}
        self.rate = DEFAULT_RATE
        self.length = 0  # frames
        self.pos = 0  # frames, when not playing
        self.mode = "stems"
        self.focus: str | None = None
        self.playing = False
        self._base = 0
        self._sink = None
        self._device = _MixDevice(self)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    # -- tracks -----------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return AUDIO_OUTPUT and not QMediaDevices.defaultAudioOutput().isNull()

    def load(self, tracks: dict[str, np.ndarray], rate: int) -> None:
        self.stop()
        self.tracks = {key: Track(key, data) for key, data in tracks.items()}
        self.rate = rate
        self.length = max((len(d) for d in tracks.values()), default=0)
        self.pos = 0
        self.mode, self.focus = "stems", None
        self._sink = None
        self.state_changed.emit()

    def clear(self) -> None:
        self.load({}, self.rate)

    def audible(self, key: str) -> bool:
        track = self.tracks.get(key)
        if track is None:
            return False
        if self.focus is not None:
            return key == self.focus
        if self.mode == "original":
            return key == "original"
        if key == "original":
            return False
        if any(t.solo for k, t in self.tracks.items() if k != "original"):
            return track.solo
        return not track.muted

    def set_gain(self, key: str, value: float) -> None:
        if key in self.tracks:
            self.tracks[key].gain = max(0.0, min(1.0, value))

    def set_muted(self, key: str, on: bool) -> None:
        if key in self.tracks:
            self.tracks[key].muted = on
            self.state_changed.emit()

    def set_solo(self, key: str, on: bool) -> None:
        if key in self.tracks:
            self.tracks[key].solo = on
            self.state_changed.emit()

    # -- rendering (called from the audio thread) ---------------------------------------
    def render(self, device: _MixDevice, frames: int) -> bytes:
        start = device.cursor
        n = min(frames, self.length - start)
        if n <= 0:
            return b""
        acc = np.zeros((n, 2), dtype=np.float32)
        for track in list(self.tracks.values()):
            target = np.float32(track.gain if self.audible(track.key) else 0.0)
            segment = track.data[start:start + n]
            m = len(segment)
            if m and (target > 0 or track.applied > 0):
                if track.applied != target:  # ramp over this chunk: a mute or solo never clicks
                    ramp = np.linspace(track.applied, target, m, dtype=np.float32)[:, None]
                    acc[:m] += segment * ramp
                else:
                    acc[:m] += segment * target
            track.applied = float(target)
        np.clip(acc, -32768, 32767, out=acc)
        device.cursor = start + n
        return acc.astype(np.int16).tobytes()

    # -- transport ----------------------------------------------------------------------
    def position(self) -> int:
        """Frames played so far (what the speakers are at, not what has been buffered)."""
        if not self.playing or self._sink is None:
            return self.pos
        played = int(self._sink.processedUSecs() * self.rate / 1_000_000)
        return max(0, min(self.length, self._base + played))

    def toggle(self, mode: str = "stems", focus: str | None = None) -> None:
        """Play with this selection, or pause when it is already what is playing."""
        if self.playing and (self.mode, self.focus) == (mode, focus):
            self.pause()
            return
        self.mode, self.focus = mode, focus
        if not self.playing:
            self._start()
        self.state_changed.emit()

    def _make_sink(self) -> bool:
        if not self.available:
            self.error.emit("No audio output device")
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(self.rate)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.Int16)
        self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), fmt, self)
        self._sink.setBufferSize(self.rate // 10 * BYTES_PER_FRAME)  # ~100 ms: how soon a mute is heard
        return True

    def _start(self) -> None:
        if self.length == 0:
            return
        if self._sink is None and not self._make_sink():
            return
        if self.pos >= self.length - 1:
            self.pos = 0
        for track in self.tracks.values():
            track.applied = 0.0  # fade in
        self._device.cursor = self._base = self.pos
        self._sink.start(self._device)
        self.playing = True
        self._timer.start()

    def pause(self) -> None:
        if not self.playing:
            return
        self.pos = self.position()
        self._halt()
        self.state_changed.emit()

    def stop(self) -> None:
        self.pos = 0
        self._halt()

    def _halt(self) -> None:
        self._timer.stop()
        if self._sink is not None:
            self._sink.stop()
        self.playing = False

    def seek(self, fraction: float) -> None:
        frames = int(max(0.0, min(1.0, fraction)) * self.length)
        if self.playing:
            self._sink.stop()
            self._device.cursor = self._base = self.pos = frames
            self._sink.start(self._device)
        else:
            self.pos = frames
        self.ticked.emit(self.fraction(), self.seconds())

    def fraction(self) -> float:
        return self.position() / self.length if self.length else 0.0

    def seconds(self) -> float:
        return self.position() / self.rate

    def duration(self) -> float:
        return self.length / self.rate

    def _tick(self) -> None:
        if self._device.cursor >= self.length and self.position() >= self.length - self.rate // 50:
            self.pos = 0  # reached the end: back to the start, paused
            self._halt()
            self.state_changed.emit()
            self.ticked.emit(0.0, 0.0)
            return
        self.ticked.emit(self.fraction(), self.seconds())
