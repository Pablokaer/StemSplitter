# StemSplitter: full documentation

End-to-end documentation of the project: what the app does, how it works inside, how much quality it delivers, how fast it is and why, how it is packaged, and how to keep developing it.

The [README](../README.md) is the quick guide for people who just want to download and use the app. This document is the technical reference.

## Contents

1. [Overview](#1-overview)
2. [Features](#2-features)
3. [Separation quality](#3-separation-quality)
4. [Performance and optimizations](#4-performance-and-optimizations)
5. [Architecture](#5-architecture)
6. [Platforms, packaging and CI](#6-platforms-packaging-and-ci)
7. [Development](#7-development)
8. [Known limitations](#8-known-limitations)
9. [Change history](#9-change-history)

---

## 1. Overview

StemSplitter is a desktop app for **Windows and macOS** that splits a song into individual tracks (*stems*) using AI models:

| Stem | Contents |
|---|---|
| **Vocals** | Lead and backing vocals |
| **Drums** | Drum kit and percussion |
| **Bass** | Bass guitar, synth bass, 808 |
| **Other** | Everything else: melody and harmony (guitars, keys, synths, strings…) |
| **Guitar** and **Piano** (optional) | Split out of Other, for 6 stems |
| **Instrumental** (optional) | Everything except the vocals |

In short:

- **Input:** MP3, WAV, FLAC, M4A, AAC, OGG, Opus, AIFF or WMA. Single files or whole folders.
- **Output:** one file per stem, as MP3 (320, 256 or 192 kbps) or 24-bit WAV, in one subfolder per song (`Song/Song - Vocals.mp3`, …).
- **Engine:** BS-RoFormer SW (all stems in one pass) and, in the *Maximum* preset, also MelBand-RoFormer for the vocals. The models run on PyTorch, on the NVIDIA (CUDA) or Apple (Metal) GPU, with an automatic fallback to the CPU.
- **Interfaces:** a desktop window (PySide6/Qt), a command line (`--cli`) and a self-test (`--selftest`) used by CI.

---

## 2. Features

### 2.1 Desktop window

- **Drag and drop** files or folders. Folders are scanned recursively and only supported formats are added. Duplicate files are ignored.
- **Add files…**, **Remove selected** and **Clear list** buttons.
- **Queue with a status per song:** ⏳ queued, ▶️ processing (with %), ✅ done (with the time taken), ⚠️ error (with the message), ⏹ cancelled.
- **Retry only what failed:** pressing *Split stems* again only picks up queued or failed songs.
- **Preloaded model:** when songs are added, PyTorch and the preset's model start loading in the background, so *Split stems* starts almost at once.
- **Robust batches:** a broken file fails on its own and the rest of the batch keeps going.
- **Progress bar and status:** shows the stage (decoding, model download with MB and %, separation, writing) and the overall progress.
- **Device in use** at the bottom, for example "NVIDIA GPU · RTX 3050 Laptop GPU", "Apple Silicon GPU (Metal)" or "CPU · 12 threads".
- **Log** that can be shown or hidden, with the time of every stage per song. It opens by itself when there is an error.
- **Cancel:** stops at the next processed block of audio. Closing the window in the middle of a job asks for confirmation.
- **Open output folder** with one click.
- **Settings remembered between sessions:** output folder, quality (saved by preset name), format, Instrumental, Guitar/Piano and the last input folder.
- **Theme:** Fusion style with an accent color; on macOS it follows the system dark mode.
- **Help → About** menu with the versions and the models in use.

### 2.2 Quality presets

| Preset | What runs | For whom |
|---|---|---|
| **Balanced** (default) | BS-RoFormer SW with overlap 2 | The best quality/time trade-off |
| **Maximum** | SW (overlap 2) + MelBand-RoFormer (overlap 2), with the two vocal estimates averaged | The cleanest possible vocals; ~1.8× slower (measured before the one-model-at-a-time VRAM change; see [4.1](#41-current-timings)) |
| **Fast** | SW with overlap 1 | ~1.8× faster; still better than the old version of the app at its best setting |

The old name `high` is still accepted, as an alias of `balanced`.

### 2.3 Output options

- **Format:** MP3 320/256/192 kbps (LAME) or 24-bit PCM WAV. Every MP3 gets the title `Song (Stem)` in its metadata.
- **Instrumental:** also writes `mix − vocals`.
- **Guitar and Piano:** takes guitar and piano out of Other at no extra time, because the model already produces those stems in the same pass.
- **Clipping protection:** if a stem's peak goes above 0.999, only that file is turned down just enough not to distort.

### 2.4 Command line

```bash
python main.py --cli song1.mp3 song2.flac -o output_folder \
    -q balanced|maximum|fast  -b 320  --wav  --instrumental  --guitar-piano
```

It prints the device, the progress and where each stem was written. With several songs, one song is written while the next one is separated (see [section 4.3](#43-optimizations-in-place)).

### 2.5 Models and user data

- **The models download themselves on first use,** with visible progress: ~0.7 GB for SW, plus ~0.9 GB for MelBand, only if *Maximum* is used.
- **Where they live:** `%LOCALAPPDATA%\StemSplitter\models` on Windows and `~/Library/Application Support/StemSplitter/models` on macOS.
- **In the same data folder:** `stemsplitter.log` (output of the builds without a console), `selftest.txt` (self-test result), `numba_cache` (librosa's cache) and `bin/` (a copy of ffmpeg when the app runs from source).

### 2.6 Hardware selection

- **NVIDIA (CUDA):** the default Windows build ships PyTorch with CUDA 13.0 and uses the GPU automatically.
- **Apple Silicon (Metal/MPS):** used automatically on the Mac. Rare operations without MPS support fall back to the CPU (`PYTORCH_ENABLE_MPS_FALLBACK=1`).
- **CPU:** used only when there is no compatible GPU.
- **`STEMSPLITTER_THREADS=N`:** pins the number of CPU threads, for testing.
- **`STEMSPLITTER_MEMLOG=1`:** logs the RAM (and the VRAM, once PyTorch is loaded) at every stage: worker started, audio decoded, model loaded, inference done, stems assembled, files written, queue finished and worker stopped.

---

## 3. Separation quality

### 3.1 Models

| Model | Author | Role | Size |
|---|---|---|---|
| **BS-RoFormer SW** (`BS-Roformer-SW.ckpt`) | jarredou | Splits the 6 stems (bass, drums, other, vocals, guitar, piano) in one pass | 0.7 GB |
| **MelBand-RoFormer** (`vocals_mel_band_roformer.ckpt`) | Kimberley Jensen | Second vocal model, *Maximum* only | 0.9 GB |

Both are RoFormer architectures (Transformers with *rotary embeddings* over the spectrogram), the state of the art in music source separation. They are loaded through the [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator) 0.47.0 library.

**SW inference settings:**
- 44.1 kHz, stereo.
- 2048-point STFT with a hop of 512.
- Blocks of 409,600 samples (9.29 s) with a Hamming window and *overlap-add*.
- Batch 1.
- fp16 on the GPU, fp32 on the CPU.
- No *shifts*: that concept belongs to Demucs and doesn't exist here.

### 3.2 How quality was measured

- **Data:** the 50 test excerpts of the public **MUSDB18** sample (7 s each, ~340 s in total). Each excerpt comes with its original stems, which serve as the ground truth.
- **Metric:** global **SDR** (*signal-to-distortion ratio*) per excerpt, `10·log10(‖ref‖² / ‖ref − estimate‖²)`, and the median over the 50. It is the same kind of metric used by the field's leaderboards (MDX, MVSep). In dB; higher is better, and +1 dB is already an audible difference.
- **How it ran:** through the app itself (`engine.split`), on an RTX 3050 Laptop.

### 3.3 Results

| Setup | Vocals | Drums | Bass | Other | **Average** |
|---|---|---|---|---|---|
| **Maximum** | **12.03** | 11.38 | 9.58 | **8.04** | **10.26** |
| **Balanced** | 11.83 | 11.38 | 9.58 | 8.03 | **10.20** |
| **Fast** | 11.54 | 10.92 | 9.11 | 7.69 | **9.81** |
| Previous version (MelBand-RoFormer + Demucs `htdemucs_ft`) | 11.30 | 9.77 | 8.76 | 6.72 | 9.14 |

Other variants tested, for reference (same metric):

| Variant | Average | Conclusion |
|---|---|---|
| SW with overlap 4 | 10.22 | +0.02 dB for twice the time: not worth it |
| Training-size blocks (13.35 s) | 10.22 | +0.02 dB: not worth it |
| SW with the model's own Other instead of the residual | 10.20 | the residual ties or wins, and also guarantees the exact sum |

### 3.4 Decisions that protect quality

- **The stems add up to the song exactly.** Other is computed as `mix − (vocals + drums + bass [+ guitar + piano])`. Measured: ~113 dB of reconstruction with 24-bit WAV, which is the limit of 24-bit precision itself. The old version reached ~16 dB. The only exception is the clipping protection (section 2.3): when it turns a file down, the sum comes out slightly lower; on a very loud song we measured 64.7 dB, still inaudible.
- **Loudly mastered songs.** Loud MP3s decode with peaks above 1.0; the Daft Punk song used in the tests reaches 1.066. The model needs a peak ≤ 1, so the app turns the input down and **applies the inverse gain to the stems**. Before that, the library normalized on its own and part of every stem ended up in Other. On a test with a peak of ~2, the average dropped from 10.20 to **3.95 dB**; with the fix it is back to **10.20 dB**.
- **Vocal ensemble in *Maximum*.** Averaging two strong models gives +0.2 dB on the vocals.
- **fp16 on the GPU.** The difference from fp32 sits ~40 dB below the signal, inaudible.
- **No reduction to 16 bits between stages.** All processing is float32, and only the final file is encoded.

### 3.5 Dropped because it cost quality

- **Skipping models of `htdemucs_ft`** (in the Demucs version): computing Other by subtraction put +4.7 dB of sub-bass into it, and removing the vocal specialist put +9.7 dB of highs (hi-hats and cymbals) into it. Reverted at the time.

---

## 4. Performance and optimizations

### 4.1 Current timings

Laptop with an Intel i5-12500H and an NVIDIA RTX 3050 Laptop (4 GB). 4-minute song:

| Hardware | Fast | Balanced | Maximum |
|---|---|---|---|
| NVIDIA GPU (RTX 3050 Laptop) | ~30 s | ~1 min | ~1 min 45 s |
| CPU only (12 cores, laptop) | ~20 min | ~28 min | slower still |

Measured directly: a 5:20 song in Balanced takes **77 s** with the model already loaded, and a batch of 2 songs takes **156 s**. Loading the model costs ~3 s and, since the preloading, usually happens before *Split stems* is pressed. Apple Silicon timings have not been measured yet.

The *Maximum* time was measured before it started keeping only one model at a time in VRAM ([section 4.5](#45-scalability-and-memory-use)) and should now be lower. On a 60 s clip it now takes ~1.5× the Balanced time, but a full song still has to be measured.

### 4.2 Where the time goes (profiling)

Per-stage profiling, with the GPU synchronized, on a 5:20 song in Balanced:

| Stage | Before the optimizations | After |
|---|---|---|
| Model forward pass (69 blocks) | 72.8 s (78%) | ~69 s (~90%) |
| MP3 encoding of the 4 stems | 14.8 s (16%, one at a time) | ~4.5 s (in parallel) |
| Intermediate WAVs (write, read back, normalize) | ~3.7 s | 0 (all in memory) |
| MP3 decoding | 0.4 s | 0.4–0.6 s |
| **Total** | **94 s** | **77 s** |

**Measured resources:** RAM peak of ~3.1 GiB; VRAM allocated by PyTorch of ~0.9 GiB (~2 GiB reserved); GPU ~87–90% busy; CPU at 25–40% on average.

Inference is **bound by the GPU computation itself**, not by Python overhead: the cost is linear (~113 ms per second of audio per overlap pass) and the GPU is only idle while writing. That is why the preset is the main speed control.

### 4.3 Optimizations in place

In chronological order, with the measured gain:

| # | Optimization | Gain |
|---|---|---|
| 1 | **GPU by default:** the Windows build became CUDA, with a fallback to the CPU | around 20× (Balanced: ~1 min on the RTX 3050 versus ~28 min on the CPU) |
| 2 | **Native fp16 in the RoFormers** on the GPU | more speed and half the VRAM |
| 3 | `shifts` 2 → 1 in Demucs (old version) | 2× in Demucs |
| 4 | **Demucs replaced by BS-RoFormer SW:** one model instead of vocal + 4 Demucs | +1.06 dB **and** faster |
| 5 | **Normalization fix** for loud songs | quality (section 3.4) |
| 6 | **Stems encoded in parallel** (LAME only uses one core) | 14.3 s → 4.4 s per song |
| 7 | **In-memory pipeline:** ffmpeg decodes through a *pipe*, inference is called directly and there are no intermediate WAVs | −4 to 6 s and ~0.8 GB less disk traffic per song |
| 8 | **Overlapping batches:** song N is written on a thread while N+1 is separated | ~−4 s per song in a batch |
| 9 | **Models loaded once** per session and reused | ~3 s less per song |
| 10 | Download progress on first use | UX |
| 11 | **Worker process:** the AI runs in a process that quits 30 s after the queue | RAM after the queue: ~2.1 GB → ~60 MB |
| 12 | **One instance per model** (the overlap is set on the loaded instance) | up to 3 → 1 instance when switching presets |
| 13 | **Fewer array copies** and encoding without `tobytes()` | split peak: 3.81 → 3.46 GB |
| 14 | **One model at a time in VRAM** in *Maximum* (CUDA) | 60 s clip on a 4 GB GPU: ~860 s → ~23 s |
| 15 | **Worker preloading** when songs are added | ~6 s less waiting per queue |

The details and measurements of items 11 to 15 are in [section 4.5](#45-scalability-and-memory-use). All of them keep the output bit-identical.

### 4.4 Tried and dropped

All measured on the RTX 3050:

| Idea | Result |
|---|---|
| Processing blocks in batches (batch 2) | 113 → 112 ms per second of audio: no gain (the GPU is already saturated) |
| Batch 4 | 935 ms/s, 8× slower: overflows the 4 GB of VRAM |
| `torch.backends.cudnn.benchmark` | no gain |
| TF32 | no gain (the model runs in fp16) |
| `torch.compile` with `triton-windows` 3.8 | compilation fails inside the library on Windows and falls back to normal mode; +4 s of loading |
| 13.35 s blocks (training size) | same speed, +0.02 dB |
| Number of CPU threads (4/8/12/16) | the default (12) was the best |
| Overlap 4 | twice the time for +0.02 dB |
| CUDA graphs | pointless with a saturated GPU |
| TensorRT / ONNX Runtime | not tried: exporting a RoFormer (complex STFT) is a project of its own, with no guaranteed gain |
| BF16 / INT8 | BF16 gains nothing over fp16; INT8 risks the quality |

### 4.5 Scalability and memory use

- **One GPU, one job at a time.** Two simultaneous inferences on a 4 GB GPU fight over the VRAM, Windows starts using system RAM and everything becomes **several times slower**. This was seen with a second copy of the app open: up to 8× slower.
- **Bounded pipelining:** at most one song waits to be written, so RAM never holds more than two songs' worth of stems. A 5-minute song with the mix and 4 stems in float32 takes ~0.5 GB; the measured process peak was ~3.1 GiB, including PyTorch and the model.
- **The AI runs in a separate process (worker).** The window doesn't import PyTorch and uses ~50 MB. The worker is created as soon as songs are added and loads the preset's model in the background (only if the model is already downloaded; it waits up to 90 s for *Split stems*), which takes ~6 s off the wait. It loads the model once for the whole queue and quits 30 s after the queue is done (`EngineProcess.IDLE_SECONDS`). Only ending the process reliably gives the memory of PyTorch, CUDA and the C libraries back to the system: in the same process, ~2.1 GB stayed resident even after `engine.close()`.
- **One instance per model.** The overlap is just an attribute read on every `demix()`, so Balanced and Fast share the same BS-RoFormer. Leaving Maximum unloads MelBand-RoFormer. Before, switching between presets left up to 3 instances loaded.
- **One model at a time in VRAM (CUDA).** In Maximum, the idle model waits in RAM while the other one runs (`_place_on_gpu`); the swap takes a fraction of a second. With both on the 4 GB GPU, the VRAM reservation reached 4.3 GB, Windows started using system RAM and a 60 s clip took ~860 s. It now takes ~23 s, with 1.8 GB reserved and bit-identical output. On Apple Silicon the memory is shared, so nothing changes there.
- **On the CPU,** inference dominates even more: ~3.3× real time per pass.
- **Fewer array copies:** only the stems in use are copied out of the model's output (the model's own "other" and, without the option, guitar and piano are dropped); the Maximum average, the gain for loud songs and Other are computed in place; ffmpeg receives the stem's own buffer, without `tobytes()`. The output is still bit-identical.

Measured (RTX 3050 Laptop, 5:21 song, Balanced, WAV + Instrumental):

| | Before | After |
|---|---|---|
| Idle window | ~53 MB | ~53 MB |
| Split RAM peak | 3.81 GB | 3.46 GB |
| RAM after the queue | ~2.1 GB (held until the app closed) | ~60 MB (the worker quits 30 s later) |
| Balanced → Fast → Maximum → Balanced in the same engine | 3 instances, 2.30 GB | 1 instance, 1.67 GB |

---

## 5. Architecture

### 5.1 Files

| File | Responsibility |
|---|---|
| `main.py` | Entry point: window (default), `--cli` and `--selftest` |
| `stemsplitter/engine.py` | The whole audio pipeline: presets, models, decoding, separation, stem assembly and writing |
| `stemsplitter/gui.py` | PySide6 interface: window, queue, settings and the `EngineProcess`, which starts and stops the worker |
| `stemsplitter/worker.py` | Worker process: runs the queue on the engine and sends progress, logs and results to the GUI |
| `stemsplitter/memory.py` | Memory measurement for debugging (`STEMSPLITTER_MEMLOG=1`) |
| `stemsplitter/platform_utils.py` | Data folders, bundled ffmpeg, packaged-app tweaks, opening folders |
| `stemsplitter/__init__.py` | App name and version |
| `StemSplitter.spec` | PyInstaller recipe |
| `.github/workflows/build.yml` | CI: lint, builds, self-test, artifacts and releases |
| `ruff.toml` | Lint rules (real errors only) |
| `CLAUDE.md` | Project rules for Claude Code: everything in English, and every relevant change documented |
| `tools/make_icon.py` | Generates the icons in `assets/` |

### 5.2 Flow of one song

```mermaid
flowchart LR
    A[Audio file] -->|ffmpeg → f32le pipe 44.1 kHz| B[mix in memory]
    B --> C{peak > 1?}
    C -->|yes: × 1/peak| D[model input]
    C -->|no| D
    D --> E[BS-RoFormer SW<br/>6 stems]
    D -.->|Maximum only| F[MelBand-RoFormer<br/>vocals]
    E --> G[vocal average]
    F -.-> G
    G --> H[undo the gain<br/>Other = mix − rest]
    H --> I[N stems in memory]
    I -->|parallel threads| J[ffmpeg/LAME MP3<br/>or 24-bit WAV]
    J --> K[song folder]
```

The stages in `engine.py`:

1. **`_decode`:** ffmpeg turns any format into 44.1 kHz stereo float32 and pipes it straight into a NumPy array, with no temporary file.
2. **Gain:** if the peak is above 1.0, the model input is multiplied by `1/peak`.
3. **`_run_model`:** calls the `audio-separator` model's `demix()` directly with the array (`channels × samples`), skipping the library's file-based path.
4. **Assembly:** undoes the gain; Vocals (the average of the two models in Maximum), Drums, Bass and, if requested, Guitar and Piano; Other = mix − sum; optional Instrumental.
5. **`write_stems`:** a thread *pool* encodes all the stems at once, with ffmpeg reading the audio from stdin.

### 5.3 Engine API

```python
from stemsplitter.engine import StemEngine, Options

engine = StemEngine(log=print)            # keeps the loaded models between songs
opts = Options(output_dir="output", quality="balanced", bitrate_kbps=320,
               output_format="mp3", also_instrumental=False, guitar_piano=False)

result = engine.separate("song.mp3", opts, progress=lambda frac, text: ...)
# or, to overlap writing and separation:
sep = engine.split("song.mp3", opts, progress)         # -> Separated (stems in memory)
result = engine.write_stems(sep, opts, progress)       # -> Result (paths + seconds)

engine.prepare_models(progress, quality="maximum")     # download/load ahead of time (optional)
engine.close()
```

- `Preset(stem_overlap, vocal_overlap)` defines each preset; `get_preset()` resolves names and aliases.
- `progress(frac, text)` receives the overall progress from 0 to 1; `cancel` is any object with `is_set()` (`threading.Event` or `multiprocessing.Event`).
- `Cancelled` is the exception raised when the user cancels.

### 5.4 Processes and threads

| Where | What it does |
|---|---|
| GUI process, main thread (Qt) | Window and events; the `EngineProcess` reads the worker's event queue every 50 ms and forwards the events as signals |
| Worker process, main thread (`worker.serve`) | `engine.split()` for each song (decoding and inference on the GPU/CPU) |
| Worker process, *writer* (1 thread) | `engine.write_stems()` for the previous song and its done event |
| Worker process, encoder *pool* (up to N threads) | One ffmpeg/LAME process per stem |

- **Communication:** two `multiprocessing.Queue`s (*spawn* context). The worker receives the commands `("prepare", quality)` (preload), `("run", jobs, opts)` (process the queue) and `None` (quit). It sends back the events `status`, `done`, `failed`, `log`, `device`, `prepared` and `finished`, delivered in the order they were emitted. Consecutive progress updates for the same song are coalesced.
- **Lifecycle:** the worker is created when songs are added and loads the preset's model right away (`prepare`, only if the model is already downloaded). It waits up to 90 s for *Split stems* (`PREWARM_IDLE_SECONDS`), is reused by any queue started within 30 s after a queue ends (`IDLE_SECONDS`), and quits after that or when the window closes. If the process dies midway (out of memory, driver failure), the remaining songs show as failed and the window keeps working.
- **Cancellation:** a shared `multiprocessing.Event`, checked between inference blocks (through the progress hook) and before each file is written.
- **Packaged:** the worker is the executable itself, started through `multiprocessing.freeze_support()` in `main.py`. The CI `--selftest` starts and stops a worker to make sure this works.
- **Careful when touching the spawn:** the queues and the `Event` must stay referenced in the parent process (for example, on `self`). `Process.start()` drops its own arguments, and on macOS/Linux a collected `Event` deletes its semaphore before the worker can open it (`FileNotFoundError`). On Windows the error doesn't show up, because the handles are duplicated into the child. This is what broke the Mac self-test from `f275d9b` to `a1ea3c3`.

### 5.5 Progress and cancellation

`audio-separator` reports progress through `tqdm`. The engine swaps that `tqdm` for a minimal stand-in (`_HookTqdm`) that:
- forwards the progress of every inference block and of the model download (bars counted in bytes);
- checks for cancellation at every step.

`_bar_tracker` turns this into a 0..1 value that only grows, and the stages are weighted: decoding 2%, stems 93% (or 53% + 40% in *Maximum*) and writing 5%.

### 5.6 Log

The handler on the `audio_separator` logger only forwards warnings and useful messages (download, device) to the window and filters out noise, such as the ONNX Runtime notice, which is irrelevant because both models run on PyTorch. At the end of every song a line with the time of each stage is added, for example `Song: 77s (decode 0.6s, stems 70.8s, encode 4.5s)`.

---

## 6. Platforms, packaging and CI

### 6.1 Requirements

- **Python:** 3.11 recommended.
- **Dependencies (`requirements.txt`):** `audio-separator[cpu]==0.47.0`, `torch>=2.3`, `PySide6==6.11.2`, `imageio-ffmpeg==0.6.0`, `soundfile`, `numpy`.
- **Windows:** 64-bit. For the GPU, an NVIDIA card with a driver compatible with CUDA 13.0.
- **macOS:** 14 (Sonoma) or newer, on Apple Silicon. Intel Macs are not supported because PyTorch no longer publishes builds for them.

### 6.2 Packaging (`StemSplitter.spec`)

- **Bundle:** PyInstaller in folder mode (`dist/StemSplitter/`) on Windows and an `.app` on macOS. No console; the output goes to `stemsplitter.log`.
- **ffmpeg:** the static binary from `imageio-ffmpeg` is bundled under the name `ffmpeg`.
- **Data and metadata:** those of `audio_separator` (model catalog) and `librosa`, plus the metadata of packages that read their own version.
- **Exclusions:** unused packages (tkinter, matplotlib, torchaudio, triton…) and **`pkg_resources`**. setuptools 82+ removed that module, an empty folder left on the runner made the `pyi_rth_pkgres` hook crash the app on macOS, and nothing in the app needs it.
- **Mac:** `Info.plist` with `LSMinimumSystemVersion` 14.0 and dark mode support.

### 6.3 CI (`.github/workflows/build.yml`)

| Trigger | What happens |
|---|---|
| Push to any branch (except commits that only touch `.md` or `tools/`) | Lint and the builds with the self-test. A newer push to the same branch cancels the previous build |
| PR from a fork | The same |
| Manual **Run workflow** | Builds with downloadable files; option for the CPU-only build |
| `v*` tag | Builds and a GitHub Release |

The stages:

1. **Lint** (~30 s): `ruff` with real-error rules only (`E9`, `F`) and `compileall`. The long builds only start if the lint passes.
2. **Matrix builds:**
   - `Windows-x64`: PyTorch **CUDA 13.0**; uses the NVIDIA GPU and falls back to the CPU without one.
   - `macOS-AppleSilicon`: default PyTorch, with Metal.
   - `Windows-x64-CPU` (optional): smaller, CPU only. It only builds on a manual run with the option ticked; in other runs its steps are skipped and the job shows as passed **without having built anything**.
3. **Self-test of the packaged app:** `StemSplitter --selftest` imports PyTorch and `audio-separator`, runs ffmpeg, starts and stops a worker process and writes `selftest.txt`, which includes the CUDA version. On the Mac the app is also signed ad hoc and, if the self-test fails, the error (`selftest.txt`, the app's output and `stemsplitter.log`) is published as a GitHub Actions **annotation**, which can be read without signing in (the job log requires signing in).
4. **Downloadable files:** one zip per platform (manual runs and tags only).
5. **Release:** the CUDA Windows build is larger than 2 GB, the GitHub Releases limit, so it ships as 7-Zip parts `.7z.001`, `.002`, …

### 6.4 First launch for the end user

- **Windows SmartScreen:** the app isn't signed, so click **More info → Run anyway**.
- **macOS Gatekeeper:** right-click → **Open**. If macOS says the app is "damaged": `xattr -dr com.apple.quarantine /Applications/StemSplitter.app`.

---

## 7. Development

### 7.1 Running from source

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Windows + NVIDIA: the PyPI torch for Windows is CPU only; swap it for the CUDA one:
pip install --force-reinstall --no-deps torch==<same version> --index-url https://download.pytorch.org/whl/cu130
python main.py                       # window
python main.py --cli song.mp3 -o output
python main.py --selftest            # the same check CI runs
```

Local build: `pip install pyinstaller && pyinstaller --noconfirm StemSplitter.spec`. The result goes to `dist/`.

### 7.2 Lint

```bash
pip install ruff==0.16.9
ruff check .
python -m compileall -q main.py stemsplitter StemSplitter.spec
```

### 7.3 Measuring quality and speed

The method of section 3.2 is easy to repeat:
1. Download the MUSDB18 sample (`MUSDB18-7-STEMS.zip`, 147 MB, on Zenodo, record 3270814).
2. Decode the streams of each `.stem.mp4`: 0 = mix, 1 = drums, 2 = bass, 3 = other, 4 = vocals.
3. Concatenate the 50 test excerpts, run `engine.split()` on the result and compute the SDR per excerpt against the ground truth.

For speed, the log already has the time of every stage. For fine measurements, synchronize the GPU (`torch.cuda.synchronize()`) before every time reading and **close other programs that use the GPU**.

### 7.4 Extension points

- **New preset:** add a `Preset` to `QUALITY_PRESETS` (`engine.py`) and an entry to `QUALITIES` (`gui.py`).
- **Another model:** any MDXC/RoFormer model from the `audio-separator` catalog can be loaded by `_separator()` and run by `_run_model()`. The result is a `stem → (samples × channels)` dictionary.
- **New output format:** in `_write_stem()`.

---

## 8. Known limitations

- **Separation is not perfect:** heavy reverb, distorted guitars in the vocal range and very dense mixes always leave some bleed.
- **The CPU is slow:** ~20–28 min per 4-minute song. A GPU changes everything.
- **4 GB of VRAM:** other programs using the GPU, including another copy of StemSplitter, can make the separation several times slower. In *Maximum*, only the model in use is kept on the GPU.
- **First pass of the vocal model:** in some tests, in a new session, it took ~45 s longer than the following ones. The cause was not confirmed and it didn't happen in every measurement. It was probably the same *Maximum* VRAM overflow fixed in `5a51768`, but that has not been verified yet.
- **Apple Silicon:** works, but the timings have not been measured yet.
- **Unsigned apps:** public distribution would need an Apple Developer ID (with notarization) and a Windows code-signing certificate.
- **Licenses:** the code belongs to the project. Components: python-audio-separator (MIT), PyTorch (BSD), PySide6/Qt (LGPL-3, dynamically linked), FFmpeg (GPL; include the license and a link to its source code if you distribute it) and the models (check each one's license before any commercial use).

---

## 9. Change history

| Commit | Change |
|---|---|
| `8de7ad7` | Initial version: MelBand-RoFormer + Demucs `htdemucs_ft`, CPU build on Windows |
| `278659a` | GPU by default (CUDA build), fp16 for the vocals, `shifts` 2 → 1, Maximum/Fast presets, timings in the log |
| `5d57e0b` | macOS build fix (`pkg_resources` excluded from PyInstaller) |
| `9b1f112` | CI on every commit: lint, builds and self-test |
| `b85805c` | Demucs replaced by **BS-RoFormer SW**: +1.06 dB and faster; Balanced/Maximum/Fast presets; optional 6 stems; exact sum |
| `2539551` | Profile-driven optimizations: fix for loud songs, parallel encoding, in-memory pipeline, overlapping batches, download progress (94 s → 77 s) |
| `ca743ec`, `5589cc0` | This documentation and the revised README |
| `f275d9b` (PR #2) | The AI runs in a worker process that quits after the queue (RAM after the queue ~2.1 GB → ~60 MB); one instance per model; fewer array copies; `STEMSPLITTER_MEMLOG`; worker self-test |
| `5a51768` (PR #3) | One model at a time in VRAM in *Maximum* (60 s clip: ~860 s → ~23 s on a 4 GB GPU); worker preloading when songs are added |
| `a1ea3c3` | CI: a failed macOS self-test is published as an annotation |
| `65e678d` | macOS self-test fix: the worker's `Event` was collected before the process could open it |
| — | Documentation brought up to date and translated to English (`docs/DOCUMENTACAO.md` → `docs/DOCUMENTATION.md`); `CLAUDE.md` with the English-only and documentation rules |
