"""A stand-in for the RoFormer network, for tests and benchmarks that need no GPU and no model download.

install(engine) makes engine._stream_model run its real chunk loop (chunk schedule, overlap-add, sliding
window, division, checkpoints, file writes) around a cheap deterministic "network": each instrument is the
input chunk times a fixed float32 factor, so the outputs are exact and identical on every machine.
`delay` seconds of sleep per chunk emulate a forward pass that keeps a GPU busy while the CPU waits.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import torch

INSTRUMENTS = ["bass", "drums", "other", "vocals", "guitar", "piano"]  # the stem model's order
FACTORS = [0.10, 0.30, 0.20, 0.35, 0.03, 0.02]


class FakeNet(torch.nn.Module):
    def __init__(self, n: int, device: torch.device, delay: float) -> None:
        super().__init__()
        self.weight = torch.nn.Parameter(torch.zeros(1, device=device), requires_grad=False)  # gives the device
        self.factors = torch.tensor(FACTORS[:n] if n > 1 else [0.4], device=device).view(-1, 1, 1)
        self.delay = delay

    def forward(self, x):  # (1, 2, chunk) -> (1, n, 2, chunk) like model_run; one target: (1, 2, chunk)
        if self.delay:
            time.sleep(self.delay)
        if len(self.factors) == 1:  # a single-target model (MelBand vocals) has no instrument dimension
            return x * self.factors[0]
        return x.unsqueeze(1) * self.factors


def model_instance(target: bool = False, device: str = "cpu", delay: float = 0.0, overlap: int = 2):
    """A fake MDXC model instance: the stem model (6 instruments) or, with target=True, the vocal model."""
    from audio_separator.separator.architectures import mdxc_separator as mdxc

    instruments = ["vocals", "other"] if target else INSTRUMENTS
    net = FakeNet(1 if target else len(instruments), torch.device(device), delay)
    cfg = SimpleNamespace(
        inference=SimpleNamespace(dim_t=801), audio=SimpleNamespace(hop_length=441),
        model=SimpleNamespace(stft_hop_length=512),  # chunk = 512 * 800 = 409,600 samples, as the real stem model
        training=SimpleNamespace(instruments=instruments, target_instrument="vocals" if target else None),
    )
    mi = SimpleNamespace(model_data_cfgdict=cfg, segment_size=256, overlap=overlap, model_run=net)
    mi._run_roformer_model = lambda part: net(part.unsqueeze(0))[0]
    mi.overlap_add = lambda result, x, w, start, length: mdxc.MDXCSeparator.overlap_add(None, result, x, w, start,
                                                                                         length)
    mi._roformer_chunk_starts = mdxc.MDXCSeparator._roformer_chunk_starts
    return mi


def install(engine, device: str = "cpu", delay: float = 0.0) -> None:
    """Replace the engine's model loading with the fake models (the stem model and the vocal model)."""
    from stemsplitter.engine import VOCAL_MODEL

    models = {}

    def separator(model, overlap, out_dir, on_wait=None):
        if model not in models:
            models[model] = model_instance(target=model == VOCAL_MODEL, device=device, delay=delay)
        models[model].overlap = overlap
        engine._separators[model] = SimpleNamespace(model_instance=models[model])
        return engine._separators[model]

    engine._separator = separator
    engine._place_on_gpu = lambda active: None
