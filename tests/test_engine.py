"""Engine regression tests with a fake network (benchmarks/fake_model.py): no GPU and no model download.

Everything except the network is the production code: decoding (ffmpeg), the streaming chunk loop with its
checkpoints, the assembly, the batch pipeline of the worker and the WAV writing. For every file written, two
hashes are compared with tests/golden_fake.json: the exact float32 samples handed to the encoder (so any change
to any sample fails, even one too small to survive 24-bit WAV) and the WAV file itself. The golden file was
captured from the code before any optimization (commit b634ca7), and only holds the plain single runs in CASES:
every other test (cancel and resume at several points, a batch, a split that leaves the assembly to
write_stems) must reproduce exactly those hashes.

    python -m unittest discover -s tests -v
    STEMSPLITTER_CAPTURE_GOLDEN=1 python -m unittest discover -s tests -k test_cases   # rewrite it (on purpose only)
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
    "balanced-vocals-short": ("short5", "balanced", ("Vocals",)),  # under 10 s: the short-audio segment size
}

_float_hashes: dict = {}  # output path -> sha256 of the float32 samples handed to the encoder
_orig_write_stem = E.StemEngine._write_stem


def _hashing_write_stem(self, audio, sr, out, opts, title, peak=None, cancel=None):
    h = hashlib.sha256()
    for i in range(0, len(audio), E.BLOCK_FRAMES):
        h.update(np.ascontiguousarray(audio[i : i + E.BLOCK_FRAMES], dtype=np.float32).tobytes())
    _float_hashes[str(out)] = h.hexdigest()
    return _orig_write_stem(self, audio, sr, out, opts, title, peak, cancel)


def _clip(path: Path, seconds: float, peak: float, seed: int) -> None:
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * seconds)) / SR
    x = np.stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 330 * t)], 1) + 0.3 * rng.standard_normal((len(t), 2))
    sf.write(path, (x * (peak / np.max(np.abs(x)))).astype(np.float32), SR, subtype="FLOAT")


def _digest(files: dict) -> dict:
    return {k: {"wav": hashlib.sha256(Path(p).read_bytes()).hexdigest(), "f32": _float_hashes[str(p)]}
            for k, p in sorted(files.items())}


class EngineGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        E.StemEngine._write_stem = _hashing_write_stem
        cls.tmp = Path(tempfile.mkdtemp(prefix="stemsplitter_test_"))
        cls.clips = {"loud40": cls.tmp / "loud40.wav", "quiet40": cls.tmp / "quiet40.wav", "short5": cls.tmp / "short5.wav"}
        _clip(cls.clips["loud40"], 40, 1.3, 1)
        _clip(cls.clips["quiet40"], 40, 0.8, 2)
        _clip(cls.clips["short5"], 5, 0.9, 3)
        cls.golden = {} if CAPTURE else json.loads(GOLDEN.read_text())
        cls.got: dict = {}

    @classmethod
    def tearDownClass(cls):
        E.StemEngine._write_stem = _orig_write_stem
        if CAPTURE:
            GOLDEN.write_text(json.dumps(cls.got, indent=1, sort_keys=True) + "\n")

    def engine(self, **fake):
        eng = E.StemEngine(log=lambda m: None)
        eng.gov.set_unlimited(True)  # the window's default; the governor only decides when steps run
        eng._songs_dir = Path(tempfile.mkdtemp(dir=self.tmp))
        fake_model.install(eng, **fake)
        self.addCleanup(eng.close)
        return eng

    def opts(self, case: str, out: Path) -> E.Options:
        _, quality, stems = CASES[case]
        return E.Options(output_dir=out, quality=quality, stems=stems, output_format="wav")

    def assertGolden(self, case: str, files: dict) -> None:
        self.assertEqual(_digest(files), self.golden[case], f"output differs from the golden output of {case}")

    def every_chunk_checkpoints(self):
        old = E.CHECKPOINT_SECONDS
        E.CHECKPOINT_SECONDS = 0
        self.addCleanup(setattr, E, "CHECKPOINT_SECONDS", old)

    # -- the golden cases ------------------------------------------------------------------
    def test_cases(self):
        for name, (clip, _, stems) in CASES.items():
            with self.subTest(name):
                eng = self.engine()
                res = eng.separate(self.clips[clip], self.opts(name, self.tmp / name), lambda f, s: None)
                self.assertEqual(list(res.stems), E.Options(output_dir=".", stems=stems).outputs())
                if CAPTURE:
                    self.got[name] = _digest(res.stems)
                else:
                    self.assertGolden(name, res.stems)

    # -- everything else must reproduce them -------------------------------------------------
    def _cancel_then_resume(self, case: str, cancel_at: float, one_encoder: bool = False) -> None:
        """Cancel once the overall progress passes `cancel_at` (a checkpoint every chunk), split again."""
        self.every_chunk_checkpoints()
        eng = self.engine()
        if one_encoder:  # one file at a time, so a cancel lands between two files
            eng.gov.encoder_slots = lambda wanted: 1
        opts = self.opts(case, self.tmp / f"resume{cancel_at}")
        cancel = threading.Event()

        def progress(frac, text):
            if frac > cancel_at:
                cancel.set()

        with self.assertRaises(E.Cancelled):
            eng.separate(self.clips[CASES[case][0]], opts, progress, cancel)
        res = eng.separate(self.clips[CASES[case][0]], opts, lambda f, s: None, threading.Event())
        self.assertGolden(case, res.stems)

    @unittest.skipIf(CAPTURE, "uses the golden output")
    def test_resume_in_stem_pass(self):
        self._cancel_then_resume("maximum-all8-loud", 0.3)

    @unittest.skipIf(CAPTURE, "uses the golden output")
    def test_resume_in_vocal_pass(self):
        self._cancel_then_resume("maximum-all8-loud", 0.75)  # the vocal model runs from 55 % to 93 %

    @unittest.skipIf(CAPTURE, "uses the golden output")
    def test_resume_in_encoding(self):
        self._cancel_then_resume("balanced-all8-loud", 0.951, one_encoder=True)  # after the first of 8 files

    @unittest.skipIf(CAPTURE, "uses the golden output")
    def test_assembly_in_write_stems(self):
        """split(assemble=False) + write_stems; also stopped before the assembly, and cancelled inside it."""
        case, clip = "maximum-all8-loud", self.clips["loud40"]
        eng = self.engine()
        opts = self.opts(case, self.tmp / "deferred")
        eng.split(clip, opts, lambda f, s: None, assemble=False)  # "crash" here: nothing assembled yet
        sep = eng.split(clip, opts, lambda f, s: None, assemble=False)
        self.assertEqual(sep.stems, {})
        cancel = threading.Event()

        def progress(frac, text):
            if text.startswith("Assembling"):
                cancel.set()

        with self.assertRaises(E.Cancelled):
            eng.write_stems(sep, opts, progress, cancel)
        sep = eng.split(clip, opts, lambda f, s: None, assemble=False)
        res = eng.write_stems(sep, opts, lambda f, s: None, threading.Event())
        self.assertGolden(case, res.stems)

    @unittest.skipIf(CAPTURE, "uses the golden output")
    def test_batch_pipeline(self):
        """worker.run_jobs (song N assembled and written while N+1 is separated) writes the single-run files."""
        eng = self.engine(delay=0.01)
        songs = []
        for i in range(3):
            p = self.tmp / f"batch{i}.wav"
            p.write_bytes(self.clips["loud40"].read_bytes())
            songs.append((i, p))
        events = []
        cancelled = run_jobs(eng, songs, self.opts("balanced-all8-loud", self.tmp / "batch"), threading.Event(),
                             lambda *e: events.append(e))
        self.assertFalse(cancelled)
        done = [e for e in events if e[0] == "done"]
        self.assertEqual(sorted(e[1] for e in done), [0, 1, 2], events)
        for _, row, files, _secs in done:
            self.assertGolden("balanced-all8-loud", files)

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
