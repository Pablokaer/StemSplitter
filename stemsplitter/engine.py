"""Separation engine.

Pipeline:
  1. Decode the input (MP3, WAV, FLAC, M4A...) to 44.1 kHz stereo float WAV with ffmpeg.
  2. BS-RoFormer SW (jarredou) splits the mix in one pass into vocals, drums, bass,
     guitar, piano and other.
  3. "maximum" only: MelBand-RoFormer (Kimberley Jensen) also isolates the vocals and the
     two vocal estimates are averaged (ensembling two strong models beats either one).
  4. Other = mix - (vocals + drums + bass [+ guitar + piano]), so the stems always add up to
     the original song exactly. Then each stem is encoded to MP3 (320 kbps by default).

Measured on the MUSDB18 test set (50 songs, median SDR in dB, higher is better):

  preset     vocals drums  bass  other   avg   speed vs. the old RoFormer + htdemucs_ft
  maximum     12.03 11.38  9.58   8.04  10.26  ~0.75x
  balanced    11.83 11.38  9.58   8.03  10.20  ~1.35x
  fast        11.54 10.92  9.11   7.69   9.81  ~2.4x
  (old)       11.30  9.77  8.76   6.72   9.14

Inference runs on the NVIDIA (CUDA) or Apple (MPS) GPU whenever one is present and falls
back to the CPU otherwise. On a GPU both models run in float16.
"""

from __future__ import annotations

import atexit
import logging
import os

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # Apple Silicon: fall back to CPU for rare ops

from .platform_utils import app_data_dir  # noqa: E402

# librosa's numba functions use cache=True; inside a packaged app the package folder is read-only.
os.environ.setdefault("NUMBA_CACHE_DIR", str(app_data_dir() / "numba_cache"))

import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

from .platform_utils import find_ffmpeg, models_dir, subprocess_flags

STEM_MODEL = "BS-Roformer-SW.ckpt"  # 6 stems: bass, drums, other, vocals, guitar, piano
VOCAL_MODEL = "vocals_mel_band_roformer.ckpt"
SAMPLE_RATE = 44100

STEM_ORDER = ["Vocals", "Drums", "Bass", "Other"]
EXTRA_STEMS = ["Guitar", "Piano"]
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
    also_instrumental: bool = False
    guitar_piano: bool = False  # also split Guitar and Piano out of Other
    output_format: str = "mp3"  # "mp3" or "wav"


