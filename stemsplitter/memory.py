"""Memory instrumentation (debug only).

Set STEMSPLITTER_MEMLOG=1 to log the process RAM (and GPU memory once PyTorch is loaded) at
each stage of a split. Off by default: it costs nothing when disabled.
"""

from __future__ import annotations

import os
import sys
from typing import Callable

ENABLED = os.environ.get("STEMSPLITTER_MEMLOG", "").strip() not in ("", "0")


def rss_mb() -> float:
    """Resident memory of this process in MB (0 if it can't be read)."""
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        pass
    try:
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi")
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / 1e6
        elif sys.platform.startswith("linux"):
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
        else:  # macOS: only the peak is available without psutil
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    except Exception:
        pass
    return 0.0


def _gpu_text() -> str:
    torch = sys.modules.get("torch")  # never import PyTorch just to measure
    if torch is None:
        return ""
    try:
        if torch.cuda.is_available() and torch.cuda.is_initialized():
            return (f", CUDA {torch.cuda.memory_allocated() / 1e6:.0f} MB allocated"
                    f" / {torch.cuda.memory_reserved() / 1e6:.0f} MB reserved")
        mps = getattr(torch, "mps", None)
        if mps is not None and torch.backends.mps.is_available():
            return f", MPS {mps.current_allocated_memory() / 1e6:.0f} MB"
    except Exception:
        pass
    return ""


def memlog(stage: str, log: Callable[[str], None]) -> None:
    """Log the memory use at `stage` when STEMSPLITTER_MEMLOG is set."""
    if ENABLED:
        log(f"[mem] {stage}: RSS {rss_mb():.0f} MB (pid {os.getpid()}){_gpu_text()}")
