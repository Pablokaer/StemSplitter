"""Golden-output check with the real models: split a fixed clip in several configurations and hash every file.

    python benchmarks/golden.py capture golden.json   # with the code before a change
    python benchmarks/golden.py compare golden.json   # after it: every file must hash the same

The clip is synthetic (tones, noise and clicks, peak 1.3 so the loud-input gain path runs too), 12 s long so it
takes the normal chunked path (clips under 10 s take the library's short-audio path). Set
STEMSPLITTER_GOLDEN_DEVICE=cpu to hide the GPU (deterministic and independent of the GPU's clocks); the default
uses whatever the engine picks. Work folders go to a temporary directory, never to the app's own work/ folder,
so a checkpoint left by older code can't leak into the comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np  # noqa: E402
import torch  # noqa: E402

if os.environ.get("STEMSPLITTER_GOLDEN_DEVICE") == "cpu":
    # Hide the GPU from the engine and the library. (CUDA_VISIBLE_DEVICES does not work here: an empty value is
    # dropped on Windows, and "-1" made torch.cuda.is_available() crash with driver 580.97 / torch 2.14.1.)
    torch.cuda.is_available = lambda: False
    torch.cuda.device_count = lambda: 0
import soundfile as sf  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stemsplitter.engine import OUTPUTS, Options, StemEngine  # noqa: E402

SR = 44100
CASES = [  # (name, quality, stems, format)
    ("balanced-all8-wav", "balanced", tuple(OUTPUTS), "wav"),
    ("maximum-all8-wav", "maximum", tuple(OUTPUTS), "wav"),
    ("fast-4stems-mp3", "fast", ("Vocals", "Drums", "Bass", "Other"), "mp3"),
    ("maximum-nodrums-wav", "maximum", ("No Drums",), "wav"),
]


def make_clip(path: Path, seconds: float = 12.0) -> None:
    t = np.arange(int(SR * seconds)) / SR
    rng = np.random.default_rng(1234)
    tone = 0.5 * np.sin(2 * np.pi * 110 * t) + 0.3 * np.sin(2 * np.pi * 440 * t * (1 + 0.01 * np.sin(t)))
    clicks = np.zeros_like(t)
    clicks[:: SR // 2] = 1.0  # a "kick" every half second
    kick = np.convolve(clicks, np.exp(-np.arange(2000) / 300.0), mode="same")
    noise = 0.05 * rng.standard_normal(t.shape)
    left, right = tone + kick + noise, 0.8 * tone + kick - noise
    stereo = np.stack([left, right], 1)
    stereo *= 1.3 / np.max(np.abs(stereo))
    sf.write(path, stereo.astype(np.float32), SR, subtype="FLOAT")


def run(out_root: Path) -> dict:
    clip = out_root / "golden_clip.wav"
    make_clip(clip)
    print(f"  device: {'cuda' if torch.cuda.is_available() else 'cpu'}, {torch.get_num_threads()} CPU threads", flush=True)
    engine = StemEngine(log=lambda m: None)
    engine.gov.set_unlimited(True)
    engine._songs_dir = out_root / "work"
    hashes: dict = {}
    try:
        for name, quality, stems, fmt in CASES:
            t0 = time.perf_counter()
            opts = Options(output_dir=out_root / name, quality=quality, stems=stems, output_format=fmt,
                           ignore_memory_limit=True)  # the window's default; never waits for other programs
            res = engine.separate(clip, opts, lambda f, s: None)
            hashes[name] = {k: hashlib.sha256(Path(p).read_bytes()).hexdigest() for k, p in sorted(res.stems.items())}
            print(f"  {name}: {len(res.stems)} files in {time.perf_counter() - t0:.1f}s", flush=True)
    finally:
        engine.close()
    return hashes


def main() -> int:
    mode, path = sys.argv[1], Path(sys.argv[2])
    with tempfile.TemporaryDirectory() as tmp:
        got = run(Path(tmp))
    if mode == "capture":
        path.write_text(json.dumps(got, indent=1))
        print(f"captured {sum(len(v) for v in got.values())} files -> {path}")
        return 0
    want = json.loads(path.read_text())
    bad = [(case, k) for case in want for k in want[case] if got.get(case, {}).get(k) != want[case][k]]
    bad += [(case, k) for case in got for k in got[case] if k not in want.get(case, {})]
    for case, k in bad:
        print(f"  DIFFERENT: {case} / {k}")
    print("golden output: IDENTICAL" if not bad else f"golden output: {len(bad)} file(s) DIFFER")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
