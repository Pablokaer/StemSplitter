"""Engine regression tests with a fake network (benchmarks/fake_model.py): no GPU and no model download.

Everything except the network is the production code: decoding (ffmpeg), the streaming chunk loop with its
checkpoints, the assembly, the batch pipeline of the worker and the WAV writing. The files written are
compared byte for byte with tests/golden_fake.json, captured from the code before any optimization, so a
change that alters a single sample fails.

    python -m unittest discover -s tests -v
    STEMSPLITTER_CAPTURE_GOLDEN=1 python -m unittest discover -s tests    # rewrite the golden file (on purpose only)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "benchmarks")]

import fake_model  # noqa: E402

from stemsplitter import engine as E  # noqa: E402
from stemsplitter.worker import run_jobs  # noqa: E402

GOLDEN = Path(__file__).with_name("golden_fake.json")
CAPTURE = os.environ.get("STEMSPLITTER_CAPTURE_GOLDEN") == "1"
SR = 44100
ALL8 = tuple(E.OUTPUTS)
DEFAULT4 = ("Vocals", "Drums", "Bass", "Other")
CASES = {  # name: (clip, quality, stems)
    "balanced-all8-loud": ("loud40", "balanced", ALL8),
    "maximum-all8-loud": ("loud40", "maximum", ALL8),
    "fast-default4-quiet": ("quiet40", "fast", DEFAULT4),
    "maximum-nodrums-loud": ("loud40", "maximum", ("No Drums",)),
    "balanced-guitar-other-quiet": ("quiet40", "balanced", ("Other", "Guitar")),
    "balanced-vocals-short": ("short5", "balanced", ("Vocals",)),  # shorter than one 9.3 s chunk
}


def _clip(path: Path, seconds: float, peak: float, seed: int) -> None:
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * seconds)) / SR
    x = np.stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 330 * t)], 1) + 0.3 * rng.standard_normal((len(t), 2))
    sf.write(path, (x * (peak / np.max(np.abs(x)))).astype(np.float32), SR, subtype="FLOAT")


def _digest(files: dict) -> dict:
    return {k: hashlib.sha256(Path(p).read_bytes()).hexdigest() for k, p in sorted(files.items())}


class EngineGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="stemsplitter_test_"))
        cls.clips = {"loud40": cls.tmp / "loud40.wav", "quiet40": cls.tmp / "quiet40.wav", "short5": cls.tmp / "short5.wav"}
        _clip(cls.clips["loud40"], 40, 1.3, 1)
        _clip(cls.clips["quiet40"], 40, 0.8, 2)
        _clip(cls.clips["short5"], 5, 0.9, 3)
        cls.golden = {} if CAPTURE else json.loads(GOLDEN.read_text())
        cls.got: dict = {}

    @classmethod
    def tearDownClass(cls):
        if CAPTURE:
            GOLDEN.write_text(json.dumps(cls.got, indent=1, sort_keys=True) + "\n")

    def engine(self, **fake):
        eng = E.StemEngine(log=lambda m: None)
        eng.gov.set_unlimited(True)  # the window's default; the governor only decides when steps run
        eng._songs_dir = Path(tempfile.mkdtemp(dir=self.tmp))
        fake_model.install(eng, **fake)
        self.addCleanup(eng.close)
        return eng

    def check(self, name: str, files: dict) -> None:
        got = _digest(files)
        self.got[name] = got
        if not CAPTURE:
            self.assertEqual(got, self.golden[name], f"{name}: output differs from the golden output")

    def opts(self, quality, stems, out):
        return E.Options(output_dir=out, quality=quality, stems=stems, output_format="wav")

    def test_cases(self):
        for name, (clip, quality, stems) in CASES.items():
            with self.subTest(name):
                eng = self.engine()
                res = eng.separate(self.clips[clip], self.opts(quality, stems, self.tmp / name), lambda f, s: None)
                self.assertEqual(list(res.stems), E.Options(output_dir=".", stems=stems).outputs())
                self.check(name, res.stems)

    def test_resume_after_cancel_is_identical(self):
        """Cancel in the middle of the stem pass (a checkpoint every chunk), split again: same files."""
        old = E.CHECKPOINT_SECONDS
        E.CHECKPOINT_SECONDS = 0
        self.addCleanup(setattr, E, "CHECKPOINT_SECONDS", old)
        eng = self.engine()
        opts = self.opts("maximum", ALL8, self.tmp / "resume")
        cancel = threading.Event()

        def progress(frac, text):
            if frac > 0.3:  # ~half of the stem pass
                cancel.set()

        with self.assertRaises(E.Cancelled):
            eng.split(self.clips["loud40"], opts, progress, cancel)
        res = eng.separate(self.clips["loud40"], opts, lambda f, s: None, threading.Event())
        self.check("maximum-all8-loud-resumed", res.stems)
        if not CAPTURE:
            self.assertEqual(_digest(res.stems), self.golden["maximum-all8-loud"])

    def test_batch_pipeline_matches_single_runs(self):
        """worker.run_jobs (song N written while N+1 is separated) writes the same files as single runs."""
        eng = self.engine(delay=0.01)
        songs = []
        for i in range(3):
            p = self.tmp / f"batch{i}.wav"
            p.write_bytes(self.clips["loud40"].read_bytes())
            songs.append((i, p))
        events = []
        opts = self.opts("balanced", ALL8, self.tmp / "batch")
        cancelled = run_jobs(eng, songs, opts, threading.Event(), lambda *e: events.append(e))
        self.assertFalse(cancelled)
        done = [e for e in events if e[0] == "done"]
        self.assertEqual(sorted(e[1] for e in done), [0, 1, 2], events)
        for _, row, files, _secs in done:
            self.check(f"batch-{row}", files)
            if not CAPTURE:
                self.assertEqual(_digest(files), self.golden["balanced-all8-loud"])

    def test_outputs_resolution(self):
        o = E.Options(output_dir=".", stems=("No Drums", "Vocals"))
        self.assertEqual(o.outputs(), ["Vocals", "No Drums"])
        self.assertEqual(E.model_stems(o.outputs()), ["Vocals", "Drums"])
        self.assertEqual(E.model_stems(["Other"]), ["Vocals", "Drums", "Bass"])
        legacy = E.Options(output_dir=".", also_instrumental=True, guitar_piano=True)
        self.assertEqual(legacy.outputs(), ["Vocals", "Drums", "Bass", "Other", "Guitar", "Piano", "Instrumental"])
        for bad in [(), ("Kazoo",)]:
            with self.assertRaises(ValueError):
                E.Options(output_dir=".", stems=bad).outputs()


if __name__ == "__main__":
    unittest.main()
