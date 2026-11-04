"""UDP listener for the Geometry Dash Geode mod (protocol: docs/gd-protocol.md).

The mod sends JSON datagrams to 127.0.0.1:47831, fire and forget. ``GDListener``
runs a daemon thread that parses them into a ``GDState`` (latest values) and a
queue of ``GDEvent``s. Anything malformed is ignored and counted, never raised:
bad JSON, wrong protocol version, unknown message types. Out-of-range numbers are
clamped; fields of the wrong type are skipped (the previous value stays).

``connected`` means a ``state`` message arrived within the last ``TIMEOUT``
seconds (and no ``level_exit`` since). The clock is injectable for tests.
"""
from __future__ import annotations

import errno
import json
import logging
import math
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47831
PROTOCOL_VERSION = 1
#: Seconds without a ``state`` message after which the game counts as gone.
TIMEOUT = 3.0

MODES = ("cube", "ship", "ball", "ufo", "wave", "robot", "spider", "swing")
DIFFICULTIES = ("auto", "easy", "normal", "hard", "harder", "insane", "demon",
                "unrated")
EVENT_KINDS = ("level_start", "death", "restart", "complete", "checkpoint",
               "level_exit")

_MAX_EVENTS = 256        # queued events kept if nobody drains them
_MAX_NAME = 80
_RECV_BYTES = 4096
_SOCKET_TIMEOUT = 0.1    # seconds; bounds how long stop() waits


@dataclass
class GDState:
    """Latest known game state (see the ``state`` table in the protocol doc)."""

    pct: float = 0.0
    best: float = 0.0
    mode: str = "cube"
    flip: bool = False
    speed: float = 1.0
    bg: tuple[int, int, int] = (0, 0, 0)
    p1: tuple[int, int, int] = (255, 255, 255)
    pulse: float = 0.0
    practice: bool = False
    paused: bool = False
    # from the last level_start
    level_name: str = ""
    difficulty: str = "unrated"
    stars: int = 0
    platformer: bool = False
    #: A ``state`` message arrived within ``TIMEOUT`` seconds.
    connected: bool = False


@dataclass
class GDEvent:
    """One event from the game: ``kind`` is a protocol ``t`` value."""

    kind: str
    detail: dict[str, Any]


