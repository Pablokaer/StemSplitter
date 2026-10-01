# StemSplitter

A desktop app for **Windows and macOS** that takes a song (MP3, WAV, FLAC, M4A, …) and splits it into four MP3 files:

| Stem | What's in it |
|---|---|
| **Vocals** | Lead vocals and backing vocals |
| **Drums** | Kit, percussion |
| **Bass** | Bass guitar, synth bass, 808s |
| **Other** | Everything else: the melody and harmony (guitars, keys, synths, strings, …) |

You can also save an **Instrumental** track (everything except the vocals), split **Guitar** and **Piano** out of Other (6 stems), and pick WAV instead of MP3.

## How it gets such clean stems

1. **All stems in one pass.** *BS-RoFormer SW* (jarredou) splits the song into vocals, drums, bass, guitar, piano and other. RoFormer models are the current state of the art in music source separation.
2. **Vocals, Maximum quality only.** *MelBand-RoFormer* (Kimberley Jensen), one of the best vocal-only models, also isolates the vocals, and the two estimates are averaged. An ensemble of two strong models beats either one alone.
3. **Other** is whatever is left of the song once the other stems are taken out, so **Vocals + Drums + Bass + Other adds back up to the original song exactly** (before MP3 encoding).

Measured on the MUSDB18 test set (50 songs with the real stems; median SDR in dB, higher is better):

| | Vocals | Drums | Bass | Other | Average |
|---|---|---|---|---|---|
| **Maximum** | 12.03 | 11.38 | 9.58 | 8.04 | 10.26 |
| **Balanced** | 11.83 | 11.38 | 9.58 | 8.03 | 10.20 |
| **Fast** | 11.54 | 10.92 | 9.11 | 7.69 | 9.81 |
| Previous version (MelBand-RoFormer + Demucs `htdemucs_ft`) | 11.30 | 9.77 | 8.76 | 6.72 | 9.14 |

> No tool can separate stems perfectly. Heavy reverb, distorted guitars that overlap the vocal range, and very dense mixes will always leave some bleed. This pipeline is close to the current state of the art.

## Getting the Windows `.exe` and macOS `.app`

The apps are built for you by GitHub Actions (free), because a Windows build has to run on Windows and a Mac build on a Mac.

1. Create a new **private** repository on GitHub and upload this folder to it (drag and drop in the web UI works, or `git push`).
2. Open the **Actions** tab → **Build desktop apps** → **Run workflow**.
   *Or* create a release tag (`git tag v1.0.0 && git push --tags`) and the downloads are attached to a GitHub Release automatically.
3. After about 15–25 minutes, download:
   * `StemSplitter-Windows-x64.zip`: unzip it and run `StemSplitter\StemSplitter.exe`. It uses an NVIDIA GPU when the PC has one and falls back to the CPU otherwise. It is a ~2.5 GB download because it includes CUDA. On a GitHub Release it comes as `StemSplitter-Windows-x64.7z.001`, `.002`, …: download all parts and open the `.001` with [7-Zip](https://www.7-zip.org).
   * `StemSplitter-macOS-AppleSilicon.zip`: unzip it and drag `StemSplitter.app` to Applications
   * *(optional)* `StemSplitter-Windows-x64-CPU.zip`: tick "CPU-only" when you run the workflow. It is a much smaller download for PCs without an NVIDIA GPU, and is only available from the workflow run's Artifacts.

### First launch

* **Windows SmartScreen**: the app isn't code-signed, so click **More info → Run anyway**.
* **macOS Gatekeeper**: right-click the app → **Open** → **Open**. If macOS says the app is "damaged", run this once:
  `xattr -dr com.apple.quarantine /Applications/StemSplitter.app`
* **Models**: the first split downloads the models once (~0.7 GB, plus ~0.9 GB the first time you use Maximum). They are stored in
  `%LOCALAPPDATA%\StemSplitter\models` on Windows and `~/Library/Application Support/StemSplitter/models` on macOS.

## Using it

1. Drag MP3s (or a whole folder) onto the window, or click **Add files…**
2. Choose where to save. Each song gets its own subfolder: `Song - Vocals.mp3`, `Song - Drums.mp3`, `Song - Bass.mp3`, `Song - Other.mp3`
3. Click **Split stems**.

**Quality**
* *Balanced*: the default. Better than the previous version's best setting on every stem, and faster.
* *Maximum*: adds a second vocal model for the cleanest vocals. About 1.8× slower than Balanced.
* *Fast*: about 1.8× faster than Balanced, with a little more bleed. Still better than the previous version's best setting.

### How long does it take? (4-minute song)

Measured on a laptop (Intel i5-12500H, NVIDIA RTX 3050 Laptop 4 GB), scaled from a 5:20 song (Balanced: 77 s) and a 30 s clip:

| Hardware | Fast | Balanced | Maximum |
|---|---|---|---|
| NVIDIA GPU (RTX 3050 Laptop) | ~30 s | ~1 min | ~1 min 45 s |
| CPU only (12-core laptop CPU) | ~20 min | ~28 min | slower still |

Apple Silicon Macs use the Metal GPU automatically and land in between (not measured yet). Without a GPU, *Fast* is the practical choice.

When you add several songs, the next one is already being separated while the previous one is written to disk.

**Tips for speed**
* Almost all of the time is the AI model itself, so the preset is the real speed knob.
* On GPUs with little memory (4 GB), close other apps that use the GPU (games, video editors, a second copy of StemSplitter). When the GPU runs out of memory Windows borrows system RAM and the split can get several times slower.
* The app shows which device it is using at the bottom of the window, and the log lists how long each step took.
* `STEMSPLITTER_THREADS` (environment variable) pins the CPU thread count. On the test laptop the default was already the fastest, so only try it if CPU-only runs look slow.

You can close the window and it asks before stopping a job that is still running.

## Running from source (developers)

Python 3.11 is recommended.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python main.py                     # desktop app
python main.py --cli song.mp3 -o out_folder     # headless / batch
```

Windows + NVIDIA from source (the PyPI `torch` wheel for Windows is CPU-only): after the install, run
`pip install --force-reinstall --no-deps torch==<same version> --index-url https://download.pytorch.org/whl/cu130`.

Build locally: `pip install pyinstaller && pyinstaller --noconfirm StemSplitter.spec`. The output goes to `dist/`.

## Project layout

```
main.py                     entry point (GUI, --cli, --selftest)
stemsplitter/engine.py      separation pipeline (RoFormer models → MP3)
stemsplitter/gui.py         PySide6 interface
stemsplitter/platform_utils.py  app folders, bundled ffmpeg, Windows/macOS quirks
StemSplitter.spec           PyInstaller build recipe
.github/workflows/build.yml Windows + macOS builds
```

## Limitations

* **macOS 14 (Sonoma) or newer on Apple Silicon** is required. Intel Macs are not supported, because PyTorch no longer ships Intel-Mac builds.
* The apps are unsigned. For public distribution you would need an Apple Developer ID ($99/yr, plus notarization) and a Windows code-signing certificate.

## Licenses

The app code is yours. The main components are **python-audio-separator** (MIT), **PyTorch** (BSD), **PySide6/Qt** (LGPL-3, dynamically linked), the **BS-RoFormer SW** model by jarredou and the **MelBand-RoFormer** vocal model by Kimberley Jensen (check each model's repository for its license before any commercial use). The bundled **FFmpeg** binary comes from `imageio-ffmpeg` and is GPL-licensed. That is fine for personal and internal use. If you distribute the app publicly, include the FFmpeg license and a link to its source.
