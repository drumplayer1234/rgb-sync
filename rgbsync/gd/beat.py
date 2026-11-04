"""Beat detection from the game's raw music meters (``pulse``, ``pulse2``, ``pulse3``).

The meters are unnormalized (0-16, whatever the level's music does) and older
mods clamp them to 1, so the raw value cannot drive brightness directly. The
``BeatTracker`` keeps a few seconds of history per meter, scales the newest
sample into 0-1 using the window's low and high percentiles, and reports how far
it is above the window's mean. The output is a 0-1 *beat level* with an instant
attack and a fast linear decay, so a hit punches and then drops.

Pure and deterministic: time is always passed in (``now``), never read.
"""
from __future__ import annotations

from collections import deque
from typing import Sequence

WINDOW_SECONDS = 5.0        # history used for the auto-scale
DECAY_SECONDS = 0.18        # a full-strength beat falls to 0 in this long
MIN_SPAN = 0.6              # seconds of history needed before any output
MIN_SAMPLES = 12            # so one stray sample cannot set the 5% / 95% marks
MIN_RANGE = 0.05            # (high - low) below this is a flat signal, in raw units
MIN_RELATIVE_RANGE = 0.10   # ... and so is (high - low) / high below this
LOW_PCT, HIGH_PCT = 0.05, 0.95
SWITCH_RATIO = 1.3          # another meter must be this much more dynamic to take over
ONSET_WINDOW = 0.15         # a beat's rising edge may keep lifting the level this long
EDGE_FALL = 0.05            # ... until the hit weakens by this much (then it is over)
NEW_BEAT_MARGIN = 0.10      # a new beat must beat the decaying level by this much
_MAX_SAMPLES = 1500


def _percentile(sorted_values: list[float], p: float) -> float:
    return sorted_values[int(round(p * (len(sorted_values) - 1)))]


class BeatTracker:
    """Turns raw meter samples into a 0-1 beat level."""

    def __init__(self, window: float = WINDOW_SECONDS, decay: float = DECAY_SECONDS,
                 channels: int = 3, attack: float = 0.0,
                 max_per_second: float = 0.0) -> None:
        self.window = window
        self.decay = decay
        #: Seconds the level takes to rise to a beat (0 = instantly).
        self.attack = max(0.0, attack)
        #: Longest-run beat rate that gets through (0 = no limit).
        self.max_per_second = max(0.0, max_per_second)
        #: Beats that got through since the last ``reset``.
        self.onsets = 0
        self._out0 = 0.0                       # output level at ``_level_t``
        self._onset_t: float | None = None
        self._edge_open = False                # still on the rising edge of the last beat
        self._prev_strength = 0.0
        self._hist: list[deque[tuple[float, float]]] = [
            deque(maxlen=_MAX_SAMPLES) for _ in range(channels)]
        self._level = 0.0
        self._level_t = 0.0
        self._last_t: float | None = None
        #: Index of the meter currently followed (the most dynamic), or None.
        self.channel: int | None = None

    def reset(self) -> None:
        for q in self._hist:
            q.clear()
        self._level = 0.0
        self._out0 = 0.0
        self._onset_t = None
        self._edge_open = False
        self._prev_strength = 0.0
        self.onsets = 0
        self._last_t = None
        self.channel = None

    # ------------------------------------------------------------------ output
    def _raw(self, now: float) -> float:
        """The beat envelope: held for ``attack`` s after the last raise, then falling."""
        dt = max(0.0, now - self._level_t)
        fall = max(0.0, dt - self.attack)
        return max(0.0, min(1.0, self._level - fall / self.decay))

    def level(self, now: float) -> float:
        """Beat level at ``now`` (0 to 1), as of the last ``update``."""
        raw = self._raw(now)
        if self.attack <= 0.0:
            return raw
        dt = max(0.0, now - self._level_t)
        return min(raw, self._out0 + dt / self.attack)     # rises at most 1 / attack per s

    # ------------------------------------------------------------------- input
    def update(self, values: Sequence[float], now: float) -> float:
        """Feed one sample per meter taken at ``now``; returns the new level."""
        if self._last_t is not None and now < self._last_t - 1.0:
            self.reset()                       # clock jumped back: start over
        self._last_t = now
        current = self._raw(now)
        shown = self.level(now)

        stats = []                             # (score, lo, hi, mean) per meter
        for i, q in enumerate(self._hist):
            v = float(values[i]) if i < len(values) else 0.0
            q.append((now, v))
            while q and now - q[0][0] > self.window:
                q.popleft()
            stats.append(self._stats(q, now))

        best: int | None = None
        for i, st in enumerate(stats):
            if st is not None and (best is None or st[0] > stats[best][0]):
                best = i
        cur = self.channel
        if (cur is not None and stats[cur] is not None and best is not None
                and stats[best][0] < stats[cur][0] * SWITCH_RATIO):
            best = cur                         # no flip-flopping between meters
        self.channel = best

        strength = 0.0
        if best is not None:
            _score, lo, hi, mean = stats[best]
            span = hi - lo
            x = min(1.0, max(0.0, (self._hist[best][-1][1] - lo) / span))
            thr = min(1.0, max(0.0, (mean - lo) / span))
            if x > thr:
                strength = min(1.0, (x - thr) / max(1.0 - thr, 0.2))
        if strength < self._prev_strength - EDGE_FALL:
            self._edge_open = False            # the hit peaked: a later rise is a new hit
        self._prev_strength = strength
        if strength > current and self._accept(strength, current, now):
            self._level = strength
            self._out0 = shown
            self._level_t = now
        return self.level(now)

    def _accept(self, strength: float, current: float, now: float) -> bool:
        """Whether a hit that would raise the level ``current`` to ``strength`` gets
        through the beat rate limit."""
        if self.max_per_second <= 0.0:
            return True
        since = float("inf") if self._onset_t is None else now - self._onset_t
        if self._edge_open and since < ONSET_WINDOW:
            return True                        # still the rising edge of the same beat
        if since >= 1.0 / self.max_per_second and strength > current + NEW_BEAT_MARGIN:
            self._onset_t = now
            self._edge_open = True
            self.onsets += 1
            return True
        return False

    @staticmethod
    def _stats(q: deque[tuple[float, float]], now: float):
        """(relative range, low, high, mean) of a meter's window, or None when
        the window is too short or the signal is flat."""
        n = len(q)
        if n < MIN_SAMPLES or q[-1][0] - q[0][0] < MIN_SPAN:
            return None
        vals = sorted(v for _t, v in q)
        lo, hi = _percentile(vals, LOW_PCT), _percentile(vals, HIGH_PCT)
        rng = hi - lo
        if rng < MIN_RANGE:
            return None
        rel = rng / max(hi, 0.5)
        if rel < MIN_RELATIVE_RANGE:
            return None
        return rel, lo, hi, sum(vals) / n
