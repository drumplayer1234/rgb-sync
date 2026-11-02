"""League lighting effects: turns LeagueState + LeagueEvents into Scenes.

``update(state, events, now)`` is called a few times a second (it may start a
background palette download); ``scene(now)`` is called at ~30 fps and does no
I/O. All timelines (flashes, pulses, shimmer, death bar) are computed from
``now`` (seconds on any monotonic clock the caller uses consistently), so the
output is deterministic and testable.

The NuPhy ``edge`` color is a slow saved setting: it is set from the champion's
primary color only when the champion (or skin) changes, and during champ select
only after the pick has been stable for ``EDGE_DEBOUNCE`` seconds.
"""
from __future__ import annotations

import colorsys
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..scene import KEY_NAMES, RGB, Scene
from .client import LeagueEvent, LeagueState

log = logging.getLogger(__name__)

ABILITY_KEYS = ("Q", "W", "E", "R")
SUMMONER_KEYS = ("D", "F")
NUMBER_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")

RED: RGB = (255, 0, 0)
WHITE: RGB = (255, 255, 255)
GOLD: RGB = (255, 190, 0)
DEAD_GREY: RGB = (40, 40, 40)
DIM_RED: RGB = (70, 0, 0)
BARON_PURPLE: RGB = (140, 0, 255)
HERALD_VIOLET: RGB = (120, 70, 255)

DRAGON_COLORS: dict[str, RGB] = {
    "Fire": (255, 90, 0),
    "Water": (0, 120, 255),
    "Earth": (200, 120, 40),
    "Air": (150, 220, 255),
    "Hextech": (0, 230, 230),
    "Chemtech": (90, 255, 60),
    "Elder": (200, 160, 255),
}

LOW_HP = 35.0               # percent: start pulsing
CRITICAL_HP = 15.0          # percent: ability keys pulse red too
PULSE_HZ_AT_LOW = 0.8
PULSE_HZ_AT_MIN = 3.0
PULSE_MIN_HP = 10.0         # percent at which the pulse reaches PULSE_HZ_AT_MIN

IN_GAME_BRIGHTNESS = 0.7
KILL_FLASH = 0.35           # seconds
MULTIKILL_PERIOD = 0.5      # seconds between flashes
PENTA_SECONDS = 3.0
WIN_SECONDS = 4.0
LOSE_FADE_SECONDS = 3.0
LOSE_TOTAL_SECONDS = 5.0
EDGE_DEBOUNCE = 1.0         # champ select: seconds a pick must stay before edge changes


class ColorSource(Protocol):
    """What LeagueEffects needs from ChampionColors (a fake works in tests)."""

    def peek(self, champion: Any, skin_num: int | None = 0) -> tuple[RGB, RGB] | None: ...
    def prefetch(self, champion: Any, skin_num: int | None = 0) -> None: ...


def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def scale(color: RGB, factor: float) -> RGB:
    return _clamp(color[0] * factor), _clamp(color[1] * factor), _clamp(color[2] * factor)


def mix(a: RGB, b: RGB, t: float) -> RGB:
    """Linear blend: t=0 gives a, t=1 gives b."""
    t = max(0.0, min(1.0, t))
    return (_clamp(a[0] + (b[0] - a[0]) * t), _clamp(a[1] + (b[1] - a[1]) * t),
            _clamp(a[2] + (b[2] - a[2]) * t))


def pulse_hz(health_pct: float | None) -> float:
    """Low-health pulse rate: 0 at/above 35%, 0.8 Hz at 35% rising to 3 Hz at 10% and below."""
    if health_pct is None or health_pct >= LOW_HP:
        return 0.0
    frac = (LOW_HP - max(health_pct, PULSE_MIN_HP)) / (LOW_HP - PULSE_MIN_HP)
    return PULSE_HZ_AT_LOW + frac * (PULSE_HZ_AT_MIN - PULSE_HZ_AT_LOW)


