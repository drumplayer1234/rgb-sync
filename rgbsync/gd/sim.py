"""Geometry Dash simulator: a scripted fake game for testing without the game.

``FakeGDSender`` sends real UDP datagrams in the mod's protocol (see
docs/gd-protocol.md) following a timeline: a hard level (cube, ship, flip, wave)
-> death at 34% -> restart during the burst -> new best death at 51% -> practice
mode with two checkpoints -> leave the level -> a normal level, run to the end,
complete with a new best.

  python -m rgbsync.gd.sim --print [--speed 4]   listener + effects, print scenes
  python -m rgbsync.gd.sim --send [--speed 1]    only send (to try the real app)

The states look like a real recording: mostly black backgrounds with brief white
flashes, the mod's ``accent`` color (null for a while), and raw 0-16 music meters
with 2 Hz beat bursts. No hardware and no game needed. A pass takes about 38 s at
``--speed 1``.
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time
from typing import Any, Callable

from ..scene import RGB, Scene
from .effects import GDEffects, KEY_COLUMN, MAX_COLUMN
from .listener import DEFAULT_HOST, DEFAULT_PORT, GDListener

STATE_HZ = 30.0
SCRIPT_END = 38.5

L1_START = 0.0          # level 1 (hard)
L1_STARS, L1_BEST = 5, 40.0
L2_START = 28.5         # level 2 (normal)
L2_RUN_SECONDS = 6.0

#: (start, end, pct reached at end) of each attempt on level 1, plus practice.
ATTEMPT1 = (0.5, 8.5, 34.0)
ATTEMPT2 = (9.0, 21.0, 51.0)
PRACTICE = (22.0, 27.5, 28.0)

#: (time, event message), times are script seconds.
TIMELINE_EVENTS: list[tuple[float, dict[str, Any]]] = [
    (0.0, {"t": "level_start", "name": "Sim Rush", "stars": L1_STARS,
           "difficulty": "hard", "platformer": False}),
    (8.5, {"t": "death", "pct": 34.0, "new_best": False}),
    (9.0, {"t": "restart"}),                      # 0.5 s later: inside the burst
    (21.0, {"t": "death", "pct": 51.0, "new_best": True}),
    (22.0, {"t": "restart"}),
    (24.0, {"t": "checkpoint"}),
    (26.0, {"t": "checkpoint"}),
    (27.5, {"t": "level_exit"}),
    (L2_START, {"t": "level_start", "name": "Sim Stroll", "stars": 3,
                "difficulty": "normal", "platformer": False}),
    (L2_START + L2_RUN_SECONDS, {"t": "complete", "new_best": True}),
]

#: Background color per level, like a real recording: mostly black, some dark
#: blue, and short white flashes. Piecewise constant: (seconds into the level, rgb).
_BG1: list[tuple[float, RGB]] = [
    (0.0, (0, 0, 0)), (6.0, (0, 3, 32)), (9.0, (0, 0, 0)), (15.0, (0, 3, 32)),
    (18.0, (0, 0, 0))]
_BG2: list[tuple[float, RGB]] = [(0.0, (0, 0, 0)), (4.0, (0, 3, 32))]
#: Brief white flashes: (level seconds, length).
_FLASHES1 = [(2.0, 0.15), (7.5, 0.2), (12.0, 0.15), (19.0, 0.2), (23.0, 0.15)]
_FLASHES2 = [(1.5, 0.15), (4.5, 0.2)]

#: The mod's "most active vivid color": keyframes (seconds, rgb) blended smoothly,
#: with windows where nothing is vivid (null, the app falls back to the player color).
_ACCENT1: list[tuple[float, RGB]] = [
    (0.0, (255, 40, 60)), (4.0, (255, 120, 0)), (8.0, (0, 170, 255)),
    (12.0, (170, 0, 255)), (16.0, (0, 255, 120)), (22.0, (255, 40, 60))]
_ACCENT2: list[tuple[float, RGB]] = [
    (0.0, (60, 255, 90)), (3.0, (0, 200, 255)), (6.0, (255, 170, 0))]
_NO_ACCENT1 = [(10.0, 11.5), (24.0, 26.0)]
_NO_ACCENT2: list[tuple[float, float]] = []

BEAT_HZ = 2.0


def _in_windows(windows, t: float) -> bool:
    return any(a <= t < b for a, b in windows)


def _step_keys(frames: list[tuple[float, RGB]], t: float) -> list[int]:
    color = frames[0][1]
    for start, c in frames:
        if t >= start:
            color = c
    return list(color)


def _bg_at(frames, flashes, t: float) -> list[int]:
    if any(a <= t < a + length for a, length in flashes):
        return [255, 255, 255]
    return _step_keys(frames, t)


def _beats(t: float) -> tuple[float, float, float]:
    """Raw (pulse, pulse2, pulse3) like the game's meters: 2 Hz kick bursts that
    rise to about 14, a quieter 4 Hz hat, and a third meter that is unavailable."""
    kick = max(0.0, math.cos(2 * math.pi * BEAT_HZ * t)) ** 2
    hat = max(0.0, math.cos(2 * math.pi * 2 * BEAT_HZ * t + 1.0)) ** 4
    return round(0.6 + 13.4 * kick, 2), round(0.3 + 4.0 * hat, 2), 0.0


def _lerp_keys(frames: list[tuple[float, RGB]], t: float) -> list[int]:
    if t <= frames[0][0]:
        return list(frames[0][1])
    for (t0, c0), (t1, c1) in zip(frames, frames[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0)
            return [int(round(a + (b - a) * k)) for a, b in zip(c0, c1)]
    return list(frames[-1][1])


def _mode_at(table: list[tuple[float, str]], t: float) -> str:
    mode = table[0][1]
    for start, name in table:
        if t >= start:
            mode = name
    return mode


_MODES1 = [(0.0, "cube"), (3.5, "ship"), (6.5, "wave"),
           (9.0, "cube"), (12.0, "ufo"), (15.0, "ball"), (18.0, "wave"),
           (20.0, "spider"), (22.0, "robot"), (25.0, "swing")]
_MODES2 = [(0.0, "cube"), (3.0, "ship")]


def state_at(t: float) -> dict[str, Any] | None:
    """The ``state`` message at script time ``t``, or None when the game would
    not be sending one (menus, between levels, after the level is complete)."""
    if L1_START <= t < 27.5:
        lt = t - L1_START
        practice = t >= PRACTICE[0]
        p1, p2, p3 = _beats(t)
        if t < ATTEMPT1[0]:
            pct = 0.0
        elif t < ATTEMPT1[1]:
            pct = (t - ATTEMPT1[0]) / (ATTEMPT1[1] - ATTEMPT1[0]) * ATTEMPT1[2]
        elif t < ATTEMPT2[0]:
            pct = ATTEMPT1[2]                     # dead, frozen until restart
        elif t < ATTEMPT2[1]:
            pct = (t - ATTEMPT2[0]) / (ATTEMPT2[1] - ATTEMPT2[0]) * ATTEMPT2[2]
        elif t < PRACTICE[0]:
            pct = ATTEMPT2[2]
        else:
            pct = (t - PRACTICE[0]) / (PRACTICE[1] - PRACTICE[0]) * PRACTICE[2]
        if practice:
            best = 0.0
        elif t < ATTEMPT2[1]:
            best = L1_BEST
        else:
            best = ATTEMPT2[2]
        return {
            "pct": round(pct, 2), "best": best, "mode": _mode_at(_MODES1, t),
            "flip": 5.0 <= t < 6.5 or 13.0 <= t < 14.0,
            "speed": 1.0, "bg": _bg_at(_BG1, _FLASHES1, lt), "p1": [255, 80, 200],
            "pulse": p1, "pulse2": p2, "pulse3": p3,
            "accent": None if _in_windows(_NO_ACCENT1, lt) else _lerp_keys(_ACCENT1, lt),
            "practice": practice, "paused": 17.0 <= t < 17.8 and not practice,
        }
    if L2_START <= t < L2_START + L2_RUN_SECONDS:
        lt = t - L2_START
        p1, p2, p3 = _beats(t)
        return {
            "pct": round(min(100.0, lt / (L2_RUN_SECONDS - 0.05) * 100.0), 2),
            "best": 0.0,
            "mode": _mode_at(_MODES2, lt), "flip": False, "speed": 1.0,
            "bg": _bg_at(_BG2, _FLASHES2, lt), "p1": [80, 255, 160],
            "pulse": p1, "pulse2": p2, "pulse3": p3,
            "accent": None if _in_windows(_NO_ACCENT2, lt) else _lerp_keys(_ACCENT2, lt),
            "practice": False, "paused": False,
        }
    return None


class FakeGDSender:
    """Sends the scripted game to ``host:port`` as real UDP datagrams.

    ``run()`` blocks for one pass (about ``SCRIPT_END / speed`` seconds) and
    returns True when it finished; ``stop()`` (from any thread) aborts it.
    """

    def __init__(self, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST,
                 speed: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.addr = (host, port)
        self.speed = speed
        self._clock = clock
        self._stop = threading.Event()
        self.sent = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> bool:
        self._stop.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            return self._play(sock)
        finally:
            sock.close()

    def _send(self, sock: socket.socket, body: dict[str, Any]) -> None:
        data = json.dumps({"v": 1, **body}, separators=(",", ":")).encode("utf-8")
        try:
            sock.sendto(data, self.addr)
            self.sent += 1
        except OSError:
            pass            # nobody listening is fine, like the real mod

    def _play(self, sock: socket.socket) -> bool:
        events = list(TIMELINE_EVENTS)
        next_event = 0
        tick = 0
        t0 = self._clock()
        while not self._stop.is_set():
            now = (self._clock() - t0) * self.speed
            if now > SCRIPT_END:
                return True
            # everything that is due, in script order (events first at a tie)
            while True:
                state_t = tick / STATE_HZ
                event_t = events[next_event][0] if next_event < len(events) else None
                if event_t is not None and event_t <= now and event_t <= state_t:
                    self._send(sock, events[next_event][1])
                    next_event += 1
                elif state_t <= now:
                    st = state_at(state_t)
                    if st is not None:
                        self._send(sock, {"t": "state", **st})
                    tick += 1
                else:
                    break
            nxt = min(tick / STATE_HZ,
                      events[next_event][0] if next_event < len(events) else SCRIPT_END)
            wait = max(0.0, (nxt - now) / self.speed)
            self._stop.wait(min(wait, 0.05))
        return False


# ------------------------------------------------------------------- print
def _hex(c: RGB) -> str:
    return "#%02x%02x%02x" % tuple(c)


def _bar(scene: Scene) -> str:
    """16 characters, one per key column of the number row: '#' lit, 'W' marker."""
    row = {}
    for name, col in KEY_COLUMN.items():
        if name in ("ESC", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "MINUS",
                    "EQUAL", "BACKSPACE", "DEL"):
            row[col] = name
    chars = []
    for col in range(MAX_COLUMN + 1):
        name = row.get(col, row.get(col - 1))
        color = scene.keys.get(name, scene.base) if name else scene.base
        chars.append("W" if color == (255, 255, 255) and name in scene.keys
                     else "#" if name in scene.keys and color != scene.base else ".")
    return "".join(chars)


def describe(scene: Scene | None) -> str:
    if scene is None:
        return "scene=None"
    edge = _hex(scene.edge) if scene.edge else "-"
    return (f"base={_hex(scene.base)} bar[{_bar(scene)}] "
            f"W={_hex(scene.keys.get('W', scene.base))} "
            f"SPACE={_hex(scene.keys.get('SPACE', scene.base))} edge={edge}")


def run_print(speed: float = 1.0, port: int = DEFAULT_PORT, every: float = 0.5,
              out: Callable[[str], None] = print) -> bool:
    """Listener + effects + sender in one process; print a scene line every
    ``every`` real seconds and one line per event. False if the port is taken."""
    listener = GDListener(port=port)
    if not listener.start():
        out(f"cannot listen on port {port}: {listener.status}")
        return False
    effects = GDEffects(idle_color=(255, 0, 0))
    sender = FakeGDSender(port=listener.port, speed=speed)
    thread = threading.Thread(target=sender.run, daemon=True)
    t0 = time.monotonic()
    thread.start()
    next_print = 0.0
    try:
        end = max(SCRIPT_END / speed, (L2_START + L2_RUN_SECONDS) / speed + 4.0)
        while time.monotonic() - t0 < end:
            now = time.monotonic()
            state = listener.state()
            events = listener.drain_events()
            effects.update(state, events, now)
            elapsed = now - t0
            for ev in events:
                out(f"      event: {ev.kind} {ev.detail}")
            if elapsed >= next_print:
                gd = (f"{state.mode:<6} {state.pct:5.1f}% best={state.best:4.1f}"
                      f"{' flip' if state.flip else ''}{' practice' if state.practice else ''}"
                      f"{' paused' if state.paused else ''}") if state.connected else "idle"
                out(f"t={elapsed:5.1f} script={elapsed * speed:5.1f} {gd:<44} | "
                    f"{describe(effects.scene(now))}")
                next_print += every
            time.sleep(0.033)
    finally:
        sender.stop()
        listener.stop()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a Geometry Dash session.")
    parser.add_argument("--print", dest="print_", action="store_true",
                        help="run listener + effects and print the scenes")
    parser.add_argument("--send", action="store_true",
                        help="only send to the port (to test the real app window)")
    parser.add_argument("--speed", type=float, default=1.0, help="time multiplier")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if args.print_:
        run_print(speed=args.speed, port=args.port)
    elif args.send:
        print(f"sending one pass to 127.0.0.1:{args.port} at speed {args.speed}...")
        sender = FakeGDSender(port=args.port, speed=args.speed)
        try:
            sender.run()
        except KeyboardInterrupt:
            pass
        print(f"done ({sender.sent} datagrams)")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
