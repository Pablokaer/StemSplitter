"""Separation engine.

Pipeline (streamed through files in the song's work folder, see _Work and _Track):
  1. ffmpeg decodes the input (MP3, WAV, FLAC, M4A...) to 44.1 kHz stereo float32 via a pipe.
  2. BS-RoFormer SW (jarredou) splits the mix in one pass into vocals, drums, bass,
     guitar, piano and other.
  3. "maximum" only: MelBand-RoFormer (Kimberley Jensen) also isolates the vocals and the
     two vocal estimates are averaged (ensembling two strong models beats either one).
  4. Other = mix - (vocals + drums + bass [+ guitar] [+ piano]), so the stems always add up
     to the original song exactly. Instrumental = mix - Vocals and No Drums = mix - Drums (the
     song with only the drums taken out). Only the outputs picked in Options.stems are built
     (and only the model stems they need), then encoded to MP3 (320 kbps by default) in
     parallel. split() and write_stems() are separate so a batch can encode one song while
     the next one is on the GPU.

Memory: the models run chunk by chunk and every finished sample goes straight to a file, so a
song never has to fit in RAM (the output is bit-identical to doing it all in memory). Every
step asks the memory governor (memgov.py) first, which keeps the app below what the system
needs to stay responsive: it frees caches, unloads idle models, runs fewer things in parallel
and, as a last resort, waits for memory without losing any work. A checkpoint per song lets an
interrupted split continue where it stopped.

Profiled on an RTX 3050 Laptop, the model forward pass is most of the time and the GPU is
saturated by it: batching chunks, cudnn.benchmark, TF32 and torch.compile (broken on
Windows) gave no speed-up, so the remaining knob is the preset's overlap.

Measured on the MUSDB18 test set (50 songs, median SDR in dB, higher is better):

  preset     vocals drums  bass  other   avg     (timings: see README)
  maximum     12.03 11.38  9.58   8.04  10.26
  balanced    11.83 11.38  9.58   8.03  10.20
  fast        11.54 10.92  9.11   7.69   9.81
  (old)       11.30  9.77  8.76   6.72   9.14   MelBand-RoFormer + htdemucs_ft

Inference runs on the NVIDIA (CUDA) or Apple (MPS) GPU whenever one is present and falls
back to the CPU otherwise. On a GPU both models run in float16.
"""

from __future__ import annotations

import atexit
import gc
import hashlib
import json
import logging
import os

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # Apple Silicon: fall back to CPU for rare ops

from .platform_utils import app_data_dir  # noqa: E402

# librosa's numba functions use cache=True; inside a packaged app the package folder is read-only.
os.environ.setdefault("NUMBA_CACHE_DIR", str(app_data_dir() / "numba_cache"))

import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

from .memgov import GB, MB, MemoryGovernor, reserve_from_env
from .memory import memlog
from .platform_utils import find_ffmpeg, models_dir, subprocess_flags

STEM_MODEL = "BS-Roformer-SW.ckpt"  # 6 stems: bass, drums, other, vocals, guitar, piano
VOCAL_MODEL = "vocals_mel_band_roformer.ckpt"
SAMPLE_RATE = 44100

# Every file the app can write per song, in this order, and the model stems each is built from.
OUTPUTS = {
    "Vocals": {"Vocals"},
    "Drums": {"Drums"},
    "Bass": {"Bass"},
    "Other": {"Vocals", "Drums", "Bass"},  # mix - the rest (also minus Guitar / Piano when those are written)
    "Guitar": {"Guitar"},
    "Piano": {"Piano"},
    "Instrumental": {"Vocals"},  # mix - Vocals
    "No Drums": {"Drums"},  # mix - Drums: the song with only the drums taken out
}
MODEL_STEMS = ["Vocals", "Drums", "Bass", "Guitar", "Piano"]  # the stem model's outputs that are used
DEFAULT_STEMS = ("Vocals", "Drums", "Bass", "Other")
BLOCK_FRAMES = SAMPLE_RATE * 10  # 10 s: the unit for decoding, assembling and encoding (~3.5 MB per stem)
WORK_VERSION = 2  # bump when the work-folder layout changes (old checkpoints are then ignored)
CHECKPOINT_SECONDS = 15
STALE_WORK_SECONDS = 7 * 24 * 3600
CHUNK_NEED_DEFAULT = 768 * MB  # memory asked for one inference chunk until the real cost is measured
CHUNK_NEED_MIN = 64 * MB
ENCODER_NEED = 96 * MB  # one ffmpeg/LAME process with its pipe buffers
OOM_RETRIES = 12  # a chunk that runs out of memory is retried this many times (with growing waits)

SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".wma"}


@dataclass(frozen=True)
class Preset:
    # RoFormer "overlap" = number of overlapping prediction windows. Each one is a full pass,
    # so time grows linearly; 2 is clearly better than 1, 4 adds almost nothing over 2.
    stem_overlap: int
    vocal_overlap: int = 0  # 0 = no second vocal model


QUALITY_PRESETS = {
    "maximum": Preset(stem_overlap=2, vocal_overlap=2),
    "balanced": Preset(stem_overlap=2),
    "fast": Preset(stem_overlap=1),
}
DEFAULT_QUALITY = "balanced"
QUALITY_ALIASES = {"high": "balanced"}  # name used by older versions (scripts)


def get_preset(quality: str) -> Preset:
    return QUALITY_PRESETS.get(QUALITY_ALIASES.get(quality, quality), QUALITY_PRESETS[DEFAULT_QUALITY])


ProgressFn = Callable[[float, str], None]  # (0..1 overall, status text)
LogFn = Callable[[str], None]


class Cancelled(Exception):
    """Raised inside the worker when the user presses Cancel."""


