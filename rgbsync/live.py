"""Real-time lighting engine: pulls Scenes from a source, pushes them to backends.

The engine knows nothing about the UI or about where Scenes come from. A
*source* is any callable ``source(now) -> Scene | None`` (``now`` is
``time.monotonic()`` seconds); the engine calls it once per tick on its own
daemon thread and hands each Scene to every backend with ``supports_live``.
Returning ``None`` means "nothing to show this tick": nothing is rendered (the
devices keep their last frame; backends resend keepalives only on render).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

from .manager import Manager
from .scene import Scene

log = logging.getLogger(__name__)

Source = Callable[[float], "Scene | None"]


class LiveEngine:
    """Fixed-rate render loop over ``manager.live_backends()``."""

    def __init__(self, manager: Manager, fps: float = 30,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.manager = manager
        self.fps = fps
        self._clock = clock
        self._source: Source | None = None
        self._backends: list = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()       # guards start/stop transitions
        self._stats_lock = threading.Lock()
        self._tick_times: deque[float] = deque()   # recent tick timestamps
        self._render_ms: dict[str, float] = {}
        self._frames = 0
        self._source_errors = 0
        self._failing: set[str] = set()     # log a crash once, not 30x a second

    # -- public API ----------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def stats(self) -> dict:
        """Snapshot: ``fps`` (achieved ticks/s over the last second),
        ``render_ms`` ({backend name: duration of its last render call}),
        ``frames`` (Scenes rendered so far) and ``source_errors``."""
        with self._stats_lock:
            times = self._tick_times
            span = times[-1] - times[0] if len(times) > 1 else 0.0
            fps = (len(times) - 1) / span if span > 0 else 0.0
            return {"fps": fps, "render_ms": dict(self._render_ms),
                    "frames": self._frames, "source_errors": self._source_errors}

    def start(self, source: Source) -> None:
        """Start (or, when already running, just swap the source of) the loop.

        Calls ``start_live()`` on every live-capable backend first; a backend
        whose ``start_live`` raises is left out of this run.
        """
        with self._lock:
            if self.running:
                self._source = source
                return
            self._source = source
            self._backends = []
            for backend in self.manager.live_backends():
                try:
                    backend.start_live()
                except Exception:
                    log.exception("%s start_live crashed", backend.name)
                    continue
                self._backends.append(backend)
            with self._stats_lock:
                self._tick_times.clear()
                self._render_ms = {}
                self._frames = 0
                self._source_errors = 0
            self._failing = set()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="live-engine")
            self._thread.start()

    def stop(self) -> None:
        """Stop the loop, wait for it, then call ``stop_live()`` on each backend."""
        with self._lock:
            thread, self._thread = self._thread, None
            backends, self._backends = self._backends, []
            self._stop_event.set()
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    log.warning("live engine thread did not stop within 5 s")
            for backend in backends:
                try:
                    backend.stop_live()
                except Exception:
                    log.exception("%s stop_live crashed", backend.name)
            self._source = None

    # -- loop ------------------------------------------------------------------
    def _run(self) -> None:
        period = 1.0 / self.fps
        next_t = self._clock()
        while not self._stop_event.is_set():
            self._tick()
            next_t += period
            delay = next_t - self._clock()
            if delay < -period:       # fell far behind: skip, do not burst
                next_t = self._clock()
                delay = 0.0
            if delay > 0 and self._stop_event.wait(delay):
                break

    def _tick(self) -> None:
        now = self._clock()
        with self._stats_lock:
            times = self._tick_times
            times.append(now)
            while times and now - times[0] > 1.0:
                times.popleft()
        source = self._source
        scene = None
        if source is not None:
            try:
                scene = source(now)
            except Exception:
                if "<source>" not in self._failing:
                    self._failing.add("<source>")
                    log.exception("live source crashed (further errors only counted)")
                with self._stats_lock:
                    self._source_errors += 1
        if scene is None:
            return
        for backend in list(self._backends):
            started = time.perf_counter()
            try:
                backend.render(scene)
            except Exception:
                if backend.name not in self._failing:
                    self._failing.add(backend.name)
                    log.exception("%s render crashed (further errors not logged)",
                                  backend.name)
            ms = (time.perf_counter() - started) * 1000.0
            with self._stats_lock:
                self._render_ms[backend.name] = ms
        with self._stats_lock:
            self._frames += 1
