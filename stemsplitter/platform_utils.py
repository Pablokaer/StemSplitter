"""Platform helpers: app data folders, bundled ffmpeg, frozen-app quirks."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_NAME

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_FROZEN = getattr(sys, "frozen", False)


def app_data_dir() -> Path:
    """Per-user folder for models, logs and settings."""
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    path = app_data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_dir() -> Path:
    music = Path.home() / "Music"
    base = music if music.is_dir() else Path.home()
    return base / APP_NAME


def fix_frozen_std_streams() -> None:
    """Windowed (no-console) builds have sys.stdout/stderr = None.

    Libraries such as tqdm and logging write to them, so point them at a log file.
    """
    if sys.stdout is None or sys.stderr is None:
        log_file = open(app_data_dir() / "stemsplitter.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_file
        if sys.stderr is None:
            sys.stderr = log_file


def _exe_name(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def find_ffmpeg() -> str:
    """Return an ffmpeg executable path and make sure `ffmpeg` resolves on PATH.

    Order: binary bundled inside the frozen app -> ffmpeg already on PATH ->
    the static binary shipped with the imageio-ffmpeg wheel.
    """
    candidates: list[Path] = []
    if IS_FROZEN:
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates.append(base / "ffmpeg" / _exe_name("ffmpeg"))

    for cand in candidates:
        if cand.is_file():
            _prepend_path(cand.parent)
            return str(cand)

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg

        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "FFmpeg was not found. Install it (https://ffmpeg.org) or `pip install imageio-ffmpeg`."
        ) from exc

    # audio-separator calls plain `ffmpeg`, so expose the wheel binary under that name.
    shim_dir = app_data_dir() / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / _exe_name("ffmpeg")
    if not shim.exists() or shim.stat().st_size != src.stat().st_size:
        shutil.copy2(src, shim)
        if not IS_WINDOWS:
            shim.chmod(0o755)
    _prepend_path(shim_dir)
    return str(shim)


def _prepend_path(folder: Path) -> None:
    folder_s = str(folder)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if folder_s not in parts:
        os.environ["PATH"] = folder_s + os.pathsep + os.environ.get("PATH", "")


def subprocess_flags() -> dict:
    """Hide the console window that ffmpeg would pop up on Windows."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def open_folder(path: Path) -> None:
    path = Path(path)
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