@dataclass
class Result:
    input_path: Path
    stems: dict[str, Path] = field(default_factory=dict)
    seconds: float = 0.0


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
class StemEngine:
    """Keeps models loaded between songs so batch processing is faster."""

    def __init__(self, log: Optional[LogFn] = None) -> None:
        self.log = log or (lambda msg: None)
        self.ffmpeg = find_ffmpeg()
        self._separators: dict[str, object] = {}
        self._work_dir = Path(tempfile.mkdtemp(prefix="stemsplitter_"))
        atexit.register(shutil.rmtree, self._work_dir, True)
        self._log_handler: Optional[logging.Handler] = None

    # -- helpers ------------------------------------------------------------------------
    def _run_ffmpeg(self, args: list[str], stdin: Optional[bytes] = None) -> None:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
        proc = subprocess.run(cmd, input=stdin, capture_output=True, **subprocess_flags())
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip().splitlines()
            raise RuntimeError(f"Could not read/write audio (ffmpeg): {err[-1] if err else 'unknown error'}")

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

    def _separator(self, model: str, overlap: int, out_dir: Path):
        """Return a Separator with `model` loaded, writing float WAVs into `out_dir`."""
        _install_progress_hooks()
        _apply_thread_override()
        from audio_separator.separator import Separator

        # find_ffmpeg() already verified ffmpeg; the library's own check would flash a console on Windows.
        Separator.check_ffmpeg_installed = lambda self: None
        key = f"{model}|{overlap}"
        sep = self._separators.get(key)
        if sep is None:
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
            sep.load_model(model_filename=model)
            self._separators[key] = sep
        sep.output_dir = str(out_dir)
        if getattr(sep, "model_instance", None) is not None:
            sep.model_instance.output_dir = str(out_dir)
        return sep

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
        models = self._models_for(get_preset(quality))
        for i, (model, overlap, label) in enumerate(models):
            base, span = i / len(models), 1 / len(models)

            def on_bar(bar: _HookTqdm, new: bool, base=base, span=span, label=label):
                if bar.total > 1_000_000:  # byte-counted download bar
                    mb = bar.n / 1e6
                    progress(base + bar.fraction * span, f"Downloading {label} (first run only): {mb:.0f} MB")

            HUB.on_bar = on_bar
            progress(base, f"Loading {label}...")
            self._separator(model, overlap, self._work_dir)
        HUB.on_bar = None
        progress(1.0, "Models ready")

    def _run_model(self, model: str, overlap: int, mix: Path, out_dir: Path, label: str, report) -> dict[str, np.ndarray]:
        """Run one model on `mix`; returns {stem name (lower case): audio}."""
        out_dir.mkdir()
        HUB.on_bar = self._bar_tracker(lambda f: report(f, f"{label}... {f:.0%}"), expected_bars=1)
        report(0, "Loading model (the first run downloads it)...")
        sep = self._separator(model, overlap, out_dir)
        report(0, f"{label}...")
        sep.separate(str(mix))
        HUB.on_bar = None
        HUB.check_cancel()
        stems = {}
        for p in out_dir.glob("*.wav"):
            m = re.search(r"_\(([^)]+)\)_", p.name)
            if m:
                stems[m.group(1).lower()] = sf.read(p, dtype="float32", always_2d=True)[0]
        return stems

    def separate(
        self, input_path: Path, opts: Options, progress: ProgressFn, cancel: Optional[threading.Event] = None
    ) -> Result:
        t0 = time.time()
        input_path = Path(input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"File not found: {input_path}")
        HUB.cancel_event = cancel
        preset = get_preset(opts.quality)
        song = input_path.stem
        timings: dict[str, float] = {}
        mark = time.time()

        def lap(name: str) -> None:
            nonlocal mark
            now = time.time()
            timings[name] = now - mark
            mark = now

        job = Path(tempfile.mkdtemp(prefix="job_", dir=self._work_dir))
        try:
            # Weighted stages -> one smooth overall progress value (the vocal pass is ~3/4 of the stem pass).
            if preset.vocal_overlap:
                stages = {"decode": (0.00, 0.02), "stems": (0.02, 0.55), "vocals": (0.55, 0.95), "encode": (0.95, 1.0)}
            else:
                stages = {"decode": (0.00, 0.02), "stems": (0.02, 0.95), "encode": (0.95, 1.0)}

            def stage(name: str):
                a, b = stages[name]
                return lambda frac, text: progress(a + (b - a) * max(0.0, min(1.0, frac)), text)

            # 1) decode ----------------------------------------------------------------------
            stage("decode")(0, "Decoding audio...")
            mix_wav = job / "mix.wav"
            self._run_ffmpeg(
                ["-i", str(input_path), "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", str(mix_wav)]
            )
            mix, sr = sf.read(mix_wav, dtype="float32", always_2d=True)
            HUB.check_cancel()
            lap("decode")

            # 2) all stems in one pass ------------------------------------------------------------
            sw = self._run_model(STEM_MODEL, preset.stem_overlap, mix_wav, job / "stems", "Separating stems", stage("stems"))
            lap("stems")
            vocals = sw["vocals"]

            # 3) maximum: average with a second, vocal-only model -----------------------------------
            if preset.vocal_overlap:
                kim = self._run_model(
                    VOCAL_MODEL, preset.vocal_overlap, mix_wav, job / "vocals", "Refining vocals", stage("vocals")
                )
                n = min(len(vocals), len(kim["vocals"]))
                vocals = 0.5 * (vocals[:n] + kim["vocals"][:n])
                lap("vocals")

            # 4) assemble + encode ---------------------------------------------------------------
            encode = stage("encode")
            encode(0, "Writing files...")
            named = {"Vocals": vocals, "Drums": sw["drums"], "Bass": sw["bass"]}
            if opts.guitar_piano:
                named.update(Guitar=sw["guitar"], Piano=sw["piano"])
            n = min(len(mix), *(len(a) for a in named.values()))
            stems = {k: a[:n] for k, a in named.items()}
            # Other = everything not claimed by another stem, so the stems add up to the song exactly.
            stems["Other"] = mix[:n] - sum(stems.values())
            stems = {k: stems[k] for k in [*STEM_ORDER, *EXTRA_STEMS] if k in stems}
            if opts.also_instrumental:
                stems["Instrumental"] = mix[:n] - stems["Vocals"]

            dest = Path(opts.output_dir) / _safe_name(song)
            dest.mkdir(parents=True, exist_ok=True)
            result = Result(input_path=input_path)
            for i, (name, audio) in enumerate(stems.items()):
                HUB.check_cancel()
                out = dest / f"{_safe_name(song)} - {name}.{opts.output_format}"
                self._write_stem(audio, sr, out, opts, title=f"{song} ({name})")
                result.stems[name] = out
                encode((i + 1) / len(stems), f"Writing {name}...")

            lap("encode")
            result.seconds = time.time() - t0
            self.log(f"{song}: {result.seconds:.0f}s (" + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items()) + ")")
            progress(1.0, "Done")
            return result
        finally:
            HUB.on_bar = None
            shutil.rmtree(job, ignore_errors=True)

    def close(self) -> None:
        self._separators.clear()
        shutil.rmtree(self._work_dir, ignore_errors=True)

    # -- internals ------------------------------------------------------------------------
    @staticmethod
    def _bar_tracker(report: Callable[[float], None], expected_bars: int):
        """Turn a sequence of tqdm bars into one monotonic 0..1 value."""
        state = {"bars": 0, "best": 0.0}

        def on_bar(bar: _HookTqdm, new: bool):
            if bar.total > 1_000_000:  # a model download, not inference
                return
            if new:
                state["bars"] += 1
            done = (state["bars"] - 1 + bar.fraction) / expected_bars
            done = min(0.99, max(state["best"], done))
            state["best"] = done
            report(done)

        return on_bar

    def _write_stem(self, audio: np.ndarray, sr: int, out: Path, opts: Options, title: str) -> None:
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0.999:  # avoid clipping in the encoded file
            audio = audio * (0.999 / peak)
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if opts.output_format == "wav":
            sf.write(out, audio, sr, subtype="PCM_24")
            return
        self._run_ffmpeg(
            [
                "-f",
                "f32le",
                "-ar",
                str(sr),
                "-ac",
                str(audio.shape[1]),
                "-i",
                "pipe:0",
                "-c:a",
                "libmp3lame",
                "-b:a",
                f"{opts.bitrate_kbps}k",
                "-metadata",
                f"title={title}",
                str(out),
            ],
            stdin=audio.tobytes(),
        )


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "song"
