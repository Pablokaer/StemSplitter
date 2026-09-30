# PyInstaller spec for StemSplitter (Windows, macOS, Linux).
#   pyinstaller --noconfirm StemSplitter.spec
# Produces dist/StemSplitter/ (Windows/Linux) or dist/StemSplitter.app (macOS).

import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

APP = "StemSplitter"
ROOT = Path(SPECPATH)
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# --- bundle a static ffmpeg under the plain name "ffmpeg" (audio-separator calls it by name)
import imageio_ffmpeg

stage = ROOT / "build" / "ffmpeg_stage"
stage.mkdir(parents=True, exist_ok=True)
ffmpeg_dst = stage / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg_dst)
ffmpeg_dst.chmod(0o755)

datas = [(str(ROOT / "assets"), "assets")]
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
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "14.0",  # current PyTorch wheels need macOS 14+
            "NSRequiresAquaSystemAppearance": False,  # follow dark mode
        },
    )
