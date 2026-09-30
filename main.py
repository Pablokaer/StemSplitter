"""StemSplitter entry point.

python main.py                 -> opens the desktop app
python main.py --cli song.mp3  -> headless mode (handy for batch jobs / testing)
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path


def run_cli(argv: list[str]) -> int:
    from stemsplitter.engine import Options, StemEngine
    from stemsplitter.platform_utils import default_output_dir

    parser = argparse.ArgumentParser(prog="StemSplitter --cli")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=default_output_dir())
    parser.add_argument("-q", "--quality", choices=["maximum", "high"], default="maximum")
    parser.add_argument("-b", "--bitrate", type=int, default=320)
    parser.add_argument("--wav", action="store_true", help="write 24-bit WAV instead of MP3")
    parser.add_argument("--instrumental", action="store_true", help="also write an Instrumental (no vocals) file")
    args = parser.parse_args(argv)

    last = {"pct": -1}

    def progress(frac: float, text: str) -> None:
        pct = int(frac * 100)
        if pct != last["pct"]:
            last["pct"] = pct
            print(f"\r[{pct:3d}%] {text:<60}", end="", flush=True)

    engine = StemEngine(log=lambda m: print(f"\n  {m}"))
    opts = Options(
        output_dir=args.output,
        bitrate_kbps=args.bitrate,
        quality=args.quality,
        also_instrumental=args.instrumental,
        output_format="wav" if args.wav else "mp3",
    )
    try:
        for f in args.files:
            print(f"\n==> {f}")
            res = engine.separate(f, opts, progress)
            print(f"\n  done in {res.seconds:.0f}s")
            for name, p in res.stems.items():
                print(f"  {name:<13} {p}")
    finally:
        engine.close()
    return 0


def run_selftest() -> int:
    """Used by CI to check a packaged build: every heavy dependency imports and ffmpeg runs."""
    import logging
    import subprocess

    from stemsplitter.platform_utils import app_data_dir, find_ffmpeg, models_dir, subprocess_flags

    report = app_data_dir() / "selftest.txt"
    try:
        import torch
        from audio_separator.separator import Separator

        from stemsplitter import engine

        ffmpeg = find_ffmpeg()
        ver = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, **subprocess_flags())
        Separator(info_only=True, model_file_dir=str(models_dir()), log_level=logging.ERROR)
        engine._install_progress_hooks()
        report.write_text(f"OK torch={torch.__version__} ffmpeg={ver.stdout.splitlines()[0]}\n")
        return 0
    except Exception:
        import traceback

        report.write_text("FAIL\n" + traceback.format_exc())
        return 1


def main() -> int:
    multiprocessing.freeze_support()  # required for PyInstaller on Windows/macOS
    from stemsplitter.platform_utils import fix_frozen_std_streams

    fix_frozen_std_streams()  # windowed builds have no console; send output to the log file
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        return run_cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        return run_selftest()

    from stemsplitter.gui import run_gui

    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
