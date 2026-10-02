"""Benchmarks of the engine's hot paths, with the fake network (benchmarks/fake_model.py).

    python benchmarks/bench_engine.py loop  [--runs 5] [--device cpu|cuda] [--stems Vocals,Drums,Bass]
    python benchmarks/bench_engine.py tail  [--runs 3]
    python benchmarks/bench_engine.py batch [--runs 3] [--songs 3] [--minutes 2] [--delay 0.5]

loop:  the stem pass of a 4-minute song with a network that costs nothing, i.e. the per-chunk work outside
       the network (input read and copy, overlap-add, sliding window, division, file writes, governor calls).
       On a GPU that work leaves the GPU idle, so it adds straight to the split time.
tail:  _assemble + write_stems (MP3 320 and 24-bit WAV) of a 4-minute song, for several stem selections.
batch: worker.run_jobs over several songs, with a network that sleeps `delay` s per chunk, like a GPU that is
       busy while the CPU waits (the real stem model takes ~1 s per chunk on an RTX 3050 Laptop). Measures the
       whole queue, so it shows how much of the per-song CPU work overlaps the "GPU" work.

Reports the median of the runs and the peak RSS of the process. Work files go to a temporary directory.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "benchmarks")]

import fake_model  # noqa: E402
import psutil  # noqa: E402

from stemsplitter import engine as E  # noqa: E402

SR = 44100


class PeakRSS:
    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        me = psutil.Process()
        while not self._stop.wait(0.05):
            self.peak = max(self.peak, me.memory_info().rss)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def new_engine(tmp: Path, **fake) -> E.StemEngine:
    eng = E.StemEngine(log=lambda m: None)
    eng.gov.set_unlimited(True)
    eng._songs_dir = tmp / "work"
    fake_model.install(eng, **fake)
    return eng


def fill(track, frames: int, rng) -> None:
    for i in range(0, frames, E.BLOCK_FRAMES):
        j = min(frames, i + E.BLOCK_FRAMES)
        track[i:j] = (rng.standard_normal((j - i, 2)) * 0.2).astype(np.float32)
    track.close()


def bench_loop(args) -> None:
    frames = SR * 240
    stems = args.stems.split(",")
    times = []
    with tempfile.TemporaryDirectory() as tmp, PeakRSS() as rss:
        tmp = Path(tmp)
        eng = new_engine(tmp, device=args.device)
        for r in range(args.runs):
            work = E._Work(tmp / "loop", f"run{r}")
            fill(work.array("mix", frames, "w+"), frames, np.random.default_rng(0))
            t0 = time.perf_counter()
            eng._stream_model(E.STEM_MODEL, 2, work, frames, 1.0, {k.lower(): f"sw_{k}" for k in stems}, "stems",
                              "bench", lambda f, s: None)
            times.append(time.perf_counter() - t0)
            work.remove()
        eng.close()
    from audio_separator.separator.architectures.mdxc_separator import MDXCSeparator

    chunks = len(MDXCSeparator._roformer_chunk_starts(frames, 409600, 204800))
    med = statistics.median(times)
    print(f"loop  device={args.device} stems={args.stems}: median {med:.2f}s over {args.runs} runs "
          f"({1000 * med / chunks:.1f} ms per chunk, {chunks} chunks) | runs {[round(t, 2) for t in times]} | "
          f"peak RSS {rss.peak / 2**20:.0f} MB")


def bench_tail(args) -> None:
    frames = SR * 240
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        eng = new_engine(tmp)
        song = tmp / "song.wav"
        song.write_bytes(b"")
        for label, stems in [("default 4", ("Vocals", "Drums", "Bass", "Other")), ("all 8", tuple(E.OUTPUTS)),
                             ("No Drums", ("No Drums",))]:
            outputs = E.Options(output_dir=tmp, stems=stems).outputs()
            names = E.model_stems(outputs)
            res = {"assemble": [], "mp3": [], "wav": []}
            for r in range(args.runs):
                work = E._Work(tmp / "tail", f"{label}{r}".replace(" ", ""))
                rng = np.random.default_rng(0)
                for k in ["mix", *(f"sw_{n}" for n in names)]:
                    fill(work.array(k, frames, "w+"), frames, rng)
                t0 = time.perf_counter()
                eng._assemble(work, frames, names, outputs, 1.0, False)
                res["assemble"].append(time.perf_counter() - t0)
                for fmt in ("mp3", "wav"):
                    opts = E.Options(output_dir=tmp / f"out{fmt}{r}", stems=stems, output_format=fmt)
                    sep = E.Separated(song, {k: work.array(f"out_{k}", frames, "r") for k in outputs}, SR, {},
                                      time.time(), work=None, peaks=dict(work.state["peaks"]))
                    t0 = time.perf_counter()
                    eng.write_stems(sep, opts, lambda f, s: None)
                    res[fmt].append(time.perf_counter() - t0)
                work.remove()
            print(f"tail  {label:9s} ({len(outputs)} files): " + " | ".join(
                f"{k} median {statistics.median(v):.2f}s {[round(x, 2) for x in v]}" for k, v in res.items()))
        eng.close()


def bench_batch(args) -> None:
    from stemsplitter.worker import run_jobs

    times = []
    with tempfile.TemporaryDirectory() as tmp, PeakRSS() as rss:
        tmp = Path(tmp)
        import soundfile as sf

        rng = np.random.default_rng(0)
        songs = []
        for i in range(args.songs):
            p = tmp / f"song{i}.wav"
            sf.write(p, (rng.standard_normal((int(SR * 60 * args.minutes), 2)) * 0.2).astype(np.float32), SR,
                     subtype="FLOAT")
            songs.append((i, p))
        for r in range(args.runs):
            eng = new_engine(tmp / f"r{r}", delay=args.delay)
            opts = E.Options(output_dir=tmp / f"out{r}", stems=tuple(args.stems.split(",")), output_format="mp3")
            t0 = time.perf_counter()
            run_jobs(eng, songs, opts, threading.Event(), lambda *e: None)
            times.append(time.perf_counter() - t0)
            eng.close()
    print(f"batch {args.songs} songs x {args.minutes} min, {args.delay}s/chunk, stems={args.stems}: median "
          f"{statistics.median(times):.2f}s over {args.runs} runs {[round(t, 2) for t in times]} | "
          f"peak RSS {rss.peak / 2**20:.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["loop", "tail", "batch"])
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--stems", default="Vocals,Drums,Bass,Other")
    ap.add_argument("--songs", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=2.0)
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()
    if args.what == "loop" and "Other" in args.stems:
        args.stems = ",".join(E.model_stems(E.Options(output_dir=".", stems=tuple(args.stems.split(","))).outputs()))
    os.environ.setdefault("STEMSPLITTER_THREADS", "")
    {"loop": bench_loop, "tail": bench_tail, "batch": bench_batch}[args.what](args)


if __name__ == "__main__":
    main()
