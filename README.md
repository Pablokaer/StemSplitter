# StemSplitter

A desktop app for **Windows and macOS** that takes a song (MP3, WAV, FLAC, M4A, …) and splits it into four MP3 files:

| Stem | What's in it |
|---|---|
| **Vocals** | Lead vocals and backing vocals |
| **Drums** | Kit, percussion |
| **Bass** | Bass guitar, synth bass, 808s |
| **Other** | Everything else: the melody and harmony (guitars, keys, synths, strings, …) |

You can also save an **Instrumental** track (everything except the vocals), and you can pick WAV instead of MP3.

## How it gets such clean stems

It runs two AI models, each one of the best available for its job:

1. **Vocals.** *MelBand-RoFormer* (Kimberley Jensen) separates the vocals from the rest (vocal SDR ≈ 12.6 dB). RoFormer models are currently the best at isolating vocals.
2. **Drums, bass and other.** *Demucs v4 `htdemucs_ft`* (Meta AI), a set of 4 fine-tuned models, splits the instrumental that step 1 left behind. It works on the vocal-free track, so the other stems end up with much less vocal bleed than a single-pass split gives.

Every step subtracts from the one before, so **Vocals + Drums + Bass + Other adds back up to the original song** (checked: 28 dB reconstruction SNR after MP3 encoding).

> No tool can separate stems perfectly. Heavy reverb, distorted guitars that overlap the vocal range, and very dense mixes will always leave some bleed. This pipeline is close to the current state of the art.

## Getting the Windows `.exe` and macOS `.app`

The apps are built for you by GitHub Actions (free), because a Windows build has to run on Windows and a Mac build on a Mac.

1. Create a new **private** repository on GitHub and upload this folder to it (drag and drop in the web UI works, or `git push`).
2. Open the **Actions** tab → **Build desktop apps** → **Run workflow**.
   *Or* create a release tag (`git tag v1.0.0 && git push --tags`) and the downloads are attached to a GitHub Release automatically.
3. After about 15–25 minutes, download:
   * `StemSplitter-Windows-x64.zip`: unzip it and run `StemSplitter\StemSplitter.exe`
   * `StemSplitter-macOS-AppleSilicon.zip`: unzip it and drag `StemSplitter.app` to Applications
   * *(optional)* `StemSplitter-Windows-x64-NVIDIA.zip`: tick "NVIDIA" when you run the workflow. It is much faster on NVIDIA GPUs but a ~2.5 GB download, and is only available from the workflow run's Artifacts, not from Releases.

### First launch

* **Windows SmartScreen**: the app isn't code-signed, so click **More info → Run anyway**.
* **macOS Gatekeeper**: right-click the app → **Open** → **Open**. If macOS says the app is "damaged", run this once:
  `xattr -dr com.apple.quarantine /Applications/StemSplitter.app`
* **Models**: the first split downloads the models once (~1.2 GB). They are stored in
  `%LOCALAPPDATA%\StemSplitter\models` on Windows and `~/Library/Application Support/StemSplitter/models` on macOS.

## Using it

1. Drag MP3s (or a whole folder) onto the window, or click **Add files…**
2. Choose where to save. Each song gets its own subfolder: `Song - Vocals.mp3`, `Song - Drums.mp3`, `Song - Bass.mp3`, `Song - Other.mp3`
3. Click **Split stems**.

**Quality**
* *Maximum*: best separation. This is the default.
* *High*: about 2× faster at the Demucs step, with slightly more bleed.

### How long does it take? (4-minute song, Maximum quality, rough figures)

| Hardware | Time |
|---|---|
| NVIDIA RTX GPU (NVIDIA build) | ~1–2 min |
| Apple Silicon M1–M4 (uses the Metal GPU automatically) | ~3–6 min |
| Modern 8-core CPU, no GPU | ~10–20 min |

The app shows which device it is using at the bottom of the window. You can close the window and it asks before stopping a job that is still running.

## Running from source (developers)

Python 3.11 is recommended.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python main.py                     # desktop app
python main.py --cli song.mp3 -o out_folder     # headless / batch
```

Windows + NVIDIA from source: after the install, run
`pip install --force-reinstall --no-deps torch==<same version> --index-url https://download.pytorch.org/whl/cu130`.

Build locally: `pip install pyinstaller && pyinstaller --noconfirm StemSplitter.spec`. The output goes to `dist/`.

## Project layout

```
main.py                     entry point (GUI, --cli, --selftest)
stemsplitter/engine.py      separation pipeline (RoFormer → Demucs → MP3)
stemsplitter/gui.py         PySide6 interface
stemsplitter/platform_utils.py  app folders, bundled ffmpeg, Windows/macOS quirks
StemSplitter.spec           PyInstaller build recipe
.github/workflows/build.yml Windows + macOS builds
```

## Limitations

* **macOS 14 (Sonoma) or newer on Apple Silicon** is required. Intel Macs are not supported, because PyTorch no longer ships Intel-Mac builds.
* The apps are unsigned. For public distribution you would need an Apple Developer ID ($99/yr, plus notarization) and a Windows code-signing certificate.

## Licenses

The app code is yours. The main components are **python-audio-separator** (MIT), **Demucs** (MIT), **PyTorch** (BSD), **PySide6/Qt** (LGPL-3, dynamically linked) and the **MelBand-RoFormer** vocal model by Kimberley Jensen (check its GitHub repository for the model license before any commercial use). The bundled **FFmpeg** binary comes from `imageio-ffmpeg` and is GPL-licensed. That is fine for personal and internal use. If you distribute the app publicly, include the FFmpeg license and a link to its source.
# StemSplitter
