"""StemSplitter entry point.

python main.py                 -> opens the desktop app
python main.py --cli song.mp3  -> headless mode (handy for batch jobs / testing)
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# --stems names (lower case, any spacing) -> engine output names
STEM_ALIASES = {"vocals": "Vocals", "drums": "Drums", "bass": "Bass", "other": "Other", "melody": "Other",
                "guitar": "Guitar", "piano": "Piano", "keys": "Piano", "instrumental": "Instrumental",
                "no-drums": "No Drums", "nodrums": "No Drums"}


def parse_stems(text: str) -> tuple[str, ...]:
    if text.strip().lower() == "all":
        return tuple(dict.fromkeys(STEM_ALIASES.values()))
    names = []
    for part in text.split(","):
        key = part.strip().lower().replace(" ", "-").replace("_", "-")
        if key not in STEM_ALIASES:
            raise argparse.ArgumentTypeError(f"unknown stem {part.strip()!r} (choose from {', '.join(STEM_ALIASES)})")
        names.append(STEM_ALIASES[key])
    if not names:
        raise argparse.ArgumentTypeError("choose at least one stem")
    return tuple(names)


def run_cli(argv: list[str]) -> int:
    from stemsplitter.engine import DEFAULT_STEMS, Options, StemEngine
    from stemsplitter.platform_utils import default_output_dir

    parser = argparse.ArgumentParser(prog="StemSplitter --cli")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=default_output_dir())
    parser.add_argument("-q", "--quality", choices=["balanced", "maximum", "fast", "high"], default="balanced",
                        help='"high" is the old name of "balanced"')
    parser.add_argument("-b", "--bitrate", type=int, default=320)
    parser.add_argument("--wav", action="store_true", help="write 24-bit WAV instead of MP3")
    parser.add_argument("--stems", type=parse_stems, default=DEFAULT_STEMS,
                        help="comma-separated files to write: vocals, drums, bass, other (melody), guitar, "
                             "piano (keys), instrumental (no vocals), no-drums (the song without drums), or all "
                             "(default: vocals,drums,bass,other)")
    parser.add_argument("--instrumental", action="store_true", help="also write an Instrumental (no vocals) file")
    parser.add_argument("--guitar-piano", action="store_true", help="also split Guitar and Piano out of Other")
    parser.add_argument("--reserve-mb", type=int, default=0,
                        help="memory (MB) the system must keep free; the app slows down or waits instead "
                             "of using it (default: automatic)")
    parser.add_argument("--no-memory-limit", action="store_true",
                        help="ignore the memory limit: never wait for free memory (the system may swap "
                             "or end the app)")
    args = parser.parse_args(argv)

    last = {"pct": -1}

    def progress(frac: float, text: str) -> None:
        pct = int(frac * 100)
        if pct != last["pct"]:
            last["pct"] = pct
            print(f"\r[{pct:3d}%] {text:<60}", end="", flush=True)

    engine = StemEngine(log=lambda m: print(f"\n  {m}"))
    if args.reserve_mb > 0:
        engine.gov.set_reserve(args.reserve_mb * 1024 * 1024)
    engine.gov.set_unlimited(args.no_memory_limit)
    import torch

    if torch.cuda.is_available():
        print(f"Device: NVIDIA GPU ({torch.cuda.get_device_name(0)})")
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        print("Device: Apple Silicon GPU (Metal)")
    else:
        print(f"Device: CPU, {torch.get_num_threads()} threads (no GPU found)")
    opts = Options(
        output_dir=args.output,
        bitrate_kbps=args.bitrate,
        quality=args.quality,
        stems=args.stems,
        also_instrumental=args.instrumental,
        guitar_piano=args.guitar_piano,
        output_format="wav" if args.wav else "mp3",
    )
    def report(future) -> None:
        res = future.result()
        print(f"\n  {res.input_path.name}: done in {res.seconds:.0f}s")
        for name, p in res.stems.items():
            print(f"  {name:<13} {p}")

    # like the GUI: encode song N on a writer thread while song N+1 is being separated
    pending = prev = None
    try:
        with ThreadPoolExecutor(max_workers=1) as writer:
            for f in args.files:
                # one song at a time while memory is short, and for the same file twice in a row (one
                # work folder, which the writer removes when it is done)
                if pending is not None and (engine.gov.level() != "relaxed" or f.resolve() == prev.resolve()):
                    report(pending)
                    pending = None
                print(f"\n==> {f}")
                separated = engine.split(f, opts, progress, assemble=False)  # assembled on the writer
                if pending is not None:
                    report(pending)
                pending, prev = writer.submit(engine.write_stems, separated, opts, lambda frac, text: None), f
            if pending is not None:
                report(pending)
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
        # the GUI runs every split in a spawned worker process: check one starts and exits cleanly
        from stemsplitter import worker

        ctx = multiprocessing.get_context("spawn")
        # Keep a reference to every queue/event: Process.start() drops its args, and on macOS/Linux a
        # collected Event unlinks its named semaphore before the child can open it (FileNotFoundError).
        commands, events, cancel = ctx.Queue(), ctx.Queue(), ctx.Event()
        proc = ctx.Process(target=worker.serve, args=(commands, events, cancel), daemon=True)
        proc.start()
        commands.put(None)
        proc.join(120)
        if proc.exitcode != 0:
            proc.kill()
            raise RuntimeError(f"worker process exit code: {proc.exitcode}")
        from stemsplitter.memgov import MemoryGovernor

        gov = MemoryGovernor()  # psutil is bundled and can read the memory figures
        if gov.available <= 0 or gov.used <= 0:
            raise RuntimeError(f"memory governor read available={gov.available} used={gov.used}")
        report.write_text(
            f"OK torch={torch.__version__} cuda={torch.version.cuda} ffmpeg={ver.stdout.splitlines()[0]}\n"
        )
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
