"""Separation engine.

Pipeline (everything stays in memory; no intermediate files):
  1. ffmpeg decodes the input (MP3, WAV, FLAC, M4A...) to 44.1 kHz stereo float32 via a pipe.
  2. BS-RoFormer SW (jarredou) splits the mix in one pass into vocals, drums, bass,
     guitar, piano and other.
  3. "maximum" only: MelBand-RoFormer (Kimberley Jensen) also isolates the vocals and the
     two vocal estimates are averaged (ensembling two strong models beats either one).
  4. Other = mix - (vocals + drums + bass [+ guitar + piano]), so the stems always add up to
     the original song exactly. Then all stems are encoded to MP3 (320 kbps by default) in
     parallel. split() and write_stems() are separate so a batch can encode one song while
     the next one is on the GPU.

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

from .memory import memlog
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


@dataclass
class Separated:
    """The stems of one song, still in memory (samples x channels float32), ready to write."""

    input_path: Path
    stems: dict[str, np.ndarray]
    sr: int
    timings: dict[str, float]
    started: float


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
    """Keeps models loaded between songs so batch processing is faster.

    One instance per model: the overlap is only read when demix() runs, so the presets share it.
    """

    def __init__(self, log: Optional[LogFn] = None) -> None:
        self.log = log or (lambda msg: None)
        self.ffmpeg = find_ffmpeg()
        self._separators: dict[str, object] = {}  # model file -> Separator
        self._work_dir = Path(tempfile.mkdtemp(prefix="stemsplitter_"))
        atexit.register(shutil.rmtree, self._work_dir, True)
        self._log_handler: Optional[logging.Handler] = None

    # -- helpers ------------------------------------------------------------------------
    def _run_ffmpeg(self, args: list[str], stdin=None) -> bytes:
        """Run ffmpeg; returns its stdout (the decoded audio when the output is "-")."""
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
        proc = subprocess.run(cmd, input=stdin, capture_output=True, **subprocess_flags())
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip().splitlines()
            raise RuntimeError(f"Could not read/write audio (ffmpeg): {err[-1] if err else 'unknown error'}")
        return proc.stdout

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
        sep = self._separators.get(model)
        if sep is None:
            self._place_on_gpu(None)  # load next to the other models in RAM, not on top of them in VRAM
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
            self._separators[model] = sep
            memlog(f"{model} loaded", self.log)
        sep.output_dir = str(out_dir)
        if getattr(sep, "model_instance", None) is not None:
            sep.model_instance.output_dir = str(out_dir)
            sep.model_instance.overlap = overlap
        return sep

    def _place_on_gpu(self, active: Optional[str]) -> None:
        """CUDA: keep only the `active` model in VRAM; the others wait in RAM (None: all in RAM).

        Only "maximum" has two models. Both on a 4 GB GPU overflow its memory and Windows then
        spills into shared system RAM, which made the split several times slower; moving a model
        between RAM and VRAM takes a fraction of a second. demix() runs on wherever the model is.
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

    def _keep_only(self, preset: Preset) -> None:
        """Unload models the preset doesn't use (e.g. the vocal model after leaving "maximum")."""
        wanted = {model for model, _, _ in self._models_for(preset)}
        stale = [m for m in self._separators if m not in wanted]
        if not stale:
            return
        for m in stale:
            del self._separators[m]
        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
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
            self._separator(model, overlap, self._work_dir)
        HUB.on_bar = None
        progress(1.0, "Models ready")

    def _run_model(
        self, model: str, overlap: int, mix: np.ndarray, label: str, report, keep: set[str]
    ) -> dict[str, np.ndarray]:
        """Run one model on `mix` (samples x channels, peak <= 1) in memory.

        Returns {stem name (lower case): samples x channels} for the stems in `keep` only: the
        model's outputs are views of one big array, so copying just the needed ones and dropping
        the rest frees it right away. Calling the model's demix()
        directly skips the library's WAV round trip (write the mix, read it back with librosa,
        write every stem, read them back): ~5 s and ~0.8 GB of disk traffic per song.
        """

        def downloading(frac: float, mb: float) -> None:
            report(0, f"Downloading model (first run only): {mb:.0f} MB ({frac:.0%})")

        HUB.on_bar = self._bar_tracker(lambda f: report(f, f"{label}... {f:.0%}"), 1, downloading)
        report(0, "Loading model...")
        mi = self._separator(model, overlap, self._work_dir).model_instance
        self._place_on_gpu(model)
        report(0, f"{label}...")
        # same rule the library applies in separate(): very short clips use the configured segment size
        use_override = getattr(mi, "_use_model_segment_override", lambda seconds: False)
        out = mi.demix(np.ascontiguousarray(mix.T), override_model_segment_size=use_override(len(mix) / SAMPLE_RATE))
        HUB.on_bar = None
        HUB.check_cancel()
        if isinstance(out, np.ndarray):  # single-target model without a residual
            out = {"vocals": out}
        memlog(f"{label} done", self.log)
        return {k.lower(): np.ascontiguousarray(v.T, dtype=np.float32) for k, v in out.items() if k.lower() in keep}

    def _decode(self, input_path: Path) -> np.ndarray:
        """Decode any input to 44.1 kHz stereo float32 (samples x 2), piped straight into memory."""
        raw = self._run_ffmpeg(["-i", str(input_path), "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"])
        mix = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)
        if not len(mix):
            raise RuntimeError(f"No audio found in {input_path.name}")
        return mix

    def split(
        self, input_path: Path, opts: Options, progress: ProgressFn, cancel: Optional[threading.Event] = None
    ) -> Separated:
        """Decode and separate one song; the stems stay in memory (see write_stems)."""
        t0 = time.time()
        input_path = Path(input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"File not found: {input_path}")
        HUB.cancel_event = cancel
        preset = get_preset(opts.quality)
        self._keep_only(preset)
        timings: dict[str, float] = {}
        mark = time.time()

        def lap(name: str) -> None:
            nonlocal mark
            now = time.time()
            timings[name] = now - mark
            mark = now

        try:
            # Weighted stages -> one smooth overall progress value (the vocal pass is ~3/4 of the stem pass).
            if preset.vocal_overlap:
                stages = {"decode": (0.00, 0.02), "stems": (0.02, 0.55), "vocals": (0.55, 0.95)}
            else:
                stages = {"decode": (0.00, 0.02), "stems": (0.02, 0.95)}

            def stage(name: str):
                a, b = stages[name]
                return lambda frac, text: progress(a + (b - a) * max(0.0, min(1.0, frac)), text)

            # 1) decode ----------------------------------------------------------------------
            stage("decode")(0, "Decoding audio...")
            mix = self._decode(input_path)
            memlog(f"{input_path.name} decoded", self.log)
            HUB.check_cancel()
            # Loud masters decode with peaks above 1.0. The models want a peak <= 1, so scale the
            # input down and the stems back up: "Other = mix - the rest" then stays exact. (Letting
            # the library normalize on its own left part of every other stem inside "Other".)
            peak = float(np.max(np.abs(mix)))
            gain = 1.0 / peak if peak > 1.0 else 1.0
            model_in = mix * gain if gain != 1.0 else mix
            lap("decode")

            # 2) all stems in one pass ------------------------------------------------------------
            # (the model's own "other" is never used: Other is rebuilt below from the mix)
            names = ["Vocals", "Drums", "Bass", *(EXTRA_STEMS if opts.guitar_piano else [])]
            sw = self._run_model(STEM_MODEL, preset.stem_overlap, model_in, "Separating stems", stage("stems"),
                                 keep={k.lower() for k in names})
            lap("stems")

            # 3) maximum: average with a second, vocal-only model -----------------------------------
            if preset.vocal_overlap:
                kim = self._run_model(VOCAL_MODEL, preset.vocal_overlap, model_in, "Refining vocals",
                                      stage("vocals"), keep={"vocals"})["vocals"]
                n = min(len(sw["vocals"]), len(kim))
                vocals = sw["vocals"][:n]
                vocals += kim[:n]  # in place: same result as 0.5 * (vocals + kim), no temporaries
                vocals *= 0.5
                sw["vocals"] = vocals
                del kim
                lap("vocals")
            del model_in

            # 4) assemble ---------------------------------------------------------------------------
            # Every array in `sw` belongs to this function, so it is trimmed and rescaled in place.
            n = min(len(mix), *(len(a) for a in sw.values()))
            stems = {k: sw.pop(k.lower())[:n] for k in names}
            if gain != 1.0:
                for a in stems.values():
                    a /= gain
            # Other = everything not claimed by another stem, so the stems add up to the song exactly.
            # Summed into one buffer (same order as sum(), so the same result) that then holds Other.
            other = stems["Vocals"] + stems["Drums"]
            for k in names[2:]:
                other += stems[k]
            np.subtract(mix[:n], other, out=other)
            stems["Other"] = other
            stems = {k: stems[k] for k in [*STEM_ORDER, *EXTRA_STEMS] if k in stems}
            if opts.also_instrumental:
                stems["Instrumental"] = mix[:n] - stems["Vocals"]
            del mix
            memlog("stems assembled", self.log)
            return Separated(input_path, stems, SAMPLE_RATE, timings, t0)
        finally:
            HUB.on_bar = None

    def write_stems(
        self, sep: Separated, opts: Options, progress: ProgressFn, cancel: Optional[threading.Event] = None
    ) -> Result:
        """Encode and save the stems of one song, all in parallel (LAME is single-threaded).

        Safe to run on another thread while the next song is being separated.
        """
        t = time.time()
        song = sep.input_path.stem
        dest = Path(opts.output_dir) / _safe_name(song)
        dest.mkdir(parents=True, exist_ok=True)
        jobs = {name: dest / f"{_safe_name(song)} - {name}.{opts.output_format}" for name in sep.stems}
        progress(0.95, "Writing files...")
        with ThreadPoolExecutor(max_workers=min(len(jobs), os.cpu_count() or 1)) as pool:
            futures = {}
            for name, out in jobs.items():
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                futures[pool.submit(self._write_stem, sep.stems[name], sep.sr, out, opts, f"{song} ({name})")] = name
            for i, fut in enumerate(as_completed(futures)):
                fut.result()
                progress(0.95 + 0.05 * (i + 1) / len(futures), f"Writing files... {i + 1}/{len(futures)}")
        memlog(f"{song} written", self.log)
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
        self._separators.clear()
        gc.collect()
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

    def _write_stem(self, audio: np.ndarray, sr: int, out: Path, opts: Options, title: str) -> None:
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if peak > 0.999:  # avoid clipping in the encoded file
            if audio.flags.writeable:
                audio *= 0.999 / peak  # in place: the stem is not needed after this
            else:
                audio = audio * (0.999 / peak)
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
            stdin=memoryview(audio).cast("B"),  # the samples' own buffer, no tobytes() copy
        )


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "song"
