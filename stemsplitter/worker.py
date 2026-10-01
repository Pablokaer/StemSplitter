"""Separation worker process.

The GUI never imports PyTorch: every batch runs in this separate process, which loads the
models once, splits the whole queue and is shut down shortly after the queue is done (see
gui.EngineProcess). When a process exits the OS takes back all of its memory, including what
PyTorch, CUDA and the C libraries keep cached, which `del model; gc.collect()` can't promise.

The worker also runs the memory governor (memgov.py), which keeps it below the memory the
system needs to stay responsive.

Commands from the GUI (the `commands` queue):
  ("prepare", quality, reserve_mb, unlimited)
                                     load PyTorch and the preset's models ahead of time (only
                                     models already downloaded, and only if they fit in the
                                     memory budget: preloading never waits; unlimited = ignore
                                     the memory limit)
  ("run", jobs, opts)                split [(row, path)]
  None                               exit

Messages to the GUI are tuples put on the `events` queue:
  ("status", row, fraction, text)   ("done", row, {stem: path}, seconds)   ("failed", row, message)
  ("log", text)   ("device", text)   ("prepared",)   ("finished", cancelled)
  ("memory", {"used", "budget", "available", "reserve", "level", "waiting", "unlimited"})  about once a second while busy
"""

from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .memgov import MB, MemoryGovernor
from .memory import memlog

Emit = Callable[..., None]


def describe_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return f"NVIDIA GPU · {torch.cuda.get_device_name(0)}"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "Apple Silicon GPU (Metal)"
        return f"CPU · {torch.get_num_threads()} threads (no GPU found - this will be slow)"
    except Exception:
        return "CPU"


def run_jobs(engine, jobs: list[tuple[int, Path]], opts, cancel, emit: Emit) -> bool:
    """Split every (row, path) in `jobs`, reporting through `emit`. Returns True if cancelled."""
    from .engine import Cancelled

    def write(row: int, separated, progress) -> bool:
        """Writer thread: save one song and report it as soon as its files are on disk."""
        try:
            res = engine.write_stems(separated, opts, progress, cancel)
            emit("done", row, {k: str(v) for k, v in res.stems.items()}, res.seconds)
        except Cancelled:
            emit("failed", row, "Cancelled")
            return True
        except Exception as exc:
            emit("log", traceback.format_exc())
            emit("failed", row, str(exc) or exc.__class__.__name__)
        return False

    def wait(future) -> bool:
        """Block until the previous song is written (bounds memory). Returns True if it was cancelled."""
        return future.result() if future is not None else False

    cancelled = False
    # Song N is encoded on a writer thread while song N+1 is being separated, when memory allows
    # it (the stems are in files, but the encoders still need some). At most one song waits.
    with ThreadPoolExecutor(max_workers=1) as writer:
        pending = None  # future of the song being written
        try:
            for row, path in jobs:
                if pending is not None and engine.gov.level() != "relaxed":
                    cancelled = wait(pending) or cancelled  # one song at a time while memory is short
                    pending = None
                if cancel.is_set():
                    cancelled = True
                    break
                progress = lambda f, t, r=row: emit("status", r, f, t)  # noqa: E731
                try:
                    separated = engine.split(path, opts, progress, cancel)
                except Cancelled:
                    cancelled = True
                    emit("failed", row, "Cancelled")
                    break
                except Exception as exc:  # keep going with the next file
                    emit("log", traceback.format_exc())
                    emit("failed", row, str(exc) or exc.__class__.__name__)
                    continue
                cancelled = wait(pending) or cancelled
                pending = writer.submit(write, row, separated, progress)
                del separated  # the writer holds the only reference: freed as soon as it is written
        finally:
            cancelled = wait(pending) or cancelled
    return cancelled


def _prepare(engine, quality: str, log) -> None:
    from .engine import get_preset
    from .platform_utils import models_dir

    models = [m for m, _, _ in engine._models_for(get_preset(quality))]
    if not all((models_dir() / m).is_file() for m in models):
        return
    if not engine.can_preload(quality):
        log("Not preloading the model: memory is short right now (it loads when Split starts)")
        return
    engine.prepare_models(lambda frac, text: None, quality=quality)
    memlog("models preloaded", log)


def serve(commands, events, cancel) -> None:
    """Entry point of the worker process: runs the GUI's commands (see the module docstring) until None."""
    from .platform_utils import fix_frozen_std_streams

    fix_frozen_std_streams()  # windowed builds have no console in this process either

    def emit(*msg) -> None:
        events.put(msg)

    def log(text: str) -> None:
        emit("log", text)

    memlog("worker started", log)
    busy = False
    governor = MemoryGovernor(log=log, report=lambda snap: emit("memory", snap) if busy else None)
    governor.start()
    engine = None

    def get_engine():
        nonlocal engine
        if engine is None:
            from .engine import StemEngine

            engine = StemEngine(log=log, governor=governor)
            emit("device", describe_device())
        return engine

    while True:
        command = commands.get()
        if command is None:
            break
        if command[0] == "prepare":
            governor.set_reserve(command[2] * MB)
            governor.set_unlimited(command[3])
            try:
                _prepare(get_engine(), command[1], log)
            except Exception:  # not fatal: the run loads (or downloads) the models again
                emit("log", traceback.format_exc())
            emit("prepared")
            continue
        _, jobs, opts = command
        governor.set_reserve(opts.memory_reserve_mb * MB)
        governor.set_unlimited(opts.ignore_memory_limit)
        cancelled = False
        busy = True
        try:
            if engine is None:
                emit("status", jobs[0][0], 0.0, "Starting engine (loading PyTorch)...")
            cancelled = run_jobs(get_engine(), jobs, opts, cancel, emit)
        except Exception as exc:
            emit("log", traceback.format_exc())
            if jobs:
                emit("failed", jobs[0][0], f"Engine error: {exc}")
        busy = False
        memlog("batch finished", log)
        emit("finished", cancelled)
    governor.stop()
    if engine is not None:
        engine.close()