@dataclass
class Options:
    output_dir: Path
    bitrate_kbps: int = 320
    quality: str = DEFAULT_QUALITY
    stems: tuple[str, ...] = DEFAULT_STEMS  # the files to write (names from OUTPUTS)
    also_instrumental: bool = False  # older switches, added to `stems` (for scripts that still use them)
    guitar_piano: bool = False  # also split Guitar and Piano out of Other
    output_format: str = "mp3"  # "mp3" or "wav"
    memory_reserve_mb: int = 0  # memory the system must keep free; 0 = automatic (see memgov.py)
    ignore_memory_limit: bool = False  # True: the governor never waits or holds back (see memgov.py)

    def outputs(self) -> list[str]:
        """The files to write, in OUTPUTS order."""
        wanted = set(self.stems)
        if self.also_instrumental:
            wanted.add("Instrumental")
        if self.guitar_piano:
            wanted |= {"Guitar", "Piano"}
        unknown = wanted - OUTPUTS.keys()
        if unknown:
            raise ValueError(f"Unknown stem(s): {', '.join(sorted(unknown))}")
        if not wanted:
            raise ValueError("Choose at least one stem to write")
        return [k for k in OUTPUTS if k in wanted]


def model_stems(outputs: list[str]) -> list[str]:
    """The stem model's outputs that `outputs` are built from, in MODEL_STEMS order."""
    needed = set().union(*(OUTPUTS[k] for k in outputs))
    return [k for k in MODEL_STEMS if k in needed]


@dataclass
class Result:
    input_path: Path
    stems: dict[str, Path] = field(default_factory=dict)
    seconds: float = 0.0


@dataclass
class _PendingAssembly:
    """What write_stems needs to build the stems itself (see split(assemble=False))."""

    frames: int
    names: list[str]
    outputs: list[str]
    gain: float
    maximum: bool


@dataclass
class Separated:
    """The stems of one song (samples x channels float32, as arrays or files), ready to write."""

    input_path: Path
    stems: dict[str, np.ndarray]
    sr: int
    timings: dict[str, float]
    started: float
    work: Optional["_Work"] = None  # the song's work folder, removed once every file is written
    peaks: dict[str, float] = field(default_factory=dict)  # per stem, for the clipping protection
    pending: Optional[_PendingAssembly] = None  # set when write_stems still has to assemble the stems


# --------------------------------------------------------------------------------------
# Progress hook: audio-separator reports progress through tqdm. We swap its tqdm for a
# tiny object that forwards progress to the GUI (and lets us cancel between chunks).
# --------------------------------------------------------------------------------------
class _ProgressHub:
    def __init__(self) -> None:
        self.cancel_event: Optional[threading.Event] = None
        self.on_bar: Optional[Callable[["_HookTqdm", bool], None]] = None

    def check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled()


HUB = _ProgressHub()


class _HookTqdm:
    """Minimal tqdm stand-in supporting iteration, manual update and context manager use."""

    def __init__(self, iterable=None, *args, total=None, **kwargs):
        self.iterable = iterable
        if total is None and iterable is not None:
            try:
                total = len(iterable)
            except TypeError:
                total = None
        self.total = total or 0
        self.n = 0
        if HUB.on_bar:
            HUB.on_bar(self, True)

    def __iter__(self):
        for item in self.iterable:
            HUB.check_cancel()
            yield item
            self.update(1)

    def __len__(self):
        return len(self.iterable) if self.iterable is not None else int(self.total)

    def update(self, n=1):
        self.n += n
        if HUB.on_bar:
            HUB.on_bar(self, False)
        HUB.check_cancel()

    @property
    def fraction(self) -> float:
        return min(1.0, self.n / self.total) if self.total else 0.0

    # tqdm API no-ops
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        pass

    def set_description(self, *a, **k):
        pass

    def set_postfix(self, *a, **k):
        pass

    def refresh(self, *a, **k):
        pass

    def write(self, *a, **k):
        pass


_patched = False


def _install_progress_hooks() -> None:
    global _patched
    if _patched:
        return
    import audio_separator.separator.separator as sep_mod
    from audio_separator.separator.architectures import mdxc_separator

    sep_mod.tqdm = _HookTqdm  # model downloads
    mdxc_separator.tqdm = _HookTqdm  # RoFormer chunks
    _patched = True


def gpu_available() -> bool:
    import torch

    if torch.cuda.is_available():
        return True
    mps = getattr(torch.backends, "mps", None)
    return bool(mps and mps.is_available())


def _apply_thread_override() -> None:
    """STEMSPLITTER_THREADS=N pins PyTorch's CPU thread count (worth trying on hybrid P/E-core CPUs)."""
    value = os.environ.get("STEMSPLITTER_THREADS", "").strip()
    if value.isdigit() and int(value) > 0:
        import torch

        torch.set_num_threads(int(value))


