"""Memory governor: keeps the app below the memory the system needs to stay responsive.

    budget   = what the app uses now + memory the system has available - reserve
    headroom = budget - what the app uses now = available - reserve

The reserve is what the system must keep free to stay responsive (default: 2 GB, or a quarter of
the RAM on machines with less than 8 GB; the user can pick another value). The budget is recalculated every 250 ms, so it floats:
opening a browser shrinks it, closing one grows it.

The engine asks the governor before every step that needs memory (`wait`). With enough
headroom it returns at once; otherwise it first asks the engine to free what it can (caches,
models it isn't using) and then waits, keeping all the work done so far, until other programs
free memory. It never gives up and never lets the app eat into the reserve. The `level`
tells the worker how much it may run in parallel (two songs at once, several encoders).

The limit can be switched off (`set_unlimited`, the "Ignore the memory limit" setting): the
headroom is then treated as endless, so the app never waits, never frees caches early and always
runs at the "relaxed" level. It keeps measuring, so the memory indicator still works, but the
system can run out of memory and swap or end the app.

On macOS the kernel's memory pressure level is used as well: it reflects the risk of the
system stalling better than the "available" figure, because macOS compresses memory first.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Optional

MB = 1024**2
GB = 1024**3

# headroom thresholds for the levels (see MemoryGovernor.level)
RELAXED = 2 * GB
NORMAL = 512 * MB
UNLIMITED = 1 << 50  # the headroom reported when the limit is off (1 PB: more than any machine has)


def total_memory() -> int:
    import psutil

    return psutil.virtual_memory().total


def available_memory() -> int:
    import psutil

    return psutil.virtual_memory().available


def default_reserve(total: Optional[int] = None) -> int:
    total = total_memory() if total is None else total
    return int(min(2 * GB, total / 4))


def process_memory(children: bool = True) -> int:
    """RAM of this process (and its ffmpeg children), plus Apple GPU memory, which is the same RAM."""
    import psutil

    me = psutil.Process()
    used = me.memory_info().rss
    if children:
        for child in me.children(recursive=True):
            try:
                used += child.memory_info().rss
            except psutil.Error:
                pass
    torch = sys.modules.get("torch")  # never import PyTorch just to measure
    if torch is not None and sys.platform == "darwin":
        try:
            used += torch.mps.driver_allocated_memory()
        except Exception:
            pass
    return used


def macos_pressure() -> int:
    """macOS memory pressure: 1 normal, 2 warning, 4 critical (0 when unknown or not on macOS)."""
    if sys.platform != "darwin":
        return 0
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        level = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(level))
        if libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(level), ctypes.byref(size), None, 0):
            return 0
        return level.value
    except Exception:
        return 0


class MemoryGovernor:
    POLL_SECONDS = 0.25
    REPORT_SECONDS = 1.0

    def __init__(
        self,
        reserve_bytes: int = 0,
        log: Optional[Callable[[str], None]] = None,
        report: Optional[Callable[[dict], None]] = None,
        available_fn: Callable[[], int] = available_memory,
        used_fn: Callable[[bool], int] = process_memory,
        pressure_fn: Callable[[], int] = macos_pressure,
    ) -> None:
        self.log = log or (lambda msg: None)
        self.report = report
        self._available_fn, self._used_fn, self._pressure_fn = available_fn, used_fn, pressure_fn
        self._lock = threading.Lock()
        self._shedders: list[Callable[[str], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.waiting = False
        self.unlimited = False
        self.available = self.used = 0
        self.pressure = 0
        self._low_water = self._mark = 1 << 62
        self.set_reserve(reserve_bytes)
        self.sample(children=True)

    # -- configuration ------------------------------------------------------------------
    def set_reserve(self, reserve_bytes: int = 0) -> None:
        """Memory the system must keep free; 0 = automatic."""
        self.reserve = int(reserve_bytes) if reserve_bytes and reserve_bytes > 0 else default_reserve()

    def set_unlimited(self, unlimited: bool = False) -> None:
        """True: ignore the reserve and the macOS pressure; every `wait` returns at once."""
        self.unlimited = bool(unlimited)

    def add_shedder(self, fn: Callable[[str], None]) -> None:
        """`fn(level)` frees what it can; called when memory runs short, before waiting."""
        self._shedders.append(fn)

    # -- measuring ----------------------------------------------------------------------
    def sample(self, children: bool = False) -> None:
        available = self._available_fn()
        pressure = self._pressure_fn()
        with self._lock:
            self.available, self.pressure = available, pressure
            self._low_water = min(self._low_water, available)
            if children or not self.used:
                self.used = self._used_fn(True)

    @property
    def headroom(self) -> int:
        """How much more the app may take right now (negative: it should give some back)."""
        if self.unlimited:
            return UNLIMITED
        room = self.available - self.reserve
        if self.pressure >= 4:  # critical: macOS is about to stall
            room = min(room, -1)
        elif self.pressure >= 2:  # warning: compressing memory already
            room = min(room, NORMAL - 1)
        return room

    @property
    def budget(self) -> int:
        return max(0, self.used + self.headroom)

    def level(self) -> str:
        """relaxed: run everything in parallel; normal: some; tight: one thing at a time; over: wait."""
        room = self.headroom
        if room >= RELAXED:
            return "relaxed"
        if room >= NORMAL:
            return "normal"
        return "tight" if room > 0 else "over"

    def encoder_slots(self, wanted: int) -> int:
        level = self.level()
        if level == "relaxed":
            return max(1, wanted)
        return max(1, min(wanted, 2 if level == "normal" else 1))

    def mark(self) -> None:
        """Start measuring how much memory the next step takes (see drop_since_mark)."""
        with self._lock:
            self._mark = self._low_water = self.available

    def drop_since_mark(self) -> int:
        """How far the available memory fell below its value at mark() (sampled every 250 ms)."""
        self.sample()
        with self._lock:
            return max(0, self._mark - self._low_water) if self._mark < 1 << 62 else 0

    def snapshot(self) -> dict:
        return {"used": self.used, "budget": self.budget, "available": self.available, "reserve": self.reserve,
                "level": self.level(), "waiting": self.waiting, "unlimited": self.unlimited}

    # -- controlling --------------------------------------------------------------------
    def shed(self) -> None:
        level = self.level()
        for fn in self._shedders:
            try:
                fn(level)
            except Exception as exc:  # freeing memory must never break the job
                self.log(f"Could not free memory: {exc}")
        self.sample(children=True)

    def wait(
        self,
        need: int,
        check: Optional[Callable[[], None]] = None,
        on_wait: Optional[Callable[[int], None]] = None,
    ) -> float:
        """Block until `need` more bytes fit in the headroom; returns the seconds spent waiting.

        Frees what it can first. `check()` is called while waiting (it raises to cancel) and
        `on_wait(bytes short)` about once a second, to tell the user why nothing is moving.
        """
        self.sample()
        if self.headroom >= need:
            return 0.0
        self.shed()
        if self.headroom >= need:
            return 0.0
        started, told = time.monotonic(), 0.0
        self.waiting = True
        try:
            while self.headroom < need:
                if check is not None:
                    check()
                now = time.monotonic()
                if now - told >= 1.0:
                    told = now
                    if on_wait is not None:
                        on_wait(need - self.headroom)
                    self._report()
                time.sleep(0.5)
                self.sample()
                if now - started > 5 and int(now - started) % 10 == 0:
                    self.shed()  # other programs may have let go of memory the caches can now use
        finally:
            self.waiting = False
        waited = time.monotonic() - started
        self.log(f"Waited {waited:.0f}s for free memory")
        return waited

    # -- background sampling --------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="memory governor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        self._thread = None

    def _run(self) -> None:
        last_report = 0.0
        while not self._stop.wait(self.POLL_SECONDS):
            now = time.monotonic()
            report = now - last_report >= self.REPORT_SECONDS
            try:
                self.sample(children=report)
            except Exception:
                continue
            if report:
                last_report = now
                self._report()

    def _report(self) -> None:
        if self.report is not None:
            try:
                self.report(self.snapshot())
            except Exception:
                pass


def reserve_from_env() -> int:
    """STEMSPLITTER_MEM_RESERVE_MB=N overrides the reserve (used by the CLI and for testing)."""
    value = os.environ.get("STEMSPLITTER_MEM_RESERVE_MB", "").strip()
    return int(value) * MB if value.isdigit() else 0