# --------------------------------------------------------------- validation
def _num(value: Any, lo: float, hi: float) -> float | None:
    """A finite number clamped into [lo, hi], or None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return max(lo, min(hi, value))


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _rgb(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    out = []
    for item in value:
        n = _num(item, 0, 255)
        if n is None:
            return None
        out.append(int(round(n)))
    return out[0], out[1], out[2]


def parse_message(data: bytes) -> tuple[str, dict[str, Any]] | None:
    """Decode one datagram to ``(type, message)``; None when not usable.

    Rejects bad JSON, non-objects, a wrong or missing ``v`` and unknown types.
    """
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(msg, dict):
        return None
    version = msg.get("v")
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        return None
    kind = msg.get("t")
    if kind != "state" and kind not in EVENT_KINDS:
        return None
    return kind, msg


def _event_detail(kind: str, msg: dict[str, Any]) -> dict[str, Any]:
    """Validated extra fields of an event message."""
    detail: dict[str, Any] = {}
    if kind == "level_start":
        name = msg.get("name")
        stars = _num(msg.get("stars"), 0, 10)
        difficulty = msg.get("difficulty")
        detail["name"] = name[:_MAX_NAME] if isinstance(name, str) else ""
        detail["stars"] = int(stars) if stars is not None else 0
        detail["difficulty"] = (difficulty.lower() if isinstance(difficulty, str)
                                and difficulty.lower() in DIFFICULTIES else "unrated")
        detail["platformer"] = bool(_bool(msg.get("platformer")))
    elif kind == "death":
        pct = _num(msg.get("pct"), 0, 100)
        detail["pct"] = pct if pct is not None else 0.0
        detail["new_best"] = bool(_bool(msg.get("new_best")))
    elif kind == "complete":
        detail["new_best"] = bool(_bool(msg.get("new_best")))
    return detail


# ----------------------------------------------------------------- listener
class GDListener:
    """Receives the mod's datagrams on a daemon thread. All methods are thread-safe.

    ``port=0`` picks a free port (see ``port`` after ``start``); tests use that.
    ``status`` is ``"stopped"``, ``"listening"``, ``"port in use"`` or
    ``"error: <reason>"``.
    """

    def __init__(self, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.host = host
        self.port = port
        self.status = "stopped"
        self.received = 0        # datagrams accepted
        self.ignored = 0         # datagrams rejected (bad JSON, version, type)
        self._clock = clock
        self._lock = threading.Lock()
        self._state = GDState()
        self._last_state_t: float | None = None
        self._events: deque[GDEvent] = deque(maxlen=_MAX_EVENTS)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> bool:
        """Bind the socket and start the thread. False (with ``status`` set)
        when the port is taken or binding fails. Starting twice is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return True
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No SO_REUSEADDR: on Windows it would let two programs share the port.
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            in_use = exc.errno in (errno.EADDRINUSE, 10048, 10013)
            self.status = "port in use" if in_use else f"error: {exc}"
            log.warning("Geometry Dash listener on %s:%s: %s", self.host, self.port, exc)
            return False
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        sock.settimeout(_SOCKET_TIMEOUT)
        self.port = sock.getsockname()[1]
        self._sock = sock
        self._stop_event.clear()
        self.status = "listening"
        self._thread = threading.Thread(target=self._run, args=(sock,), daemon=True,
                                        name="gd-listener")
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop the thread and close the socket (returns within about 0.2 s)."""
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self.status = "stopped"

    # -- data ----------------------------------------------------------------
    def state(self) -> GDState:
        """A copy of the latest state; ``connected`` is evaluated now."""
        with self._lock:
            out = replace(self._state)
            last = self._last_state_t
        out.connected = last is not None and self._clock() - last <= TIMEOUT
        return out

    def drain_events(self) -> list[GDEvent]:
        """Events received since the last call, oldest first."""
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    # -- thread --------------------------------------------------------------
    def _run(self, sock: socket.socket) -> None:
        while not self._stop_event.is_set():
            try:
                data, _addr = sock.recvfrom(_RECV_BYTES)
            except socket.timeout:
                continue
            except OSError as exc:
                # Windows reports ICMP "port unreachable" on UDP as ECONNRESET;
                # harmless. Anything else after stop() is the closed socket.
                if self._stop_event.is_set():
                    return
                if getattr(exc, "winerror", None) == 10054:
                    continue
                log.warning("Geometry Dash listener socket error: %s", exc)
                self.status = f"error: {exc}"
                return
            try:
                self.handle(data)
            except Exception:           # never let a bad packet kill the thread
                log.exception("Geometry Dash message handling crashed")

    def handle(self, data: bytes) -> bool:
        """Process one datagram. Returns whether it was accepted. Never raises
        for bad input."""
        parsed = parse_message(data)
        if parsed is None:
            self.ignored += 1
            return False
        kind, msg = parsed
        now = self._clock()
        with self._lock:
            self.received += 1
            if kind == "state":
                self._apply_state(msg)
                self._last_state_t = now
            else:
                self._apply_event(kind, msg)
        return True

    def _apply_state(self, msg: dict[str, Any]) -> None:
        st = self._state
        pct = _num(msg.get("pct"), 0, 100)
        if pct is not None:
            st.pct = pct
        best = _num(msg.get("best"), 0, 100)
        if best is not None:
            st.best = best
        mode = msg.get("mode")
        if isinstance(mode, str) and mode.lower() in MODES:
            st.mode = mode.lower()
        speed = _num(msg.get("speed"), 0, 10)
        if speed is not None:
            st.speed = speed
        pulse = _num(msg.get("pulse"), 0, 1)
        if pulse is not None:
            st.pulse = pulse
        for key in ("bg", "p1"):
            color = _rgb(msg.get(key))
            if color is not None:
                setattr(st, key, color)
        for key in ("flip", "practice", "paused"):
            flag = _bool(msg.get(key))
            if flag is not None:
                setattr(st, key, flag)

    def _apply_event(self, kind: str, msg: dict[str, Any]) -> None:
        detail = _event_detail(kind, msg)
        if kind == "level_start":
            st = self._state
            st.level_name = detail["name"]
            st.difficulty = detail["difficulty"]
            st.stars = detail["stars"]
            st.platformer = detail["platformer"]
            st.pct = 0.0
            st.flip = False
            st.paused = False
        elif kind == "level_exit":
            self._state = GDState()      # left the level: back to idle at once
            self._last_state_t = None
        self._events.append(GDEvent(kind, detail))