@dataclass
class _Overlay:
    """A timed effect drawn over the base scene."""

    kind: str            # "flash", "pulse", "shimmer", "fade"
    start: float
    duration: float
    color: RGB = WHITE
    peak: float = 1.0    # max blend amount 0-1
    count: int = 1       # flashes only: number of flashes
    period: float = KILL_FLASH
    hold: float = 0.25   # flashes only: full-strength part of each flash
    fade: float = 0.10   # flashes only: fade-out after the hold
    priority: int = 0

    def alpha(self, now: float) -> float:
        """Blend amount at ``now`` (0 when not active)."""
        t = now - self.start
        if t < 0 or t >= self.duration:
            return 0.0
        if self.kind == "flash":
            idx = int(t // self.period)
            if idx >= self.count:
                return 0.0
            within = t - idx * self.period
            if within < self.hold:
                return self.peak
            if within < self.hold + self.fade:
                return self.peak * (1.0 - (within - self.hold) / self.fade)
            return 0.0
        if self.kind == "pulse":
            return self.peak * math.sin(math.pi * t / self.duration)
        if self.kind == "shimmer":
            return self.peak * min(1.0, t / 0.2, (self.duration - t) / 0.4)
        if self.kind == "fade":
            return self.peak * min(1.0, t / LOSE_FADE_SECONDS)
        return 0.0


def _shimmer_color(i: int, now: float) -> RGB:
    """Gold, rainbow-ish sparkle for key index ``i``."""
    hue = (43 + 16 * math.sin(now * 5.0 + i * 0.55)) / 360.0
    val = 0.8 + 0.2 * math.sin(now * 11.0 + i * 1.7)
    r, g, b = colorsys.hsv_to_rgb(hue, 1.0, val)
    return _clamp(r * 255), _clamp(g * 255), _clamp(b * 255)


class LeagueEffects:
    """Maps League state/events to lighting Scenes."""

    def __init__(self, colors: ColorSource) -> None:
        self.colors = colors
        self._state: LeagueState | None = None
        self._palette: tuple[RGB, RGB] | None = None
        self._palette_exact = False
        self._edge: RGB | None = None
        self._edge_key: tuple[Any, int] | None = None
        self._cand_key: tuple[Any, int] | None = None
        self._cand_since = 0.0
        # low-health pulse (phase integrated so rate changes don't jump)
        self._phase = 0.0
        self._phase_t = 0.0
        self._freq = 0.0
        # death countdown
        self._was_dead = False
        self._respawn_total = 0.0
        self._respawn_val = 0.0
        self._respawn_t = 0.0
        # timed overlays: slot -> overlay
        self._overlays: dict[str, _Overlay] = {}
        self._end: str | None = None
        self._end_start = 0.0

    # ----------------------------------------------------------- update
    def update(self, state: LeagueState, events: list[LeagueEvent] | tuple = (),
               now: float | None = None) -> None:
        """Feed the latest LeagueState and any new LeagueEvents."""
        now = time.monotonic() if now is None else now
        self._state = state
        if state.phase != "in_game":
            self._overlays.clear()
            self._end = None
            self._was_dead = False
            self._freq, self._phase = 0.0, 0.0
        if state.phase not in ("champ_select", "in_game"):
            self._palette = None
            self._edge = self._edge_key = self._cand_key = None
            return
        self._update_palette(state, now)
        if state.phase != "in_game":
            return
        self._update_pulse(state, now)
        self._update_death(state, now)
        for ev in events:
            self._on_event(ev, now)
        # drop finished overlays
        self._overlays = {k: o for k, o in self._overlays.items() if now < o.start + o.duration}

    def _update_palette(self, state, now: float) -> None:
        ref = state.champion or state.champion_key
        if not ref:
            self._palette = None
            return
        skin = state.skin_num or 0
        exact = self.colors.peek(ref, skin)
        if exact is None:
            self.colors.prefetch(ref, skin)  # background thread; result shows up in a later update
            exact_pal = None
            fallback = self.colors.peek(ref, 0) if skin else None
        else:
            exact_pal, fallback = exact, None
        self._palette = exact_pal or fallback
        self._palette_exact = exact_pal is not None
        if exact_pal is None:
            return
        key = (ref, skin)
        if key == self._edge_key:
            return
        if state.phase == "champ_select":
            if key != self._cand_key:
                self._cand_key, self._cand_since = key, now
                return
            if now - self._cand_since < EDGE_DEBOUNCE:
                return
        self._edge, self._edge_key = exact_pal[0], key

    def _update_pulse(self, state, now: float) -> None:
        self._phase = self._phase_at(now)
        self._phase_t = now
        self._freq = pulse_hz(None if state.is_dead else state.health_pct)
        if self._freq == 0.0:
            self._phase = 0.0

    def _phase_at(self, now: float) -> float:
        return self._phase + self._freq * (now - self._phase_t)

    def _update_death(self, state, now: float) -> None:
        if state.is_dead:
            resp = max(0.0, state.respawn_in or 0.0)
            self._respawn_total = resp if not self._was_dead else max(self._respawn_total, resp)
            self._respawn_val, self._respawn_t = resp, now
        self._was_dead = state.is_dead

    # ----------------------------------------------------------- events
    def _set_overlay(self, slot: str, ov: _Overlay) -> None:
        cur = self._overlays.get(slot)
        if cur is not None and cur.start == ov.start and cur.priority > ov.priority:
            return  # a higher-priority effect from the same instant wins
        self._overlays[slot] = ov

    def _on_event(self, ev, now: float) -> None:
        kind = ev.kind
        if kind == "champion_kill" and ev.mine:
            assist = bool(ev.detail.get("assist"))
            self._set_overlay("kill", _Overlay("flash", now, KILL_FLASH, WHITE,
                                               peak=0.5 if assist else 1.0, priority=1))
        elif kind == "multikill" and ev.mine:
            n = int(ev.detail.get("kill_streak") or 0)
            if n >= 5:
                self._set_overlay("penta", _Overlay("shimmer", now, PENTA_SECONDS, GOLD, priority=5))
            elif n >= 2:
                dur = (n - 1) * MULTIKILL_PERIOD + KILL_FLASH
                self._set_overlay("kill", _Overlay("flash", now, dur, WHITE, count=n,
                                                   period=MULTIKILL_PERIOD, priority=2))
        elif kind == "first_blood" and ev.mine:
            self._set_overlay("kill", _Overlay("flash", now, 0.5, RED, period=0.5, hold=0.3,
                                               fade=0.2, priority=3))
        elif kind == "dragon_kill":
            color = DRAGON_COLORS.get(str(ev.detail.get("dragon")), WHITE)
            if ev.my_team:
                self._set_overlay("objective", _Overlay("pulse", now, 1.0, color, peak=0.9))
            else:
                self._set_overlay("objective", _Overlay("pulse", now, 0.5, color, peak=0.4))
        elif kind == "baron_kill":
            if ev.my_team:
                self._set_overlay("objective", _Overlay("pulse", now, 2.0, BARON_PURPLE, peak=0.9))
            else:
                self._set_overlay("objective", _Overlay("pulse", now, 1.0, BARON_PURPLE, peak=0.4))
        elif kind == "herald_kill" and ev.my_team:
            self._set_overlay("objective", _Overlay("pulse", now, 1.0, HERALD_VIOLET, peak=0.8))
        elif kind == "ace" and ev.my_team:
            self._set_overlay("objective", _Overlay("flash", now, 1.0, GOLD, period=1.0, hold=0.6, fade=0.4))
        elif kind == "game_end" and self._end is None:
            win = str(ev.detail.get("result", "")).lower().startswith("win")
            self._end = "win" if win else "lose"
            self._end_start = now
            if win:
                self._overlays["end"] = _Overlay("shimmer", now, WIN_SECONDS, GOLD, priority=9)
            else:
                self._overlays["end"] = _Overlay("fade", now, LOSE_TOTAL_SECONDS, DIM_RED, priority=9)

    # ------------------------------------------------------------ scene
    def scene(self, now: float) -> Scene | None:
        """The scene to show at ``now``, or None to leave the app's normal color."""
        state = self._state
        if state is None or self._palette is None:
            return None
        if state.phase == "champ_select":
            primary, secondary = self._palette
            keys = {k: secondary for k in ABILITY_KEYS + SUMMONER_KEYS}
            return Scene(base=primary, keys=keys, edge=self._edge)
        if state.phase != "in_game":
            return None
        if self._end is not None:
            total = WIN_SECONDS if self._end == "win" else LOSE_TOTAL_SECONDS
            if now - self._end_start >= total:
                return None
        scene = self._dead_scene(now) if state.is_dead else self._alive_scene(now)
        for slot in ("objective", "kill", "penta", "end"):
            ov = self._overlays.get(slot)
            if ov is not None:
                self._apply(scene, ov, now)
        return scene

    def _alive_scene(self, now: float) -> Scene:
        primary, secondary = self._palette  # type: ignore[misc]
        base = scale(primary, IN_GAME_BRIGHTNESS)
        ability = secondary
        summoner = mix(primary, WHITE, 0.7)
        hp = self._state.health_pct
        if hp is not None and hp < LOW_HP:
            m = 0.5 - 0.5 * math.cos(2 * math.pi * self._phase_at(now))
            base = mix(base, RED, m)
            if hp < CRITICAL_HP:
                ability = mix(secondary, RED, m)
        keys = {k: ability for k in ABILITY_KEYS}
        keys.update({k: summoner for k in SUMMONER_KEYS})
        return Scene(base=base, keys=keys, edge=self._edge)

    def _dead_scene(self, now: float) -> Scene:
        primary = self._palette[0]  # type: ignore[index]
        if self._respawn_total > 0:
            remaining = max(0.0, self._respawn_val - (now - self._respawn_t))
            lit = math.ceil(min(1.0, remaining / self._respawn_total) * len(NUMBER_KEYS) - 1e-9)
        else:
            lit = 0
        keys = {k: primary for k in NUMBER_KEYS[:max(0, lit)]}
        return Scene(base=DEAD_GREY, keys=keys, edge=self._edge)

    @staticmethod
    def _apply(scene: Scene, ov: _Overlay, now: float) -> None:
        a = ov.alpha(now)
        if a <= 0.0:
            return
        if ov.kind == "shimmer":
            for i, name in enumerate(KEY_NAMES):
                scene.keys[name] = mix(scene.keys.get(name, scene.base), _shimmer_color(i, now), a)
            scene.base = mix(scene.base, _shimmer_color(len(KEY_NAMES), now), a)
            return
        scene.base = mix(scene.base, ov.color, a)
        for name, color in list(scene.keys.items()):
            scene.keys[name] = mix(color, ov.color, a)