# --------------------------------------------------------------------------------------
class _Track:
    """Stereo float32 audio (frames x 2) in a raw file, read and written in blocks: track[i:j].

    Plain file I/O on purpose, not a memory map: the data then sits in the OS file cache, which
    every OS counts as available memory and gives back first, while a memory map's pages count
    against this process's own memory (on Windows they leave "available" memory altogether),
    which made the memory governor wait for memory the app itself was holding.
    """

    def __init__(self, path: Path, frames: int, mode: str = "r") -> None:
        if mode == "w+":
            # An empty file that grows as it is written: every "w+" track is written from start to end
            # before anything reads it. (Pre-sizing it with truncate() made NTFS write zeros first, ~85 MB
            # per 4-minute track, which the real samples then overwrote.)
            open(path, "wb").close()
        self._f = open(path, "rb" if mode == "r" else "r+b")
        self._lock = threading.Lock()
        self.frames = frames
        self.shape = (frames, 2)
        self.dtype = np.dtype(np.float32)

    def __len__(self) -> int:
        return self.frames

    def _range(self, key: slice) -> tuple[int, int]:
        start, stop, step = key.indices(self.frames)
        if step != 1:
            raise IndexError("only contiguous slices are supported")
        return start, max(start, stop)

    def __getitem__(self, key: slice) -> np.ndarray:
        start, stop = self._range(key)
        buf = bytearray((stop - start) * 8)
        with self._lock:
            self._f.seek(start * 8)
            got = self._f.readinto(buf)
        return np.frombuffer(buf, dtype=np.float32, count=got // 4).reshape(-1, 2)

    def __setitem__(self, key: slice, value) -> None:
        start, stop = self._range(key)
        data = np.ascontiguousarray(value, dtype=np.float32)
        if data.shape != (stop - start, 2):
            raise ValueError(f"expected {(stop - start, 2)} samples, got {data.shape}")
        with self._lock:
            self._f.seek(start * 8)
            self._f.write(memoryview(data).cast("B"))

    def __array__(self, dtype=None, copy=None) -> np.ndarray:
        return self[0 : self.frames]  # whole track (tests and scripts; the app never does this)

    def flush(self) -> None:
        if not self._f.closed:
            self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class _Work:
    """The on-disk state of one song in progress: its audio tracks plus a JSON checkpoint.

    The decoded mix, the models' outputs and the final stems live in raw float32 files
    (_Track), so a song never has to fit in RAM: while memory is plentiful the OS keeps them
    cached and it is as fast as RAM; under pressure it simply drops them. The checkpoint lets a
    split continue where it stopped after a crash or a cancel.
    """

    def __init__(self, root: Path, key: str) -> None:
        self.dir = root / key
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.state: dict = {}
        try:
            state = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
            if state.get("version") == WORK_VERSION:
                self.state = state
        except (OSError, ValueError):
            pass

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.f32"

    def array(self, name: str, frames: int, mode: str = "r") -> _Track:
        return _Track(self.path(name), frames, mode)

    def save(self, **updates) -> None:
        with self._lock:
            self.state.update(updates, version=WORK_VERSION)
            tmp = self.dir / "state.json.tmp"
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            os.replace(tmp, self.dir / "state.json")

    def forget(self, *keys: str) -> None:
        """Remove steps from the checkpoint, so they run again."""
        with self._lock:
            for key in keys:
                self.state.pop(key, None)
        self.save()

    def complete(self, name: str, frames: int) -> bool:
        """True if the track holds all `frames` (nothing is fsync'ed: a power cut can leave it short)."""
        try:
            return self.path(name).stat().st_size >= frames * 8
        except OSError:
            return False

    def drop(self, *names: str) -> None:
        for name in names:
            self.unlink(f"{name}.f32")

    def unlink(self, filename: str) -> None:
        """Delete a file of the work folder; never fails (what is left goes with the folder)."""
        try:
            (self.dir / filename).unlink(missing_ok=True)
        except OSError:
            pass

    def remove(self) -> None:
        gc.collect()  # tracks still referenced somewhere close when collected
        shutil.rmtree(self.dir, ignore_errors=True)


def _song_key(path: Path, opts: Options) -> str:
    st = path.stat()
    ident = [str(path.resolve()), st.st_size, st.st_mtime_ns, get_preset(opts.quality), opts.outputs(),
             WORK_VERSION]
    return hashlib.sha1(repr(ident).encode()).hexdigest()[:16]


def _is_oom(exc: BaseException) -> bool:
    return "out of memory" in str(exc).lower() or type(exc).__name__ == "OutOfMemoryError"


# --------------------------------------------------------------------------------------
class StemEngine:
    """Keeps models loaded between songs so batch processing is faster.

    One instance per model: the overlap is only read when inference runs, so the presets share
    it. Every step that needs memory asks the memory governor first (see memgov.py).
    """

    def __init__(self, log: Optional[LogFn] = None, governor: Optional[MemoryGovernor] = None) -> None:
        self.log = log or (lambda msg: None)
        self.ffmpeg = find_ffmpeg()
        self._separators: dict[str, object] = {}  # model file -> Separator
        self._active_model: Optional[str] = None  # the model running right now (never unloaded)
        self._work_dir = Path(tempfile.mkdtemp(prefix="stemsplitter_"))
        atexit.register(shutil.rmtree, self._work_dir, True)
        self._log_handler: Optional[logging.Handler] = None
        self._own_gov = governor is None
        self.gov = governor or MemoryGovernor(reserve_from_env(), log=self.log)
        if self._own_gov:
            self.gov.start()  # samples every 250 ms, so the cost of each step can be measured
        self.gov.add_shedder(self._shed)
        self._songs_dir = app_data_dir() / "work"
        self._chunk_need: dict[str, int] = {}  # learned memory cost of one inference chunk, per model
        self._load_cost: dict[str, int] = {}  # measured memory taken while loading each model
        self._encoded_lock = threading.Lock()
        self._clean_stale_work()

    # -- helpers ------------------------------------------------------------------------
    def _run_ffmpeg(self, args: list[str], stdin=None) -> bytes:
        """Run ffmpeg; returns its stdout."""
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
        proc = subprocess.run(cmd, input=stdin, capture_output=True, **subprocess_flags())
        if proc.returncode != 0:
            raise RuntimeError(_ffmpeg_error(proc.stderr))
        return proc.stdout

    def _clean_stale_work(self) -> None:
        """Songs left half-done (cancelled, crashed) are resumed when split again; drop them after a week."""
        try:
            for d in self._songs_dir.iterdir():
                if d.is_dir() and time.time() - d.stat().st_mtime > STALE_WORK_SECONDS:
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass

    def _attach_logging(self) -> None:
        if self._log_handler is not None:
            return
        engine = self

        class _Handler(logging.Handler):
            def emit(self, record):
                msg = record.getMessage()
                # the ONNX notice is irrelevant: both models run on PyTorch, which does use the GPU
                if "Using soundfile" in msg or "ONNXruntime" in msg:
                    return
                if record.levelno >= logging.WARNING or any(
                    k in msg for k in ("Downloading", "Model downloaded", "hardware acceleration", "Using device")
                ):
                    engine.log(msg)

        self._log_handler = _Handler(level=logging.INFO)
        logging.getLogger("audio_separator").addHandler(self._log_handler)

    def _model_need(self, model: str) -> int:
        """Memory a model takes while it loads (the checkpoint is read in float32, then halved on a GPU)."""
        if model in self._separators:
            return 0
        if model in self._load_cost:  # measured the last time it loaded
            return max(256 * MB, int(self._load_cost[model] * 1.2))
        try:
            size = (models_dir() / model).stat().st_size
        except OSError:
            size = 1000 * MB  # not downloaded yet
        return size + 128 * MB  # measured: the 0.70 GB stem model takes ~0.76 GB while it loads

    def _separator(self, model: str, overlap: int, out_dir: Path, on_wait=None):
        """Return a Separator with `model` loaded, writing float WAVs into `out_dir`."""
        _install_progress_hooks()
        _apply_thread_override()
        from audio_separator.separator import Separator

        # find_ffmpeg() already verified ffmpeg; the library's own check would flash a console on Windows.
        Separator.check_ffmpeg_installed = lambda self: None
        sep = self._separators.get(model)
        if sep is None:
            self._place_on_gpu(None)  # load next to the other models in RAM, not on top of them in VRAM
            self.gov.wait(self._model_need(model), HUB.check_cancel, on_wait)
            sep = Separator(
                log_level=logging.WARNING,
                model_file_dir=str(models_dir()),
                output_dir=str(out_dir),
                output_format="WAV",
                use_soundfile=True,  # keeps 32-bit float (no 16-bit rounding between steps)
                normalization_threshold=1.0,  # only prevents clipping; keeps stems summable
                sample_rate=SAMPLE_RATE,
                mdxc_params={"segment_size": 256, "override_model_segment_size": False, "batch_size": 1,
                             "overlap": overlap, "pitch_shift": 0},
                # float16 RoFormer: much faster on GPU and half the VRAM; the output differs from
                # float32 only ~40 dB down. The library only allows it where verified (CUDA / MPS).
                use_native_fp16=gpu_available(),
            )
            logging.getLogger("audio_separator").setLevel(logging.INFO)
            self._attach_logging()
            self.gov.mark()
            sep.load_model(model_filename=model)
            self._separators[model] = sep
            took = self.gov.drop_since_mark()
            self._load_cost[model] = took
            memlog(f"{model} loaded (took {took / MB:.0f} MB while loading)", self.log)
        sep.output_dir = str(out_dir)
        if getattr(sep, "model_instance", None) is not None:
            sep.model_instance.output_dir = str(out_dir)
            sep.model_instance.overlap = overlap
        return sep

    def _place_on_gpu(self, active: Optional[str]) -> None:
        """CUDA: keep only the `active` model in VRAM; the others wait in RAM (None: all in RAM).

        Only "maximum" has two models. Both on a 4 GB GPU overflow its memory and Windows then
        spills into shared system RAM, which made the split several times slower; moving a model
        between RAM and VRAM takes a fraction of a second. Inference runs wherever the model is.
        (Apple GPUs share one memory with the CPU, so there is nothing to gain there.)
        """
        if not self._separators:
            return
        import torch

        if not torch.cuda.is_available():
            return
        freed = False
        for name, sep in self._separators.items():
            mi = sep.model_instance
            net = getattr(mi, "model_run", None)
            if net is None or getattr(mi.torch_device, "type", None) != "cuda":
                continue
            want = mi.torch_device if name == active else torch.device("cpu")
            if next(net.parameters()).device.type != want.type:
                net.to(want)
                freed = freed or want.type == "cpu"
        if freed:
            torch.cuda.empty_cache()

    def _empty_caches(self) -> None:
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is None:
            return
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass

    def _shed(self, level: str) -> None:
        """Memory governor callback: give back what isn't needed right now."""
        if level in ("tight", "over"):
            idle = [m for m in self._separators if m != self._active_model]
            for m in idle:  # reloaded when needed again (a few seconds), instead of stalling
                del self._separators[m]
            if idle:
                self.log(f"Low memory: unloaded {', '.join(idle)} until it is needed")
        self._empty_caches()

    def _keep_only(self, preset: Preset) -> None:
        """Unload models the preset doesn't use (e.g. the vocal model after leaving "maximum")."""
        wanted = {model for model, _, _ in self._models_for(preset)}
        stale = [m for m in self._separators if m not in wanted]
        if not stale:
            return
        for m in stale:
            del self._separators[m]
        self._empty_caches()
        memlog("unused models released", self.log)

    @staticmethod
    def _models_for(preset: Preset) -> list[tuple[str, int, str]]:
        models = [(STEM_MODEL, preset.stem_overlap, "stem model")]
        if preset.vocal_overlap:
            models.append((VOCAL_MODEL, preset.vocal_overlap, "vocal model"))
        return models

    # -- public API -----------------------------------------------------------------------
    def prepare_models(
        self, progress: ProgressFn, cancel: Optional[threading.Event] = None, quality: str = DEFAULT_QUALITY
    ) -> None:
        """Download (first run only) and load the models the preset needs."""
        HUB.cancel_event = cancel
        preset = get_preset(quality)
        self._keep_only(preset)
        models = self._models_for(preset)
        for i, (model, overlap, label) in enumerate(models):
            base, span = i / len(models), 1 / len(models)

            def on_bar(bar: _HookTqdm, new: bool, base=base, span=span, label=label):
                if bar.total > 1_000_000:  # byte-counted download bar
                    mb = bar.n / 1e6
                    progress(base + bar.fraction * span, f"Downloading {label} (first run only): {mb:.0f} MB")

            HUB.on_bar = on_bar
            progress(base, f"Loading {label}...")
            self._separator(model, overlap, self._work_dir,
                            on_wait=lambda short: progress(base, _waiting_text(short)))
        HUB.on_bar = None
        progress(1.0, "Models ready")

    def can_preload(self, quality: str) -> bool:
        """True if the preset's models fit in the memory budget right now (preloading never waits)."""
        need = sum(self._model_need(m) for m, _, _ in self._models_for(get_preset(quality)))
        self.gov.sample()
        return self.gov.headroom >= need

    def _stream_model(
        self, model: str, overlap: int, work: _Work, frames: int, gain: float, outputs: dict[str, str],
        tag: str, label: str, report,
    ) -> None:
        """Run one model over the song, streaming: `outputs` maps a model stem (lower case) to a work file.

        This is the library's RoFormer demix() loop (same chunk schedule, Hamming window,
        overlap-add, counter and division, in the same order, so the output is bit-identical),
        except that every sample is divided out and written to disk as soon as no later chunk
        covers it. Memory is then one chunk of buffers instead of the whole song times every
        stem. The loop also checkpoints itself, retries a chunk that ran out of memory and asks
        the memory governor before each chunk.
        """
        import torch
        from audio_separator.separator.architectures import mdxc_separator as mdxc
        from scipy import signal

        def downloading(frac: float, mb: float) -> None:
            report(0, f"Downloading model (first run only): {mb:.0f} MB ({frac:.0%})")

        HUB.on_bar = self._bar_tracker(lambda f: None, 1, downloading)
        report(0, "Loading model...")
        self._active_model = model
        mi = self._separator(model, overlap, self._work_dir, on_wait=lambda s: report(0, _waiting_text(s))).model_instance
        HUB.on_bar = None
        self._place_on_gpu(model)
        report(0, f"{label}...")

        cfg = mi.model_data_cfgdict
        # same rule the library applies in separate(): very short clips use the configured segment size
        use_override = getattr(mi, "_use_model_segment_override", lambda seconds: False)(frames / SAMPLE_RATE)
        dim_t = mi.segment_size if use_override else cfg.inference.dim_t
        hop = getattr(cfg.model, "stft_hop_length", None) or cfg.audio.hop_length
        chunk = int(hop) * (int(dim_t) - 1)
        step = chunk // mi.overlap
        instruments = [str(i).lower() for i in cfg.training.instruments]
        target = cfg.training.target_instrument
        # Only the instruments that are written are accumulated (the model also returns e.g. its own "other",
        # or 5 stems nobody asked for): each kept sample goes through exactly the same operations as before.
        # A single-target model returns no instrument dimension; its output broadcasts into the one row.
        model_rows = sorted({0 if target else instruments.index(name) for name in outputs})
        rows = {name: model_rows.index(0 if target else instruments.index(name)) for name in outputs}
        device = next(mi.model_run.parameters()).device
        pick = None if target or len(model_rows) == len(instruments) else torch.tensor(model_rows, device=device)
        estimated = mdxc._estimate_roformer_full_track_buffer_bytes(len(instruments), 2, frames, chunk)
        acc = device if mdxc.should_accumulate_on_device(device, estimated) else torch.device("cpu")
        window = torch.tensor(signal.windows.hamming(chunk), dtype=torch.float32, device=acc)
        starts = mi._roformer_chunk_starts(frames, chunk, step)
        shape = (len(model_rows), 2, chunk)
        counter_shape = (1, 1, chunk)  # the window sum is the same for every instrument and channel

        mix = work.array("mix", frames, "r")
        saved = dict(work.state.get(tag) or {})
        resumable = all(work.path(f).is_file() for f in outputs.values()) and saved.get("rows") == model_rows
        if resumable and saved.get("next") and (work.dir / saved.get("window", "-")).is_file():
            k0, base = saved["next"], saved["base"]
            with np.load(work.dir / saved["window"]) as buffers:  # closed at once (Windows can't delete open files)
                result = torch.from_numpy(buffers["result"]).to(acc)
                counter = torch.from_numpy(buffers["counter"]).to(acc)
            outs = {name: work.array(f, frames, "r+") for name, f in outputs.items()}
            self.log(f"{label}: resuming at {starts[k0] / SAMPLE_RATE:.0f}s")
        else:
            k0, base = 0, 0
            result = torch.zeros(shape, dtype=torch.float32, device=acc)
            counter = torch.zeros(counter_shape, dtype=torch.float32, device=acc)
            outs = {name: work.array(f, frames, "w+") for name, f in outputs.items()}

        def flush(n: int) -> None:
            """Divide out and write the first n samples of the buffers (song position `base`)."""
            if n <= 0:
                return
            c = counter[..., :n].clamp(min=1e-10)
            done = (result[..., :n] / c).cpu().numpy()
            for name, mm in outs.items():
                mm[base : base + n] = done[rows[name]].T

        def checkpoint(k_next: int) -> None:
            for mm in outs.values():
                mm.flush()
            name = f"{tag}-{k_next}.npz"
            np.savez(work.dir / name, result=result.cpu().numpy(), counter=counter.cpu().numpy())
            old = saved.get("window")
            work.save(**{tag: {"next": k_next, "base": base, "window": name, "rows": model_rows}})
            saved["window"] = name
            if old and old != name:
                work.unlink(old)

        need = self._chunk_need.get(model, CHUNK_NEED_DEFAULT)
        last_checkpoint = time.monotonic()
        k = k0
        attempts = 0
        with torch.no_grad():
            while k < len(starts):
                start = starts[k]
                if start > base:  # samples before `start` get nothing more: finish them, slide the window
                    shift = start - base
                    flush(shift)
                    keep = chunk - shift
                    result[..., :keep] = result[..., shift:].clone()
                    counter[..., :keep] = counter[..., shift:].clone()
                    result[..., keep:] = 0
                    counter[..., keep:] = 0
                    base = start
                HUB.check_cancel()
                self.gov.wait(need, HUB.check_cancel, lambda s: report(k / len(starts), _waiting_text(s)))
                self._limit_mps()
                self.gov.mark()
                part = mix[start : start + chunk]
                if gain != 1.0:
                    part = part * gain
                part = torch.from_numpy(np.ascontiguousarray(part.T)).to(device)
                length = part.shape[-1]
                try:
                    x = mi._run_roformer_model(part)
                    if pick is not None:
                        x = x.index_select(0, pick)  # before the copy to `acc`: only the kept rows move
                    if x.device != acc:
                        x = x.to(acc)
                except Exception as exc:
                    if not _is_oom(exc) or attempts >= OOM_RETRIES:
                        raise
                    # Out of memory inside the model: nothing was added for this chunk, so free
                    # memory, wait for more and run the same chunk again.
                    attempts += 1
                    del part
                    x = None
                    self._empty_caches()
                    need = int(need * 1.5)
                    self._chunk_need[model] = need
                    report(k / len(starts), "Not enough memory for this step: waiting and retrying...")
                    time.sleep(min(2.0 ** attempts, 30.0))
                    continue
                attempts = 0
                mi.overlap_add(result, x, window, 0, length)
                safe_len = min(length, x.shape[-1], window.shape[0])
                if safe_len > 0:
                    counter[..., :safe_len] += window[:safe_len]
                del x, part
                # learn what one chunk really costs on this machine (RAM plus Apple GPU memory)
                need = max(CHUNK_NEED_MIN, int(self.gov.drop_since_mark() * 1.25), int(need * 0.9))
                self._chunk_need[model] = need
                if self.gov.level() in ("tight", "over"):
                    self._empty_caches()
                k += 1
                report(k / len(starts), f"{label}... {k / len(starts):.0%}")
                if time.monotonic() - last_checkpoint >= CHECKPOINT_SECONDS and k < len(starts):
                    checkpoint(k)
                    last_checkpoint = time.monotonic()
            flush(frames - base)
        for mm in outs.values():
            mm.close()
        outs.clear()
        mix.close()
        if saved.get("window"):
            work.unlink(saved["window"])
        work.save(**{tag: {"done": True}})
        self._active_model = None
        memlog(f"{label} done", self.log)

    def _limit_mps(self) -> None:
        """Apple GPU: cap PyTorch at what the budget allows, so it raises (and the chunk is retried)
        instead of pushing macOS into swap. CUDA has its own VRAM; the CPU path uses the governor.
        With the limit off there is no cap."""
        torch = sys.modules.get("torch")
        try:
            if torch is None or not torch.backends.mps.is_available():
                return
            if self.gov.unlimited:
                fraction = 0.0  # PyTorch: 0 = no cap
            else:
                allowed = torch.mps.driver_allocated_memory() + max(self.gov.headroom, 0)
                fraction = min(1.0, max(0.05, allowed / torch.mps.recommended_max_memory()))
            if abs(fraction - getattr(self, "_mps_fraction", 0.0)) > 0.02:
                torch.mps.set_per_process_memory_fraction(fraction)
                self._mps_fraction = fraction
        except Exception:
            pass

    def _decode(self, input_path: Path, work: _Work) -> tuple[int, float]:
        """Decode any input to 44.1 kHz stereo float32 into the song's work folder; returns (frames, peak)."""
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path), "-vn", "-ac", "2",
               "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
        peak, size = 0.0, 0
        with tempfile.TemporaryFile() as err, open(work.path("mix"), "wb") as out:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, **subprocess_flags())
            pending = b""
            while True:
                data = proc.stdout.read(BLOCK_FRAMES * 8)
                if not data:
                    break
                out.write(data)
                size += len(data)
                data = pending + data
                whole = len(data) - len(data) % 8
                if whole:
                    peak = max(peak, float(np.max(np.abs(np.frombuffer(data[:whole], dtype=np.float32)))))
                pending = data[whole:]
                HUB.check_cancel()
            proc.stdout.close()
            if proc.wait() != 0:
                err.seek(0)
                raise RuntimeError(_ffmpeg_error(err.read()))
        frames = size // 8
        if not frames:
            raise RuntimeError(f"No audio found in {input_path.name}")
        return frames, peak

    def split(
        self, input_path: Path, opts: Options, progress: ProgressFn, cancel: Optional[threading.Event] = None,
        assemble: bool = True,
    ) -> Separated:
        """Decode and separate one song into stem files in its work folder (see write_stems).

        A song that was cancelled or interrupted (crash, power cut) continues where it stopped.
        assemble=False returns as soon as the models are done and leaves building the final stems to
        write_stems: in a batch that runs on the writer thread, so the next song reaches the GPU sooner.
        """
        t0 = time.time()
        input_path = Path(input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"File not found: {input_path}")
        HUB.cancel_event = cancel
        preset = get_preset(opts.quality)
        outputs = opts.outputs()
        # (the model's own "other" is never used: Other is rebuilt from the mix)
        names = model_stems(outputs)
        refine_vocals = bool(preset.vocal_overlap) and "Vocals" in names  # no output needs vocals: skip that model
        self._keep_only(preset)
        work = _Work(self._songs_dir, _song_key(input_path, opts))
        st = work.state
        timings: dict[str, float] = {}
        mark = time.time()

        def lap(name: str) -> None:
            nonlocal mark
            now = time.time()
            timings[name] = now - mark
            mark = now

        try:
            # Weighted stages -> one smooth overall progress value (the vocal pass is ~3/4 of the stem pass).
            if refine_vocals:
                stages = {"decode": (0.00, 0.02), "stems": (0.02, 0.55), "vocals": (0.55, 0.93), "mix": (0.93, 0.95)}
            else:
                stages = {"decode": (0.00, 0.02), "stems": (0.02, 0.93), "mix": (0.93, 0.95)}

            def stage(name: str):
                a, b = stages[name]
                return lambda frac, text: progress(a + (b - a) * max(0.0, min(1.0, frac)), text)

            # A step recorded as done whose files came out short (a power cut before the OS wrote them)
            # runs again, instead of failing on every retry.
            if st.get("frames") and not work.complete("mix", st["frames"]):
                work.forget(*list(st))
            elif st.get("assembled") and not all(work.complete(f"out_{k}", st["frames"]) for k in outputs):
                work.forget("stems", "vocals", "assembled", "peaks", "encoded")  # their inputs were dropped
            elif not st.get("assembled"):  # (after the assembly the model outputs are gone on purpose)
                if (st.get("stems") or {}).get("done") and not all(work.complete(f"sw_{k}", st["frames"])
                                                                   for k in names):
                    work.forget("stems")
                if (st.get("vocals") or {}).get("done") and not work.complete("mel_Vocals", st["frames"]):
                    work.forget("vocals")

            # 1) decode ----------------------------------------------------------------------
            if st.get("frames"):
                frames, peak = st["frames"], st["peak"]
            else:
                stage("decode")(0, "Decoding audio...")
                frames, peak = self._decode(input_path, work)
                self._check_disk(work, frames, len(names) + refine_vocals + len(outputs))
                work.save(frames=frames, peak=peak)
            memlog(f"{input_path.name} decoded", self.log)
            # Loud masters decode with peaks above 1.0. The models want a peak <= 1, so scale the
            # input down and the stems back up: "Other = mix - the rest" then stays exact. (Letting
            # the library normalize on its own left part of every other stem inside "Other".)
            gain = 1.0 / peak if peak > 1.0 else 1.0
            lap("decode")

            # 2) all stems in one pass ------------------------------------------------------------
            if not st.get("assembled"):
                if not (st.get("stems") or {}).get("done"):
                    self._stream_model(STEM_MODEL, preset.stem_overlap, work, frames, gain,
                                       {k.lower(): f"sw_{k}" for k in names}, "stems", "Separating stems",
                                       stage("stems"))
                lap("stems")

                # 3) maximum: a second, vocal-only model, averaged in below ---------------------------
                if refine_vocals and not (st.get("vocals") or {}).get("done"):
                    self._stream_model(VOCAL_MODEL, preset.vocal_overlap, work, frames, gain,
                                       {"vocals": "mel_Vocals"}, "vocals", "Refining vocals", stage("vocals"))
                    lap("vocals")

                # 4) assemble (here, or in write_stems) ---------------------------------------------
                pending = _PendingAssembly(frames, names, outputs, gain, refine_vocals)
                if not assemble:
                    return Separated(input_path, {}, SAMPLE_RATE, timings, t0, work=work, pending=pending)
                stage("mix")(0, "Assembling stems...")
                self._finish_assembly(work, pending)
            stems = {k: work.array(f"out_{k}", frames, "r") for k in outputs}
            memlog("stems assembled", self.log)
            return Separated(input_path, stems, SAMPLE_RATE, timings, t0, work=work,
                             peaks=dict(work.state.get("peaks", {})))
        finally:
            HUB.on_bar = None
            self._active_model = None

    def _check_disk(self, work: _Work, frames: int, tracks: int) -> None:
        files = 2 * (tracks + 1)  # model outputs + final stems + the mix, doubled to be generous
        need = frames * 8 * files + 200 * MB
        free = shutil.disk_usage(work.dir).free
        if free < need:
            raise RuntimeError(f"Not enough free disk space: {need / GB:.1f} GB needed in {work.dir.parent}, "
                               f"{free / GB:.1f} GB free")

    def _finish_assembly(self, work: _Work, a: _PendingAssembly,
                         check: Optional[Callable[[], None]] = None) -> None:
        """Build the final stems and drop the model outputs they came from."""
        self._assemble(work, a.frames, a.names, a.outputs, a.gain, a.maximum, check)
        work.drop(*(f"sw_{k}" for k in a.names), "mel_Vocals")

    def _assemble(self, work: _Work, frames: int, names: list[str], finals: list[str], gain: float,
                  maximum: bool, check: Optional[Callable[[], None]] = None) -> None:
        """Build the `finals` block by block (same operations, in the same order, as in memory).

        `names` are the model stems (see model_stems()). Vocals = average of the two models in
        "maximum"; every stem / gain; Other = mix - the rest; Instrumental = mix - Vocals; No
        Drums = mix - Drums. Also records each stem's peak for clipping protection.
        The inputs are not modified, so an interrupted assembly simply runs again.
        """
        mix = work.array("mix", frames, "r")
        src = {k: work.array(f"sw_{k}", frames, "r") for k in names}
        mel = work.array("mel_Vocals", frames, "r") if maximum else None
        out = {k: work.array(f"out_{k}", frames, "w+") for k in finals}
        peaks = dict.fromkeys(finals, 0.0)
        check = check or HUB.check_cancel
        for i in range(0, frames, BLOCK_FRAMES):
            j = min(frames, i + BLOCK_FRAMES)
            check()
            block = {k: np.array(src[k][i:j]) for k in names}
            if maximum:
                block["Vocals"] += mel[i:j]  # same as 0.5 * (vocals + kim)
                block["Vocals"] *= 0.5
            if gain != 1.0:
                for a in block.values():
                    a /= gain
            m = mix[i:j]
            if "Other" in finals:
                # Other = everything not claimed by another stem, so the stems add up to the song exactly.
                # Summed in the same order as sum(), so the same result (names starts with Vocals, Drums).
                other = block["Vocals"] + block["Drums"]
                for k in names[2:]:
                    other += block[k]
                np.subtract(m, other, out=other)
                block["Other"] = other
            if "Instrumental" in finals:
                block["Instrumental"] = m - block["Vocals"]
            if "No Drums" in finals:
                block["No Drums"] = m - block["Drums"]
            for k in finals:
                out[k][i:j] = block[k]
                if j > i:
                    peaks[k] = max(peaks[k], float(np.max(np.abs(block[k]))))
        for t in [*out.values(), *src.values(), mix, *([mel] if mel is not None else [])]:
            t.close()
        work.save(assembled=True, peaks=peaks, encoded=[])

    def write_stems(
        self, sep: Separated, opts: Options, progress: ProgressFn, cancel: Optional[threading.Event] = None
    ) -> Result:
        """Encode and save the stems of one song in parallel (LAME is single-threaded).

        How many run at once follows the memory governor. Safe to run on another thread while
        the next song is being separated.
        """
        t = time.time()
        if sep.pending is not None:  # split(assemble=False): build the stems here, off the GPU thread
            progress(0.93, "Assembling stems...")
            self._finish_assembly(sep.work, sep.pending, lambda: _raise_if(cancel))
            sep.stems = {k: sep.work.array(f"out_{k}", sep.pending.frames, "r") for k in sep.pending.outputs}
            sep.peaks = dict(sep.work.state.get("peaks", {}))
            sep.pending = None
            memlog("stems assembled", self.log)
        song = sep.input_path.stem
        dest = Path(opts.output_dir) / _safe_name(song)
        dest.mkdir(parents=True, exist_ok=True)
        jobs = {name: dest / f"{_safe_name(song)} - {name}.{opts.output_format}" for name in sep.stems}
        done_before = set(sep.work.state.get("encoded", [])) if sep.work is not None else set()
        todo = [n for n in jobs if not (str(jobs[n]) in done_before and jobs[n].is_file())]
        progress(0.95, "Writing files...")
        finished = len(jobs) - len(todo)
        with ThreadPoolExecutor(max_workers=max(1, min(len(jobs), os.cpu_count() or 1))) as pool:
            running: dict = {}
            queue_ = list(todo)
            while queue_ or running:
                while queue_ and len(running) < self.gov.encoder_slots(len(jobs)):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    self.gov.wait(ENCODER_NEED, lambda: _raise_if(cancel),
                                  lambda s: progress(0.95, _waiting_text(s)))
                    name = queue_.pop(0)
                    peak = sep.peaks.get(name)
                    running[pool.submit(self._write_stem, sep.stems[name], sep.sr, jobs[name], opts,
                                        f"{song} ({name})", peak, cancel)] = name
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for fut in done:
                    name = running.pop(fut)
                    fut.result()
                    finished += 1
                    if sep.work is not None:
                        with self._encoded_lock:
                            sep.work.save(encoded=sorted({*sep.work.state.get("encoded", []), str(jobs[name])}))
                    progress(0.95 + 0.05 * finished / len(jobs), f"Writing files... {finished}/{len(jobs)}")
        memlog(f"{song} written", self.log)
        for track in sep.stems.values():
            if isinstance(track, _Track):
                track.close()  # (Windows can't delete open files)
        sep.stems.clear()
        if sep.work is not None:
            sep.work.remove()
        timings = {**sep.timings, "encode": time.time() - t}
        result = Result(input_path=sep.input_path, stems=jobs, seconds=time.time() - sep.started)
        self.log(f"{song}: {result.seconds:.0f}s (" + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items()) + ")")
        progress(1.0, "Done")
        return result

    def separate(
        self, input_path: Path, opts: Options, progress: ProgressFn, cancel: Optional[threading.Event] = None
    ) -> Result:
        """split() + write_stems() for one song."""
        return self.write_stems(self.split(input_path, opts, progress, cancel), opts, progress, cancel)

    def close(self) -> None:
        if self._own_gov:
            self.gov.stop()
        self._separators.clear()
        self._empty_caches()
        shutil.rmtree(self._work_dir, ignore_errors=True)

    # -- internals ------------------------------------------------------------------------
    @staticmethod
    def _bar_tracker(
        report: Callable[[float], None], expected_bars: int, on_download: Optional[Callable[[float, float], None]] = None
    ):
        """Turn a sequence of tqdm bars into one monotonic 0..1 value."""
        state = {"bars": 0, "best": 0.0}

        def on_bar(bar: _HookTqdm, new: bool):
            if bar.total > 1_000_000:  # a model download (byte-counted), not inference
                if on_download is not None:
                    on_download(bar.fraction, bar.n / 1e6)
                return
            if new:
                state["bars"] += 1
            done = (state["bars"] - 1 + bar.fraction) / expected_bars
            done = min(0.99, max(state["best"], done))
            state["best"] = done
            report(done)

        return on_bar

    def _write_stem(self, audio: np.ndarray, sr: int, out: Path, opts: Options, title: str,
                    peak: Optional[float] = None, cancel=None) -> None:
        """Stream one stem to its file block by block (it is never copied whole into memory)."""
        if peak is None:
            peak = max((float(np.max(np.abs(audio[i : i + BLOCK_FRAMES]))) for i in range(0, len(audio), BLOCK_FRAMES)),
                       default=0.0)
        scale = 0.999 / peak if peak > 0.999 else None  # avoid clipping in the encoded file

        def blocks():
            for i in range(0, len(audio), BLOCK_FRAMES):
                _raise_if(cancel)
                block = np.ascontiguousarray(audio[i : i + BLOCK_FRAMES], dtype=np.float32)
                yield block * scale if scale is not None else block

        tmp = out.with_name(out.stem + ".part" + out.suffix)  # never leave a half-written file under the real name
        try:
            if opts.output_format == "wav":
                with sf.SoundFile(tmp, "w", sr, audio.shape[1], subtype="PCM_24", format="WAV") as f:
                    for block in blocks():
                        f.write(block)
            else:
                cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac",
                       str(audio.shape[1]), "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", f"{opts.bitrate_kbps}k",
                       "-metadata", f"title={title}", "-f", "mp3", str(tmp)]
                with tempfile.TemporaryFile() as err:
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                                            **subprocess_flags())
                    try:
                        for block in blocks():
                            proc.stdin.write(memoryview(block).cast("B"))
                        proc.stdin.close()
                    except BrokenPipeError:
                        pass
                    except BaseException:
                        proc.kill()
                        proc.wait()
                        raise
                    if proc.wait() != 0:
                        err.seek(0)
                        raise RuntimeError(_ffmpeg_error(err.read()))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, out)


def _raise_if(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _waiting_text(short: int) -> str:
    return f"Waiting for free memory ({max(short, 0) / MB:.0f} MB more needed)..."


def _ffmpeg_error(stderr: bytes) -> str:
    err = stderr.decode(errors="replace").strip().splitlines()
    return f"Could not read/write audio (ffmpeg): {err[-1] if err else 'unknown error'}"


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "song"
