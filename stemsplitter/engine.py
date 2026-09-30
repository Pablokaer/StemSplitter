"""Separation engine.

Pipeline (max quality):
  1. Decode the input (MP3, WAV, FLAC, M4A...) to 44.1 kHz stereo float WAV with ffmpeg.
  2. Vocals: MelBand-RoFormer (Kimberley Jensen) -> vocals + instrumental.
     RoFormer models are the current state of the art for vocal isolation.
  3. Instrumental -> Demucs v4 "htdemucs_ft" (fine-tuned bag of 4 models) -> drums, bass, other.
     Anything Demucs still labels "vocals" at this point is really melodic content
     (lead synths, whistles, guitar leads...) so it is folded into "other".
     (All 4 models are needed: taking that "vocals" part from the "other" specialist
     instead of the vocals specialist leaks hi-hats and cymbals into "other".)
  4. Encode each stem to MP3 (320 kbps by default).

Because every step is subtractive, Vocals + Drums + Bass + Other ~= the original mix.

Inference runs on the NVIDIA (CUDA) or Apple (MPS) GPU whenever one is present and falls
back to the CPU otherwise. On a GPU the vocal model runs in float16.
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
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

from .platform_utils import find_ffmpeg, models_dir, subprocess_flags

VOCAL_MODEL = "vocals_mel_band_roformer.ckpt"
SAMPLE_RATE = 44100

STEM_ORDER = ["Vocals", "Drums", "Bass", "Other"]
SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".wma"}


@dataclass(frozen=True)
class Preset:
    demucs_model: str
    shifts: int  # Demucs random time-shift averaging; every shift is one more full pass
    passes: int  # Demucs sub-models actually run per shift (for progress reporting)


QUALITY_PRESETS = {
    # 4 fine-tuned specialists. A second shift costs a full extra pass for very little gain.
    "maximum": Preset("htdemucs_ft.yaml", shifts=1, passes=4),
    # one general-purpose model: about 2x faster at the Demucs step, slightly more bleed
    "fast": Preset("htdemucs.yaml", shifts=1, passes=1),
}
QUALITY_ALIASES = {"high": "fast"}  # name used by older versions (saved settings, scripts)


def get_preset(quality: str) -> Preset:
    return QUALITY_PRESETS.get(QUALITY_ALIASES.get(quality, quality), QUALITY_PRESETS["maximum"])


ProgressFn = Callable[[float, str], None]  # (0..1 overall, status text)
LogFn = Callable[[str], None]


class Cancelled(Exception):
    """Raised inside the worker when the user presses Cancel."""


@dataclass
class Options:
    output_dir: Path
    bitrate_kbps: int = 320
    quality: str = "maximum"
    also_instrumental: bool = False
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
    from audio_separator.separator.uvr_lib_v5.demucs import apply as demucs_apply
    from audio_separator.separator.uvr_lib_v5.demucs import utils as demucs_utils

    sep_mod.tqdm = _HookTqdm  # model downloads
    mdxc_separator.tqdm = _HookTqdm  # RoFormer chunks
    demucs_apply.tqdm = types.SimpleNamespace(tqdm=_HookTqdm)  # Demucs segments
    demucs_utils.tqdm = types.SimpleNamespace(tqdm=_HookTqdm)
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

    def _separator(self, model: str, shifts: int, out_dir: Path):
        """Return a Separator with `model` loaded, writing float WAVs into `out_dir`."""
        _install_progress_hooks()
        _apply_thread_override()
        from audio_separator.separator import Separator

        # find_ffmpeg() already verified ffmpeg; the library's own check would flash a console on Windows.
        Separator.check_ffmpeg_installed = lambda self: None
        key = f"{model}|{shifts}"
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
                demucs_params={"segment_size": "Default", "shifts": shifts, "overlap": 0.25, "segments_enabled": True},
                # float16 RoFormer: much faster on GPU and half the VRAM. The library only
                # allows it where it is verified (CUDA / MPS RoFormer); Demucs stays float32.
                use_native_fp16=model == VOCAL_MODEL and gpu_available(),
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
    def _find_stem(folder: Path, stem: str) -> Path:
        pattern = re.compile(rf"_\({re.escape(stem)}\)_", re.IGNORECASE)
        for p in folder.glob("*.wav"):
            if pattern.search(p.name):
                return p
        raise FileNotFoundError(f"Separator did not produce a '{stem}' stem in {folder}")

    # -- public API -----------------------------------------------------------------------
    def prepare_models(
        self, progress: ProgressFn, cancel: Optional[threading.Event] = None, quality: str = "maximum"
    ) -> None:
        """Download (first run only) and load both models."""
        preset = get_preset(quality)
        shifts = preset.shifts
        HUB.cancel_event = cancel
        for i, (model, label) in enumerate([(VOCAL_MODEL, "vocal model"), (preset.demucs_model, "drums/bass model")]):
            base = i / 2

            def on_bar(bar: _HookTqdm, new: bool, base=base, label=label):
                if bar.total > 1_000_000:  # byte-counted download bar
                    mb = bar.n / 1e6
                    progress(base + bar.fraction / 2, f"Downloading {label} (first run only): {mb:.0f} MB")

            HUB.on_bar = on_bar
            progress(base, f"Loading {label}...")
            self._separator(model, shifts, self._work_dir)
        HUB.on_bar = None
        progress(1.0, "Models ready")

    def separate(
        self, input_path: Path, opts: Options, progress: ProgressFn, cancel: Optional[threading.Event] = None
    ) -> Result:
        t0 = time.time()
        input_path = Path(input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"File not found: {input_path}")
        HUB.cancel_event = cancel
        preset = get_preset(opts.quality)
        shifts = preset.shifts
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
            # Weighted stages -> one smooth overall progress value.
            stages = {"decode": (0.00, 0.02), "vocals": (0.02, 0.45), "demucs": (0.45, 0.95), "encode": (0.95, 1.0)}

            def stage_progress(name: str, frac: float, text: str):
                a, b = stages[name]
                progress(a + (b - a) * max(0.0, min(1.0, frac)), text)

            # 1) decode ----------------------------------------------------------------------
            stage_progress("decode", 0, "Decoding audio...")
            mix = job / "mix.wav"
            self._run_ffmpeg(
                ["-i", str(input_path), "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", str(mix)]
            )
            HUB.check_cancel()
            lap("decode")

            # 2) vocals ----------------------------------------------------------------------
            vocal_dir = job / "vocal_pass"
            vocal_dir.mkdir()
            HUB.on_bar = self._bar_tracker(
                lambda f: stage_progress("vocals", f, f"Isolating vocals... {f:.0%}"), expected_bars=1
            )
            stage_progress("vocals", 0, "Loading vocal model...")
            sep = self._separator(VOCAL_MODEL, shifts, vocal_dir)
            stage_progress("vocals", 0, "Isolating vocals...")
            sep.separate(str(mix))
            vocals_wav = self._find_stem(vocal_dir, "Vocals")
            inst_wav = (
                self._find_stem(vocal_dir, "Other")
                if self._has_stem(vocal_dir, "Other")
                else self._find_stem(vocal_dir, "Instrumental")
            )
            HUB.check_cancel()
            lap("vocals")

            # 3) drums / bass / other ------------------------------------------------------------
            inst_in = job / "inst.wav"
            shutil.move(str(inst_wav), inst_in)
            demucs_dir = job / "demucs_pass"
            demucs_dir.mkdir()
            HUB.on_bar = self._bar_tracker(
                lambda f: stage_progress("demucs", f, f"Separating drums, bass & other... {f:.0%}"),
                expected_bars=preset.passes * max(1, shifts),
            )
            stage_progress("demucs", 0, "Loading drums/bass model...")
            sep = self._separator(preset.demucs_model, shifts, demucs_dir)
            stage_progress("demucs", 0, "Separating drums, bass & other...")
            sep.separate(str(inst_in))
            HUB.on_bar = None
            lap("demucs")

            # 4) assemble + encode ---------------------------------------------------------------
            stage_progress("encode", 0, "Writing files...")
            vocals, sr = sf.read(vocals_wav, dtype="float32", always_2d=True)
            drums, _ = sf.read(self._find_stem(demucs_dir, "Drums"), dtype="float32", always_2d=True)
            bass, _ = sf.read(self._find_stem(demucs_dir, "Bass"), dtype="float32", always_2d=True)
            other, _ = sf.read(self._find_stem(demucs_dir, "Other"), dtype="float32", always_2d=True)
            residue, _ = sf.read(self._find_stem(demucs_dir, "Vocals"), dtype="float32", always_2d=True)
            n = min(len(vocals), len(drums), len(bass), len(other), len(residue))
            stems = {
                "Vocals": vocals[:n],
                "Drums": drums[:n],
                "Bass": bass[:n],
                "Other": other[:n] + residue[:n],
            }
            if opts.also_instrumental:
                inst, _ = sf.read(inst_in, dtype="float32", always_2d=True)
                stems["Instrumental"] = inst[:n]

            dest = Path(opts.output_dir) / _safe_name(song)
            dest.mkdir(parents=True, exist_ok=True)
            result = Result(input_path=input_path)
            for i, (name, audio) in enumerate(stems.items()):
                HUB.check_cancel()
                out = dest / f"{_safe_name(song)} - {name}.{opts.output_format}"
                self._write_stem(audio, sr, out, opts, title=f"{song} ({name})")
                result.stems[name] = out
                stage_progress("encode", (i + 1) / len(stems), f"Writing {name}...")

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
    def _has_stem(folder: Path, stem: str) -> bool:
        return any(f"_({stem.lower()})_" in p.name.lower() for p in folder.glob("*.wav"))

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
