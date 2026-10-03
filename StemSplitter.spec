# PyInstaller spec for StemSplitter (Windows, macOS, Linux).
#   pyinstaller --noconfirm StemSplitter.spec
# Produces dist/StemSplitter/ (Windows/Linux) or dist/StemSplitter.app (macOS).

import fnmatch
import json
import os
import re
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

APP = "StemSplitter"
ROOT = Path(SPECPATH)
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "stemsplitter" / "__init__.py").read_text()).group(1)
# set by the release workflow (the matrix name, e.g. Windows-x64): the app needs it to find its own update files
PLATFORM = os.environ.get("STEMSPLITTER_PLATFORM", "")

# --- bundle a static ffmpeg under the plain name "ffmpeg" (audio-separator calls it by name)
import imageio_ffmpeg

stage = ROOT / "build" / "ffmpeg_stage"
stage.mkdir(parents=True, exist_ok=True)
ffmpeg_dst = stage / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg_dst)
ffmpeg_dst.chmod(0o755)

datas = [(str(ROOT / "assets"), "assets")]
if PLATFORM:
    build_info = ROOT / "build" / "build-info.json"
    build_info.parent.mkdir(parents=True, exist_ok=True)
    build_info.write_text(json.dumps({"platform": PLATFORM}))
    datas.append((str(build_info), "."))
datas += collect_data_files("audio_separator")  # models.json, model-data.json, configs
datas += collect_data_files("librosa")  # lazy_loader .pyi stubs + example registry
for dist in ["audio-separator", "torch", "onnxruntime", "librosa", "numpy", "tqdm", "requests",
             "soundfile", "PySide6", "beartype", "rotary-embedding-torch"]:
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

hiddenimports = collect_submodules("audio_separator")
hiddenimports += [
    "samplerate", "resampy", "diffq", "julius", "einops", "rotary_embedding_torch", "beartype",
    "ml_collections", "yaml", "pydub", "scipy.signal", "sklearn.utils._typedefs",
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[(str(ffmpeg_dst), "ffmpeg")],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # pkg_resources: setuptools >= 82 no longer ships it, but a leftover empty folder on the build
    # machine still gets bundled and PyInstaller's pyi_rth_pkgres hook then crashes at startup
    # ("no attribute 'NullProvider'"). Nothing needs it: audio_separator only tries it as a
    # guarded fallback after importlib.metadata.
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "notebook", "PyQt5", "PyQt6",
              "torchaudio", "tensorboard", "imageio_ffmpeg", "pytest", "triton", "pkg_resources"],
    noarchive=False,
)

# CUDA DLLs from the PyTorch wheel that nothing in the app loads: no DLL imports them (dumpbin /dependents)
# and torch only loads them by scanning its lib folder. Multi-GPU cuSOLVER, the alternative NVRTC build and
# the profiler's metrics library (~140 MB compressed): without them the CUDA build fits in a single release
# zip under GitHub's 2 GiB limit. A GPU split was bit-identical with and without them (docs, 6.2).
UNUSED_DLLS = ("cusolvermg64_*.dll", "nvrtc64_*.alt.dll", "nvperf_host.dll")
a.binaries = [b for b in a.binaries if not any(fnmatch.fnmatch(Path(b[0]).name.lower(), p) for p in UNUSED_DLLS)]
a.datas = [d for d in a.datas if not any(fnmatch.fnmatch(Path(d[0]).name.lower(), p) for p in UNUSED_DLLS)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / ("icon.ico" if IS_WIN else "icon.icns")),
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP)

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP}.app",
        icon=str(ROOT / "assets" / "icon.icns"),
        bundle_identifier="com.stemsplitter.app",
        info_plist={
            "CFBundleName": APP,
            "CFBundleDisplayName": APP,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "14.0",  # current PyTorch wheels need macOS 14+
            "NSRequiresAquaSystemAppearance": False,  # follow dark mode
        },
    )
