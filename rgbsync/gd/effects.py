"""Geometry Dash lighting effects: turns GDState + GDEvents into Scenes.

``update(state, events, now)`` is called every tick (or a few times a second);
``scene(now)`` is called at ~30 fps, does no I/O and is deterministic given the
sequence of ``now`` values (all timelines come from ``now``; the only state is
the 0.15 s color smoothing). Fans and other non-keyboard devices show only
``Scene.base``, so every full-device effect goes through ``base`` too.

Layers, bottom to top: level background color (``base``) -> progress bar and
mode accent keys -> beat pulse -> paused dimming -> checkpoint flash -> death
burst -> level complete celebration.

``Scene.edge`` (NuPhy halo and side lights) is a slow saved setting, so it is
chosen once per level from the difficulty and stays constant; ``None`` whenever
the game is not connected.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable

from ..scene import RGB, Scene
from .listener import GDEvent, GDState

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ colors
RED: RGB = (255, 0, 0)
WHITE: RGB = (255, 255, 255)
GOLD: RGB = (255, 190, 0)
PRACTICE_GREEN: RGB = (30, 160, 70)
CHECKPOINT_GREEN: RGB = (0, 255, 60)

MODE_COLORS: dict[str, RGB] = {
    "cube": (255, 200, 0), "ship": (255, 60, 160), "ball": (255, 110, 0),
    "ufo": (170, 60, 255), "wave": (0, 220, 255), "robot": (255, 40, 40),
    "spider": (60, 255, 90), "swing": (40, 110, 255),
}
ACCENT_KEYS = ("W", "A", "S", "D", "UP", "LEFT", "DOWN", "RIGHT", "SPACE")

EDGE_COLORS: dict[str, RGB] = {
    "auto": WHITE, "unrated": WHITE, "easy": (0, 255, 255), "normal": (0, 255, 0),
    "hard": (255, 255, 0), "harder": (255, 120, 0), "insane": (255, 0, 255),
    "demon": (255, 0, 0),
}

# ----------------------------------------------------------------- key layout
#: Copy of the Halo65 LED slots (slot = column * 6 + row) from the NuPhy backend;
#: only the column is used, as a rough physical x position of each key.
_HALO65_SLOTS: dict[str, int] = {
    "ESC": 1, "1": 7, "2": 13, "3": 19, "4": 25, "5": 31, "6": 37, "7": 43,
    "8": 49, "9": 55, "0": 61, "MINUS": 67, "EQUAL": 73, "BACKSPACE": 79,
    "TAB": 2, "Q": 8, "W": 14, "E": 20, "R": 26, "T": 32, "Y": 38, "U": 44,
    "I": 50, "O": 56, "P": 62, "LBRACKET": 68, "RBRACKET": 74,
    "BACKSLASH": 80,
    "CAPS": 3, "A": 9, "S": 15, "D": 21, "F": 27, "G": 33, "H": 39, "J": 45,
    "K": 51, "L": 57, "SEMICOLON": 63, "QUOTE": 69, "ENTER": 81,
    "LSHIFT": 4, "Z": 10, "X": 16, "C": 22, "V": 28, "B": 34, "N": 40,
    "M": 46, "COMMA": 52, "PERIOD": 58, "SLASH": 64, "RSHIFT": 82, "UP": 88,
    "LCTRL": 5, "LWIN": 11, "LALT": 17, "SPACE": 35, "RALT": 53, "FN": 59,
    "LEFT": 83, "DOWN": 89, "RIGHT": 95,
    "DEL": 91, "PGUP": 92, "PGDN": 93, "END": 94,
}
#: Key name -> LED column index (0 = left edge).
KEY_COLUMN: dict[str, int] = {k: slot // 6 for k, slot in _HALO65_SLOTS.items()}
MAX_COLUMN = max(KEY_COLUMN.values())
#: Key name -> approximate physical position, 0.0 (left) to 1.0 (right).
KEY_POSITION: dict[str, float] = {k: c / MAX_COLUMN for k, c in KEY_COLUMN.items()}
_KEYS_IN_COLUMN: dict[int, tuple[str, ...]] = {
    c: tuple(k for k, kc in KEY_COLUMN.items() if kc == c)
    for c in range(MAX_COLUMN + 1)}
_ALL_KEYS = tuple(KEY_COLUMN)
_CENTER = MAX_COLUMN / 2.0
#: Key -> distance from the keyboard's center column, 0.0 (middle) to 1.0 (edge).
_KEY_RADIUS: dict[str, float] = {
    k: abs(c - _CENTER) / _CENTER for k, c in KEY_COLUMN.items()}

# ------------------------------------------------------------------- tuning
BASE_FLOOR, BASE_CEIL = 0.35, 0.80      # level color brightness range (pulse adds on top)
PLAYER_FLOOR, PLAYER_CEIL = 0.65, 1.0
SMOOTH_SECONDS = 0.15                   # color smoothing settles in about this long
PULSE_GAIN = 0.4                        # pulse 1.0 -> +40% brightness
PAUSED_BRIGHTNESS = 0.25
CHECKPOINT_SECONDS = 0.25
BURST_SECONDS = 0.6
BURST_REACH = 1.3                       # ring radius at the end of the burst
BURST_LEAD, BURST_TAIL = 0.12, 0.5      # ring softness ahead of / behind the front
GOLD_FLASH_SECONDS = 0.5
RAINBOW_SECONDS = 3.0
RAINBOW_FADE = 0.3


# ------------------------------------------------------------ color helpers
def _clamp(v: float) -> int:
    return 0 if v <= 0 else 255 if v >= 255 else int(v + 0.5)


def scale(color: RGB, factor: float) -> RGB:
    return _clamp(color[0] * factor), _clamp(color[1] * factor), _clamp(color[2] * factor)


def mix(a: RGB, b: RGB, t: float) -> RGB:
    """Linear blend: t=0 gives a, t=1 gives b."""
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    return (_clamp(a[0] + (b[0] - a[0]) * t), _clamp(a[1] + (b[1] - a[1]) * t),
            _clamp(a[2] + (b[2] - a[2]) * t))


def boost(color: RGB, floor: float, ceil: float) -> RGB:
    """Remap brightness (the largest channel) from 0..1 to floor..ceil, keeping
    hue and saturation. Black becomes a grey of brightness ``floor``."""
    peak = max(color) / 255.0
    if peak <= 0.0:
        g = _clamp(255 * floor)
        return g, g, g
    return scale(color, (floor + (ceil - floor) * peak) / peak)


def boost_bg(color: RGB) -> RGB:
    """Level background color made bright enough for LEDs (35%..80%)."""
    return boost(color, BASE_FLOOR, BASE_CEIL)


def boost_player(color: RGB) -> RGB:
    """Player color for the progress bar (65%..100%)."""
    return boost(color, PLAYER_FLOOR, PLAYER_CEIL)


def hue_color(h: float) -> RGB:
    """Fully saturated, full brightness color for hue ``h`` (0..1, wraps)."""
    x = (h % 1.0) * 6.0
    i = int(x)
    f = x - i
    up, down = _clamp(255 * f), _clamp(255 * (1.0 - f))
    return ((255, up, 0), (down, 255, 0), (0, 255, up), (0, down, 255),
            (up, 0, 255), (255, 0, down))[i % 6]


def edge_for(difficulty: str) -> RGB:
    """Edge-light color for a level difficulty."""
    return EDGE_COLORS.get(difficulty, WHITE)


# ------------------------------------------------------------------ effects
class GDEffects:
    """Maps Geometry Dash state/events to lighting Scenes."""

    def __init__(self, idle_color: RGB = RED) -> None:
        #: Shown while the game is not connected (the app's chosen color).
        self.idle_color: RGB = idle_color
        self._state: GDState | None = None      # latest state, as given
        self._data = GDState()                  # latest state seen while connected
        self._edge: RGB | None = None
        self._smooth: tuple[float, float, float] | None = None
        self._smooth_t = 0.0
        self._p1_src: RGB | None = None
        self._p1_boosted: RGB = WHITE
        self._death_t: float | None = None
        self._death_gold = False
        self._complete_t: float | None = None
        self._complete_gold = False
        self._checkpoint_t: float | None = None
        self._failed = False

    # ----------------------------------------------------------- update
    def update(self, state: GDState, events: list[GDEvent] | tuple = (),
               now: float | None = None) -> None:
        """Feed the latest GDState and any new GDEvents."""
        now = time.monotonic() if now is None else now
        self._state = state
        for ev in events:
            self._on_event(ev, now)
        if state.connected:
            self._data = state
            if self._edge is None:             # joined mid-level: no level_start seen
                self._edge = edge_for(state.difficulty)
        else:
            self._smooth = None                # snap to the level color on reconnect
            self._death_t = self._checkpoint_t = None

    def _on_event(self, ev: GDEvent, now: float) -> None:
        kind = ev.kind
        if kind == "level_start":
            self._edge = edge_for(str(ev.detail.get("difficulty", "unrated")))
            self._death_t = self._complete_t = self._checkpoint_t = None
        elif kind == "level_exit":
            self._edge = None
            self._death_t = self._checkpoint_t = None
        elif kind == "death":
            self._death_t = now
            self._death_gold = bool(ev.detail.get("new_best"))
        elif kind == "checkpoint":
            self._checkpoint_t = now
        elif kind == "complete":
            self._complete_t = now
            self._complete_gold = bool(ev.detail.get("new_best"))
            self._death_t = None
        # "restart" is deliberately ignored: it must not cancel a running burst.

    # ------------------------------------------------------------ scene
    def scene(self, now: float) -> Scene | None:
        """The scene to show at ``now``. Never raises."""
        try:
            return self._scene(now)
        except Exception:
            if not self._failed:
                self._failed = True
                log.exception("Geometry Dash scene crashed (shown once)")
            return Scene(base=self.idle_color)

    def _complete_total(self) -> float:
        return RAINBOW_SECONDS + (GOLD_FLASH_SECONDS if self._complete_gold else 0.0)

    def _scene(self, now: float) -> Scene:
        st = self._state
        connected = st is not None and st.connected
        celebrating = (self._complete_t is not None
                       and 0.0 <= now - self._complete_t < self._complete_total())
        if not connected and not celebrating:
            return Scene(base=self.idle_color, edge=None)
        data = self._data

        # level background color, smoothed
        target = boost_bg(data.bg)
        sm = self._smooth
        if sm is None:
            sm = (float(target[0]), float(target[1]), float(target[2]))
        else:
            k = 1.0 - math.exp(-3.0 * max(0.0, now - self._smooth_t) / SMOOTH_SECONDS)
            sm = (sm[0] + (target[0] - sm[0]) * k, sm[1] + (target[1] - sm[1]) * k,
                  sm[2] + (target[2] - sm[2]) * k)
        self._smooth, self._smooth_t = sm, now
        base: RGB = PRACTICE_GREEN if data.practice else (
            _clamp(sm[0]), _clamp(sm[1]), _clamp(sm[2]))

        keys: dict[str, RGB] = {}
        if not data.practice and not data.platformer:
            self._progress_bar(keys, data)
        accent = MODE_COLORS.get(data.mode, MODE_COLORS["cube"])
        for name in ACCENT_KEYS:
            keys[name] = accent

        if data.paused:
            base = scale(base, PAUSED_BRIGHTNESS)
            for name, color in keys.items():
                keys[name] = scale(color, PAUSED_BRIGHTNESS)
        elif data.pulse > 0.0:
            gain = 1.0 + PULSE_GAIN * data.pulse
            base = scale(base, gain)
            for name, color in keys.items():
                keys[name] = scale(color, gain)

        if self._checkpoint_t is not None:
            base = self._checkpoint_flash(base, keys, now)
        if self._death_t is not None:
            base = self._death_burst(base, keys, now)
        if celebrating:
            base = self._celebrate(base, keys, now)
        return Scene(base=base, keys=keys, edge=self._edge if connected else None)

    # ------------------------------------------------------------ layers
    def _progress_bar(self, keys: dict[str, RGB], data: GDState) -> None:
        if data.pct <= 0.0 and data.best <= 0.0:
            return
        if data.p1 != self._p1_src:
            self._p1_src, self._p1_boosted = data.p1, boost_player(data.p1)
        reach = data.pct / 100.0 * MAX_COLUMN + 1e-9
        flip = data.flip
        if data.pct > 0.0:
            for name, col in KEY_COLUMN.items():
                if (MAX_COLUMN - col if flip else col) <= reach:
                    keys[name] = self._p1_boosted
        if data.best > data.pct:                       # personal best marker
            col = int(data.best / 100.0 * MAX_COLUMN + 0.5)
            if flip:
                col = MAX_COLUMN - col
            for name in _KEYS_IN_COLUMN.get(col, ()):
                keys[name] = WHITE

    def _checkpoint_flash(self, base: RGB, keys: dict[str, RGB], now: float) -> RGB:
        t = now - (self._checkpoint_t or 0.0)
        if not 0.0 <= t < CHECKPOINT_SECONDS:
            return base
        a = 1.0 if t < 0.15 else 1.0 - (t - 0.15) / (CHECKPOINT_SECONDS - 0.15)
        for name in _ALL_KEYS:
            keys[name] = mix(keys.get(name, base), CHECKPOINT_GREEN, a)
        return mix(base, CHECKPOINT_GREEN, a)

    def _death_burst(self, base: RGB, keys: dict[str, RGB], now: float) -> RGB:
        """Red (gold on a new best) ring spreading from the keyboard's center;
        ``base`` (fans etc.) fades from full color to nothing."""
        t = now - (self._death_t or 0.0)
        if not 0.0 <= t < BURST_SECONDS:
            return base
        color = GOLD if self._death_gold else RED
        life = t / BURST_SECONDS
        fade = 1.0 - life * life
        front = BURST_REACH * life
        for name in _ALL_KEYS:
            delta = front - _KEY_RADIUS[name]       # > 0: the ring already passed
            if delta >= 0.0:
                a = 1.0 - delta / BURST_TAIL
            else:
                a = 1.0 + delta / BURST_LEAD
            if a > 0.0:
                keys[name] = mix(keys.get(name, base), color, a * fade)
            elif name not in keys:
                keys[name] = base
        return mix(base, color, 1.0 - life)

    def _celebrate(self, base: RGB, keys: dict[str, RGB], now: float) -> RGB:
        """Gold for 0.5 s on a new best, then a 3 s rainbow sweep."""
        t = now - (self._complete_t or 0.0)
        if self._complete_gold:
            if t < GOLD_FLASH_SECONDS:
                for name in _ALL_KEYS:
                    keys[name] = GOLD
                return GOLD
            t -= GOLD_FLASH_SECONDS
        a = min(1.0, (RAINBOW_SECONDS - t) / RAINBOW_FADE)
        for name in _ALL_KEYS:
            hue = t * 0.6 - KEY_POSITION[name] * 0.8
            keys[name] = mix(keys.get(name, base), hue_color(hue), a)
        return mix(base, hue_color(t * 0.9), a)


def make_source(listener, effects: GDEffects) -> Callable[[float], Scene | None]:
    """A LiveEngine source: feeds the listener's data to ``effects`` each tick.

    Runs on the engine thread; ``effects`` must not be used from other threads
    (except setting ``idle_color``).
    """
    def source(now: float) -> Scene | None:
        effects.update(listener.state(), listener.drain_events(), now)
        return effects.scene(now)
    return source
