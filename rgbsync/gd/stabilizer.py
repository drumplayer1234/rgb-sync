"""Color stabilizer: keeps the level color calm.

The mod's ``accent`` (and the background fallbacks) can change many times a
second. Shown directly that is a strobe, so every tick the *chosen* source color
goes through a ``ColorStabilizer`` and only its output is displayed:

* A hue change of more than ``small_degrees`` (25) must be seen for
  ``persist`` seconds (0.3) in a row before it is adopted, so one-frame or short
  triggers are ignored. Going back to the shown hue cancels the wait.
* After a color is adopted it is held for at least ``hold`` seconds (1.0) before
  the next big change can be adopted. Together: at most one color change a second.
* An adopted change is a crossfade through RGB over ``fade`` seconds (0.35), not
  a snap.
* Hue changes of ``small_degrees`` or less just track the source smoothly (settling
  in about ``track`` seconds), with no wait and no hold.
* The first color after construction or ``reset()`` (a level start or a
  reconnect) is shown immediately, with no fade, and starts no hold.

Colors are RGB float triples (0-255). ``hue`` is 0..1, or ``None`` for a color
without a hue (the dim neutral): it counts as far from every real hue. Pure and
deterministic: time is always passed in (``now``), never read.
"""
from __future__ import annotations

import math

Color = tuple[float, float, float]

SMALL_DEGREES = 25.0        # hue moves up to this track smoothly; bigger ones are gated
PERSIST_SECONDS = 0.3       # a big change must last this long to be adopted
HOLD_SECONDS = 1.0          # minimum time between adopted big changes
FADE_SECONDS = 0.35         # crossfade length of an adopted change
TRACK_SECONDS = 0.15        # small changes settle in about this long
_EPS = 1e-9                  # so 0.1 s steps summing to 0.3 count as 0.3


def hue_distance(a: float | None, b: float | None) -> float:
    """Degrees between two hues (0..1), 0 to 180; 180 when exactly one is None."""
    if a is None or b is None:
        return 0.0 if a is b else 180.0
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d) * 360.0


def _lerp(a: Color, b: Color, t: float) -> Color:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


class ColorStabilizer:
    """``update(color, hue, now)`` -> the color to display."""

    def __init__(self, small_degrees: float = SMALL_DEGREES,
                 persist: float = PERSIST_SECONDS, hold: float = HOLD_SECONDS,
                 fade: float = FADE_SECONDS, track: float = TRACK_SECONDS) -> None:
        self.small_degrees = small_degrees
        self.persist = persist
        self.hold = hold
        self.fade = fade
        self.track = track
        #: Big color changes adopted since the last ``reset`` (not the first color).
        self.changes = 0
        self.reset()

    def reset(self) -> None:
        """Forget everything: the next ``update`` shows its color immediately."""
        self._goal: Color | None = None        # where the shown color is heading
        self._track: Color = (0.0, 0.0, 0.0)   # smoothed goal
        self._hue: float | None = None         # hue of the adopted color
        self._from: Color | None = None        # crossfade start (None: not fading)
        self._fade_t = 0.0
        self._adopted_t: float | None = None   # when the last big change was adopted
        self._pending: list | None = None      # [color, hue, since] of a waiting change
        self._t = 0.0
        self.changes = 0

    # ----------------------------------------------------------------- output
    def _shown(self, now: float) -> Color:
        if self._from is None:
            return self._track
        p = (now - self._fade_t) / self.fade if self.fade > 0.0 else 1.0
        if p >= 1.0:
            self._from = None
            return self._track
        p = max(0.0, p)
        return _lerp(self._from, self._track, p * p * (3.0 - 2.0 * p))   # smoothstep

    # ------------------------------------------------------------------ input
    def update(self, color: Color, hue: float | None, now: float) -> Color:
        """Feed this tick's source color (and its hue); returns the color to show."""
        if self._goal is None or now < self._t - 1.0:    # first color / clock jumped back
            self.reset()
            self._goal = self._track = color
            self._hue, self._t = hue, now
            return color
        dt = max(0.0, now - self._t)
        self._t = now
        if hue_distance(hue, self._hue) <= self.small_degrees:
            self._goal, self._hue, self._pending = color, hue, None
        else:
            self._consider(color, hue, now)
        if self.track > 0.0 and dt > 0.0:
            self._track = _lerp(self._track, self._goal,
                                1.0 - math.exp(-3.0 * dt / self.track))
        elif self.track <= 0.0:
            self._track = self._goal
        return self._shown(now)

    def _consider(self, color: Color, hue: float | None, now: float) -> None:
        """A color of a clearly different hue is on offer: wait, then adopt it."""
        pend = self._pending
        if pend is None or hue_distance(hue, pend[1]) > self.small_degrees:
            pend = self._pending = [color, hue, now]
        else:
            pend[0], pend[1] = color, hue                # follow a drifting offer
        if (now - pend[2] >= self.persist - _EPS
                and (self._adopted_t is None or now - self._adopted_t >= self.hold - _EPS)):
            self._from = self._shown(now)                # fade from what is on the lights
            self._fade_t = now
            self._goal = self._track = color
            self._hue = hue
            self._adopted_t = now
            self._pending = None
            self.changes += 1
