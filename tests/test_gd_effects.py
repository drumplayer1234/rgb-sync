"""Geometry Dash effects tests: deterministic timelines driven by explicit ``now``.

No game, no network, no hardware. Run: python tests/test_gd_effects.py
"""
import colorsys
import logging
import math
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.gd.beat import BeatTracker  # noqa: E402
from rgbsync.gd.effects import (  # noqa: E402
    ACCENT_KEYS, CHECKPOINT_GREEN, EDGE_COLORS, GOLD, KEY_COLUMN, MAX_COLUMN,
    MODE_COLORS, PRACTICE_GREEN, REST_BRIGHTNESS, GDEffects, boost_player,
    led_gamma, saturate, scale, vividness)
from rgbsync.gd.listener import GDEvent, GDState  # noqa: E402
from rgbsync.scene import KEY_NAMES  # noqa: E402

IDLE = (12, 34, 56)
BG = (20, 40, 120)
P1 = (255, 0, 128)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
VIVID_P1 = (125, 255, 0)        # the player color of the real recording
T0 = 1000.0
#: Column 14 holds only arrow keys (accent keys), so it never shows the bar.
ALL_COLS = set(range(16)) - {14}


def gd(**kw) -> GDState:
    """A connected playing state with calm defaults."""
    fields = dict(pct=0.0, best=0.0, mode="cube", flip=False, bg=BG, p1=P1,
                  pulse=0.0, practice=False, paused=False, connected=True,
                  level_name="T", difficulty="hard")
    fields.update(kw)
    return GDState(**fields)


def ev(kind: str, **detail) -> GDEvent:
    return GDEvent(kind, detail)


def fx(**kw) -> GDEffects:
    e = GDEffects(idle_color=IDLE)
    e.update(gd(**kw), [ev("level_start", difficulty="hard")], T0)
    return e


def non_accent(scene):
    return {k: v for k, v in scene.keys.items() if k not in ACCENT_KEYS}


def hue_of(c) -> float:
    return colorsys.rgb_to_hsv(*(x / 255 for x in c))[0]


def sat_of(c) -> float:
    return colorsys.rgb_to_hsv(*(x / 255 for x in c))[1]


def val_of(c) -> float:
    return max(c) / 255


def hue_gap(a, b) -> float:
    d = abs(hue_of(a) - hue_of(b))
    return min(d, 1 - d)


def level_color(c):
    """The level color of ``c`` at full brightness: same hue, saturation >= 0.85."""
    h, s_, _v = colorsys.rgb_to_hsv(*(x / 255 for x in c))
    return tuple(int(round(x * 255)) for x in colorsys.hsv_to_rgb(h, max(s_, 0.85), 1.0))


def lit(c, beat: float = 0.0):
    """Final (post-gamma) base color for source ``c`` at the given beat level."""
    return led_gamma(scale(level_color(c), REST_BRIGHTNESS + (1 - REST_BRIGHTNESS) * beat))


#: Progress bar color as it reaches the LEDs (player color boosted, enriched, gamma).
BAR = led_gamma(saturate(boost_player(P1)))
#: The same, for the default test level (vivid bg BG) at rest.
REST_BG = lit(BG)


def lit_columns(scene, color) -> set[int]:
    return {KEY_COLUMN[k] for k, v in scene.keys.items()
            if v == color and k not in ACCENT_KEYS}


def kick(t: float) -> float:
    """Raw pulse like the game's meter: 2 Hz bursts between about 0.6 and 14."""
    return 0.6 + 13.4 * max(0.0, math.cos(2 * math.pi * 2 * t)) ** 2


def play(e: GDEffects, t0: float, seconds: float, signal=kick, hz: int = 30, **kw):
    """Feed ``signal(t - t0)`` as the pulse at ``hz``; yields (t - t0, Scene)."""
    for i in range(int(seconds * hz)):
        t = t0 + i / hz
        e.update(gd(pulse=signal(t - t0), **kw), [], t)
        yield t - t0, e.scene(t)


def test_key_table_matches_layout() -> None:
    assert set(KEY_COLUMN) <= set(KEY_NAMES)
    assert KEY_COLUMN["ESC"] == 0 and KEY_COLUMN["SPACE"] == 5 and MAX_COLUMN == 15
    assert KEY_COLUMN["BACKSPACE"] > KEY_COLUMN["Q"] > KEY_COLUMN["TAB"] == 0
    # kept in sync with the real backend's slot table (column = slot // 6)
    from rgbsync.backends.nuphy import HALO65_KEY_SLOTS
    assert KEY_COLUMN == {k: s // 6 for k, s in HALO65_KEY_SLOTS.items()}


def test_idle_when_not_connected() -> None:
    e = GDEffects(idle_color=IDLE)
    assert e.scene(T0) == type(e.scene(T0))(base=IDLE, keys={}, edge=None)
    e.update(GDState(), [], T0)                       # connected=False
    s = e.scene(T0)
    assert s.base == IDLE and s.keys == {} and s.edge is None
    e.idle_color = (1, 2, 3)                          # follows the app's color
    assert e.scene(T0 + 1).base == (1, 2, 3)          # (no gamma on the user's color)
    # playing -> disconnected -> idle again, edge gone
    e.update(gd(pct=30), [ev("level_start", difficulty="demon")], T0 + 2)
    assert e.scene(T0 + 2).edge == EDGE_COLORS["demon"]
    e.update(gd(pct=30, connected=False), [], T0 + 3)
    s = e.scene(T0 + 3)
    assert s.base == (1, 2, 3) and s.keys == {} and s.edge is None


# ------------------------------------------------------------ color helpers
def test_led_gamma() -> None:
    # fixed points: black, white and the pure primaries are unchanged
    for c in ((0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255),
              (255, 255, 0), (0, 255, 255)):
        assert led_gamma(c) == c, c
    # mid tones are pulled down per channel: 128 -> 56, 200 -> 149, 30 -> 2
    assert led_gamma((128, 128, 128)) == (56, 56, 56)
    assert led_gamma((255, 200, 0)) == (255, 149, 0)
    assert led_gamma((30, 160, 70)) == (2, 91, 15)
    # monotonic, never brighter than the input, ints in range, equal channels stay equal
    prev = -1
    for v in range(256):
        out = led_gamma((v, v, v))
        assert out[0] == out[1] == out[2] and prev <= out[0] <= v
        assert isinstance(out[0], int)
        prev = out[0]
    assert led_gamma((300, -5, 128)) == (255, 0, 56)  # stray input is clamped first
    # gamma 1 is the identity, a custom gamma is honoured
    assert led_gamma((10, 128, 250), gamma=1.0) == (10, 128, 250)
    assert led_gamma((128, 0, 0), gamma=3.0)[0] < 56
    assert sat_of(led_gamma((180, 180, 180))) == 0    # grey never gains a hue


def test_saturate() -> None:
    # not vivid: black, white, grey, washed-out colors come back unchanged
    for c in (BLACK, WHITE, (100, 100, 100), (200, 150, 150), (0, 3, 32)):
        assert vividness(c) < 0.35 and saturate(c) == c, c
    # vivid but pale: saturation goes up to 0.85, hue and brightness stay
    pale = (255, 90, 120)                                # s ~ 0.65, v = 1
    assert vividness(pale) >= 0.35 and sat_of(pale) < 0.85
    out = saturate(pale)
    assert abs(sat_of(out) - 0.85) < 0.02 and abs(val_of(out) - 1.0) < 0.01
    assert hue_gap(out, pale) < 0.01
    # already rich: untouched
    assert saturate((255, 0, 128)) == (255, 0, 128)
    assert saturate((20, 20, 255)) == (20, 20, 255)
    for c in ((255, 90, 120), (60, 130, 200), (255, 255, 0)):
        o = saturate(c)
        assert len(o) == 3 and all(isinstance(x, int) and 0 <= x <= 255 for x in o)
        assert sat_of(o) >= min(0.85, sat_of(c)) - 0.01
    assert saturate((200, 150, 150), vivid=0.1) != (200, 150, 150)   # parameter


# ---------------------------------------------------------------- level color
def test_color_source_priority() -> None:
    # black background, no accent, vivid player color: the hue of p1, never grey
    base = fx(bg=BLACK, accent=None, p1=VIVID_P1).scene(T0).base
    assert base == lit(VIVID_P1)
    assert sat_of(base) >= 0.85 and hue_gap(base, VIVID_P1) < 0.08
    assert base[1] > 3 * max(base[0], base[2])
    # a white background behaves the same; so do greys and the near-black dark blue
    for bg in (WHITE, (120, 120, 120), (0, 3, 32), (250, 250, 250)):
        assert fx(bg=bg, accent=None, p1=VIVID_P1).scene(T0).base == base, bg
    # a vivid background beats the player color
    assert fx(bg=(0, 60, 200), accent=None, p1=VIVID_P1).scene(T0).base == lit((0, 60, 200))
    # the accent wins over a vivid background and over the player color
    acc = fx(bg=(0, 60, 200), accent=(255, 20, 20), p1=VIVID_P1).scene(T0).base
    assert acc == lit((255, 20, 20)) and hue_gap(acc, (255, 0, 0)) < 0.02
    assert fx(bg=BLACK, accent=(255, 20, 20), p1=VIVID_P1).scene(T0).base == acc
    assert fx(accent=[255, 20, 20]).scene(T0).base == acc      # a JSON list works too
    # pale colors come out rich: saturation pushed to at least 0.85 before the gamma
    assert fx(bg=BLACK, accent=(255, 100, 130)).scene(T0).base == lit((255, 100, 130))
    assert level_color((255, 100, 130)) != (255, 100, 130)
    assert sat_of(fx(bg=BLACK, accent=(255, 100, 130)).scene(T0).base) >= 0.85
    # nothing has a hue (default white player color): a very dim neutral, no wash
    dim = fx(bg=BLACK, accent=None, p1=WHITE).scene(T0).base
    assert dim[0] == dim[1] == dim[2] and 0 <= dim[0] <= 12
    assert fx(bg=(80, 80, 80), accent=None, p1=(90, 90, 90)).scene(T0).base == dim
    # resting brightness is about 45% (about 17% on the LEDs after the gamma)
    assert 0.40 <= REST_BRIGHTNESS <= 0.50
    assert val_of(base) == val_of(led_gamma(scale(VIVID_P1, REST_BRIGHTNESS)))
    # the old behaviour is gone: black background is not a 35% grey
    black_bg = fx(bg=BLACK, accent=None, p1=VIVID_P1).scene(T0).base
    assert max(black_bg) < 60 and sat_of(black_bg) > 0.85
    assert sat_of(fx(bg=WHITE, accent=None, p1=VIVID_P1).scene(T0).base) > 0.85


def test_never_a_grey_wash() -> None:
    """The reported bug: black/white backgrounds used to become a bright grey."""
    for bg in (BLACK, WHITE, (0, 3, 32), (128, 128, 128)):
        e = fx(bg=bg, accent=None, p1=VIVID_P1)
        for _dt, s in play(e, T0 + 1, 4.0, bg=bg, accent=None, p1=VIVID_P1):
            assert sat_of(s.base) >= 0.85, (bg, s.base)
            assert s.base[1] >= s.base[0] and s.base[1] > s.base[2], (bg, s.base)


def test_color_smoothing_and_snap() -> None:
    a, b = (0, 255, 0), (0, 255, 120)         # 28 degrees apart: smoothed, but fast
    e = fx(accent=a)
    assert e.scene(T0).base == lit(a)
    e.scene(T0 + 1)                                  # frames are drawn continuously
    e.update(gd(accent=b), [], T0 + 1)
    assert e.scene(T0 + 1).base == lit(a)            # same instant: nothing moved yet
    mid = e.scene(T0 + 1.012).base
    assert lit(a)[2] < mid[2] < lit(b)[2], (mid, lit(a), lit(b))
    end = e.scene(T0 + 1.3).base                     # settled in a fraction of a second
    assert max(abs(x - y) for x, y in zip(end, lit(b))) <= 1
    # a big hue move (> 30 degrees) is instant: the color trigger hits the lights at once
    e.update(gd(accent=(0, 120, 255)), [], T0 + 2)
    assert e.scene(T0 + 2).base == lit((0, 120, 255))
    e.update(gd(accent=(255, 0, 0)), [], T0 + 2.033)
    assert e.scene(T0 + 2.033).base == lit((255, 0, 0))
    # the source changing (accent -> player color) to another hue snaps as well
    e.update(gd(accent=None, bg=BLACK, p1=VIVID_P1), [], T0 + 2.066)
    assert e.scene(T0 + 2.066).base == lit(VIVID_P1)
    # reconnecting snaps to the level color (no fade from the old one)
    e.update(gd(connected=False), [], T0 + 3)
    e.update(gd(accent=(40, 255, 0)), [], T0 + 4)
    assert e.scene(T0 + 4).base == lit((40, 255, 0))


# ----------------------------------------------------------------- beat tracker
def test_beat_tracker_on_2hz_signal() -> None:
    bt = BeatTracker()
    levels = []
    for i in range(30 * 12):
        t = i / 30
        bt.update((kick(t), 0.0, 0.0), t)
        levels.append((t, bt.level(t)))
    late = [(t, lv) for t, lv in levels if t >= 3.0]
    # peaks (the middle of each kick, every 0.5 s) are near 1 ...
    peaks = [lv for t, lv in late if abs((t + 0.25) % 0.5 - 0.25) < 0.02]
    assert peaks and min(peaks) > 0.9, min(peaks)
    # ... and the valleys (between kicks, past the decay time) are near 0
    valleys = [lv for t, lv in late if 0.30 <= t % 0.5 <= 0.37]
    assert valleys and max(valleys) < 0.1, max(valleys)
    assert all(0.0 <= lv <= 1.0 for _t, lv in levels)
    # deterministic: no wall clock anywhere
    bt2 = BeatTracker()
    again = []
    for i in range(30 * 12):
        t = i / 30
        bt2.update((kick(t), 0.0, 0.0), t)
        again.append((t, bt2.level(t)))
    assert again == levels


def test_beat_tracker_flat_and_scaling() -> None:
    # always clamped at 1 (old mods), always 0, any constant: no beat, ever
    for value in (1.0, 0.0, 7.3, 16.0):
        bt = BeatTracker()
        for i in range(30 * 10):
            t = i / 30
            assert bt.update((value, value, value), t) == 0.0
            assert bt.level(t + 0.05) == 0.0
    # tiny noise is still flat
    bt = BeatTracker()
    top = 0.0
    for i in range(30 * 10):
        top = max(top, bt.update((8.0 + 0.01 * (i % 3), 0.0, 0.0), i / 30))
    assert top == 0.0
    # auto-scale: the same rhythm at any amplitude still peaks near 1
    for amp in (0.05, 1.0, 14.0):
        bt = BeatTracker()
        peaks = []
        for i in range(30 * 10):
            t = i / 30
            lv = bt.update((amp * max(0.0, math.cos(2 * math.pi * 2 * t)) ** 2, 0, 0), t)
            if t > 3 and abs((t + 0.25) % 0.5 - 0.25) < 0.02:
                peaks.append(lv)
        assert min(peaks) > 0.9, (amp, min(peaks))
    # the scale follows the music: a loud passage, then a quiet one (same rhythm)
    bt = BeatTracker()
    quiet_peaks = []
    for i in range(30 * 16):
        t = i / 30
        amp = 14.0 if t < 6 else 2.0
        lv = bt.update((amp * max(0.0, math.cos(2 * math.pi * 2 * t)) ** 2, 0, 0), t)
        if t > 13 and abs((t + 0.25) % 0.5 - 0.25) < 0.02:
            quiet_peaks.append(lv)
    assert min(quiet_peaks) > 0.9
    # old-format data (0-1, clamped at 1, dipping on beats) stays in range and moves
    bt = BeatTracker()
    seen = [bt.update((min(1.0, 1.4 + 0.6 * math.cos(2 * math.pi * 2 * i / 30)), 0, 0), i / 30)
            for i in range(30 * 8)]
    assert all(0.0 <= v <= 1.0 for v in seen) and max(seen) > 0.5


def test_beat_tracker_attack_decay_and_channels() -> None:
    bt = BeatTracker()
    for i in range(30 * 4):                          # a window: rest at 1, hits at 10
        bt.update((10.0 if i % 15 == 0 else 1.0, 0, 0), i / 30)
    t = 4.0
    assert bt.update((10.0, 0, 0), t) == 1.0         # instant attack
    assert 0.3 < bt.level(t + 0.09) < 0.7            # fast decay ...
    assert bt.level(t + 0.2) == 0.0                  # ... gone in about 0.2 s
    assert bt.level(t - 1.0) == 1.0                  # asking about the past is safe
    # the most dynamic of the three meters is followed, whichever it is
    for dynamic in range(3):
        bt = BeatTracker()
        peaks = []
        for i in range(30 * 10):
            t = i / 30
            vals = [3.0, 3.0, 3.0]
            vals[(dynamic + 1) % 3] = 0.5 + 0.1 * math.sin(i)        # almost flat
            vals[dynamic] = kick(t)
            lv = bt.update(vals, t)
            if t > 3 and abs((t + 0.25) % 0.5 - 0.25) < 0.02:
                peaks.append(lv)
        assert bt.channel == dynamic and min(peaks) > 0.9, (dynamic, min(peaks))
    # a clock that jumps back resets instead of raising
    bt.update((5, 0, 0), 1.0)
    assert 0.0 <= bt.level(1.0) <= 1.0 and bt.channel is None


def test_beat_drives_base() -> None:
    e = fx(bg=BLACK, accent=None, p1=VIVID_P1)
    rest_color, peak_color = lit(VIVID_P1, 0.0), lit(VIVID_P1, 1.0)
    assert peak_color == led_gamma(VIVID_P1) == (53, 255, 0)
    frames = [(dt, s.base) for dt, s in play(e, T0 + 1, 6.0, bg=BLACK, p1=VIVID_P1)]
    late = [(dt, b) for dt, b in frames if dt >= 3.0]
    bright = [val_of(b) for _dt, b in late]
    assert max(bright) == 1.0 and min(bright) == val_of(rest_color)  # 100% peak, rest valleys
    assert max(b[1] for _dt, b in late) == 255
    assert any(b == rest_color for _dt, b in late)
    assert len({b for _dt, b in late}) > 3                   # it fades through steps, not on/off
    assert all(hue_gap(b, VIVID_P1) < 0.08 for _dt, b in late)   # same hue throughout
    # no beat information (always clamped at 1, or 0): the base sits at rest, not bright
    for flat in (1.0, 0.0):
        e = fx(bg=BLACK, accent=None, p1=VIVID_P1)
        assert {s.base for _dt, s in play(e, T0 + 1, 4.0, signal=lambda t, f=flat: f,
                                          bg=BLACK, p1=VIVID_P1)} \
            == {rest_color}
    # the progress bar and the accent keys do not pulse; unlit keys show the base
    e = fx(bg=BLACK, accent=None, p1=VIVID_P1, pct=50)
    scenes = [s for _dt, s in play(e, T0 + 1, 3.0, pct=50, bg=BLACK, p1=VIVID_P1)]
    assert len({s.keys["Q"] for s in scenes}) == 1 and len({s.keys["W"] for s in scenes}) == 1
    assert len({s.base for s in scenes}) > 5
    # pulse2 / pulse3 are used when they are the dynamic ones
    e = fx(bg=BLACK, accent=None, p1=VIVID_P1)
    greens = []
    for i in range(30 * 6):
        t = T0 + 1 + i / 30
        e.update(gd(bg=BLACK, p1=VIVID_P1, pulse=2.0, pulse2=0.0, pulse3=kick(i / 30)), [], t)
        greens.append(e.scene(t).base[1])
    assert max(greens[90:]) == 255 and min(greens[90:]) < 60


def test_progress_bar() -> None:
    s = fx(pct=50).scene(T0)
    assert lit_columns(s, BAR) == set(range(0, 8))              # col <= 7.5
    assert all(k not in s.keys or k in ACCENT_KEYS or s.keys[k] == BAR
               for k in KEY_COLUMN)                              # others show base
    assert s.base == REST_BG
    assert lit_columns(fx(pct=100).scene(T0), BAR) == ALL_COLS
    assert lit_columns(fx(pct=0).scene(T0), BAR) == set()        # nothing at the start
    assert lit_columns(fx(pct=7).scene(T0), BAR) == {0, 1}
    # gravity flip: fills from the right
    s = fx(pct=50, flip=True).scene(T0)
    assert lit_columns(s, BAR) == set(range(8, 16)) - {14}
    assert lit_columns(fx(pct=100, flip=True).scene(T0), BAR) == ALL_COLS
    # platformer levels have no percent
    e = GDEffects(idle_color=IDLE)
    e.update(gd(pct=50, platformer=True), [], T0)
    assert lit_columns(e.scene(T0), BAR) == set()
    # the bar stands out from the dark base on every black-background level
    s = fx(pct=50, bg=BLACK, accent=None, p1=VIVID_P1).scene(T0)
    assert val_of(s.keys["Q"]) >= 0.65 and val_of(s.base) < 0.2
    # a pale player color is enriched, a grey one stays grey
    pale = fx(pct=50, p1=(255, 90, 120)).scene(T0).keys["Q"]
    assert sat_of(pale) > 0.8
    assert sat_of(fx(pct=50, p1=(120, 120, 120)).scene(T0).keys["Q"]) == 0


def test_best_marker() -> None:
    s = fx(pct=20, best=60).scene(T0)                # column round(0.6 * 15) = 9
    marked = {k for k, v in s.keys.items() if v == WHITE and k not in ACCENT_KEYS}
    assert marked and {KEY_COLUMN[k] for k in marked} == {9}
    assert marked == {k for k, c in KEY_COLUMN.items() if c == 9 and k not in ACCENT_KEYS}
    assert lit_columns(s, BAR) == {0, 1, 2, 3}
    # no marker once the player is past the best (or equal)
    for pct, best in ((60, 60), (70, 60), (30, 0)):
        s = fx(pct=pct, best=best).scene(T0)
        assert WHITE not in non_accent(s).values(), (pct, best)
    # flip mirrors it
    s = fx(pct=20, best=60, flip=True).scene(T0)
    assert {KEY_COLUMN[k] for k, v in s.keys.items()
            if v == WHITE and k not in ACCENT_KEYS} == {15 - 9}


def test_mode_accents() -> None:
    assert MODE_COLORS == {
        "cube": (255, 200, 0), "ship": (255, 60, 160), "ball": (255, 110, 0),
        "ufo": (170, 60, 255), "wave": (0, 220, 255), "robot": (255, 40, 40),
        "spider": (60, 255, 90), "swing": (40, 110, 255)}
    assert set(ACCENT_KEYS) == {"W", "A", "S", "D", "UP", "DOWN", "LEFT", "RIGHT", "SPACE"}
    for mode, color in MODE_COLORS.items():
        s = fx(mode=mode, pct=100).scene(T0)             # accent wins over the bar
        for k in ACCENT_KEYS:
            assert s.keys[k] == led_gamma(color), (mode, k)   # gamma on the way out
        assert s.keys["Q"] == BAR                        # other keys unaffected
    assert fx(mode="nonsense").scene(T0).keys["W"] == led_gamma(MODE_COLORS["cube"])
    # hues survive the gamma: each accent is still clearly its own color
    for mode, color in MODE_COLORS.items():
        out = led_gamma(color)
        assert hue_gap(out, color) < 0.06 and sat_of(out) >= sat_of(color) - 0.01, mode


def test_practice() -> None:
    e = fx(practice=True, pct=60, best=80)
    s = e.scene(T0)
    assert PRACTICE_GREEN == (30, 160, 70)
    assert s.base == led_gamma(PRACTICE_GREEN) == (2, 91, 15)
    assert non_accent(s) == {}                           # progress bar hidden
    assert s.keys["W"] == led_gamma(MODE_COLORS["cube"])
    # checkpoint: everything bright green for 0.25 s
    cp = led_gamma(CHECKPOINT_GREEN)
    assert cp[1] == 255 and cp[0] == 0
    e.update(gd(practice=True, pct=60), [ev("checkpoint")], T0 + 1)
    s = e.scene(T0 + 1.05)
    assert s.base == cp
    assert all(v == cp for v in s.keys.values()) and set(s.keys) == set(KEY_COLUMN)
    s = e.scene(T0 + 1.2)                                # fading out
    assert s.base[1] > led_gamma(PRACTICE_GREEN)[1] and s.base != cp
    s = e.scene(T0 + 1.3)
    assert s.base == led_gamma(PRACTICE_GREEN) and non_accent(s) == {}
    # the practice green lifts with the beat but stays green
    e = fx(practice=True)
    tops = [s.base for _dt, s in play(e, T0 + 1, 5.0, practice=True)]
    assert max(b[1] for b in tops) == 255 and min(b[1] for b in tops) == 91
    assert all(b[1] > 3 * b[0] and b[1] > 3 * b[2] for b in tops)


def test_paused() -> None:
    e = fx(pct=50, bg=(100, 100, 100), accent=None, p1=VIVID_P1)
    list(play(e, T0 + 1, 3.0, pct=50, bg=(100, 100, 100), p1=VIVID_P1))   # beats running
    rest = fx(pct=50, bg=(100, 100, 100), p1=VIVID_P1).scene(T0)
    e.update(gd(pct=50, paused=True, pulse=14.0, bg=(100, 100, 100), p1=VIVID_P1),
             [], T0 + 4.0)
    s = e.scene(T0 + 4.0)
    near = lambda a, b: max(abs(x - y) for x, y in zip(a, b)) <= 2    # noqa: E731
    assert near(s.base, scale(rest.base, 0.25)), (s.base, rest.base)  # no beat while paused
    assert near(s.keys["Q"], scale(led_gamma(saturate(boost_player(VIVID_P1))), 0.25))
    assert near(s.keys["W"], scale(led_gamma(MODE_COLORS["cube"]), 0.25))
    assert max(s.base) <= max(rest.base) / 3 + 1
    assert s.edge == rest.edge                            # edge untouched


def redness(c) -> int:
    return c[0] - max(c[1], c[2])


def burst_front(scene) -> float:
    """Distance from the keyboard center of the outermost clearly red key."""
    from rgbsync.gd.effects import _KEY_RADIUS
    return max((_KEY_RADIUS[k] for k, c in scene.keys.items() if redness(c) > 150),
               default=-1.0)


def test_death_burst() -> None:
    # (no progress bar here: the magenta player color is red enough to confuse the ring)
    e = fx(pct=0, bg=(20, 40, 120))
    e.update(gd(pct=0), [ev("death", pct=40.0, new_best=False)], T0 + 5)
    quiet = fx(pct=0).scene(T0 + 5)
    s1 = e.scene(T0 + 5.1)
    assert redness(s1.base) > 150 and s1.edge == EDGE_COLORS["hard"]
    assert any(redness(c) > 150 for c in s1.keys.values())
    # the ring grows outward from the center
    fronts = [burst_front(e.scene(T0 + 5 + dt)) for dt in (0.1, 0.2, 0.3, 0.4, 0.5)]
    assert fronts[0] >= 0 and all(a < b for a, b in zip(fronts, fronts[1:])), fronts
    s = e.scene(T0 + 5.1)
    inner = [k for k in KEY_COLUMN if KEY_COLUMN[k] in (7, 8)]
    outer = [k for k in KEY_COLUMN if KEY_COLUMN[k] == 15 and k not in ACCENT_KEYS]
    assert all(redness(s.keys[k]) > 100 for k in inner)
    assert all(redness(s.keys[k]) < 50 for k in outer)
    # base fades out; fully gone after 0.6 s
    reds = [redness(e.scene(T0 + 5 + dt).base) for dt in (0.0, 0.15, 0.3, 0.45)]
    assert all(a > b for a, b in zip(reds, reds[1:])) and reds[0] > 200
    after = e.scene(T0 + 5.7)
    assert after.base == quiet.base and after.keys == quiet.keys
    # restart during the burst does not cancel it
    e.update(gd(pct=0), [ev("restart")], T0 + 5.2)
    assert redness(e.scene(T0 + 5.3).base) > 50
    # new best: gold instead of red (gold goes through the gamma like everything else)
    e.update(gd(pct=0), [ev("death", pct=51.0, new_best=True)], T0 + 9)
    s = e.scene(T0 + 9.1)
    gold = led_gamma(GOLD)
    assert s.base[0] > 150 and s.base[1] > 60 and s.base[2] < 40 and s.base[1] < s.base[0]
    gold_keys = [c for c in s.keys.values() if c[0] > 150 and c[1] > 60 and c[2] < 40]
    assert gold_keys and not any(redness(c) > 150 and c[1] < 40 for c in s.keys.values())
    assert gold == (255, 133, 0)
    after = e.scene(T0 + 9.7)
    assert after.base == REST_BG and not any(c[0] > 150 and c[1] > 60 and c[2] < 40
                                              for k, c in after.keys.items()
                                              if k not in ACCENT_KEYS)


def test_complete() -> None:
    e = fx(pct=100)
    e.update(gd(pct=100), [ev("complete", new_best=False)], T0 + 10)
    seen_hues = set()
    base_hues = set()
    for dt in (0.1, 0.5, 1.0, 1.5, 2.0, 2.5):
        s = e.scene(T0 + 10 + dt)
        hues = {round(hue_of(c), 1) for c in s.keys.values()}
        assert len(hues) >= 6, (dt, hues)                 # a spread of colors on the keys
        seen_hues.add(frozenset(s.keys.values()))
        base_hues.add(round(hue_of(s.base), 1))
    assert len(seen_hues) == 6 and len(base_hues) >= 3    # sweeps over time, base cycles
    s = e.scene(T0 + 13.2)                                # after 3 s: back to normal
    assert s.base == REST_BG and s.keys["Q"] == BAR
    # new best: gold for 0.5 s first, then the rainbow
    gold = led_gamma(GOLD)
    e = fx(pct=100)
    e.update(gd(pct=100), [ev("complete", new_best=True)], T0 + 20)
    s = e.scene(T0 + 20.3)
    assert s.base == gold and set(s.keys.values()) == {gold} and set(s.keys) == set(KEY_COLUMN)
    s = e.scene(T0 + 20.9)
    assert s.base != gold and len(set(s.keys.values())) > 8
    assert e.scene(T0 + 23.4).base != REST_BG             # 3.5 s total, still going
    assert e.scene(T0 + 23.6).base == REST_BG
    # the game stops sending state after completion: the celebration still finishes
    e = fx(pct=100)
    e.update(gd(pct=100), [ev("complete", new_best=False)], T0 + 30)
    e.update(gd(pct=100, connected=False), [], T0 + 33.0)
    s = e.scene(T0 + 31.0)
    assert len(set(s.keys.values())) > 8 and s.edge is None
    e.update(gd(connected=False), [], T0 + 34)
    assert e.scene(T0 + 34).base == IDLE and e.scene(T0 + 34).keys == {}


def test_deliberate_colors_after_gamma() -> None:
    """Primaries are fixed points of the gamma; the others stay recognizably themselves."""
    assert led_gamma((255, 0, 0)) == (255, 0, 0)          # death red, the crash color
    assert led_gamma(CHECKPOINT_GREEN) == (0, 255, 11)    # bright green, a hint of blue
    assert led_gamma(GOLD) == (255, 133, 0)               # orange-gold
    assert led_gamma(PRACTICE_GREEN) == (2, 91, 15)       # deep green
    for color in (GOLD, PRACTICE_GREEN, CHECKPOINT_GREEN, *MODE_COLORS.values()):
        out = led_gamma(color)
        assert hue_gap(out, color) < 0.07, (color, out)
        assert sat_of(out) >= sat_of(color) - 0.01
    # the rainbow keeps its full range of hues through the gamma
    e = fx(pct=100)
    e.update(gd(pct=100), [ev("complete", new_best=False)], T0)
    hues = {round(hue_of(c), 1) for dt in (0.2, 1.0, 2.0) for c in e.scene(T0 + dt).keys.values()
            if sat_of(c) > 0.9}
    assert len(hues) >= 8


def test_edge_per_difficulty_and_constant() -> None:
    expected = {"auto": WHITE, "unrated": WHITE, "easy": (0, 255, 255),
                "normal": (0, 255, 0), "hard": (255, 255, 0), "harder": (255, 120, 0),
                "insane": (255, 0, 255), "demon": (255, 0, 0)}
    assert EDGE_COLORS == expected
    for diff, color in expected.items():
        e = GDEffects(idle_color=IDLE)
        e.update(gd(difficulty=diff), [ev("level_start", difficulty=diff)], T0)
        assert e.scene(T0).edge == color, diff            # edge picks a preset: no gamma
    # constant for the whole level, whatever happens
    e = GDEffects(idle_color=IDLE)
    e.update(gd(difficulty="insane"), [ev("level_start", difficulty="insane")], T0)
    seen = set()
    t = T0
    for i in range(300):
        t += 0.1
        events = []
        if i == 50:
            events = [ev("death", pct=10.0, new_best=False)]
        elif i == 80:
            events = [ev("restart"), ev("checkpoint")]
        elif i == 200:
            events = [ev("complete", new_best=True)]
        e.update(gd(pct=i % 100, mode="ship" if i % 2 else "ufo", flip=bool(i % 3 == 0),
                    practice=i > 150, paused=i % 17 == 0, pulse=(i % 10) * 1.4,
                    bg=(i % 255, 50, 200 - i % 200)), events, t)
        seen.add(e.scene(t).edge)
    assert seen == {EDGE_COLORS["insane"]}, seen
    # a new level changes it once; leaving the level clears it
    e.update(gd(difficulty="easy"), [ev("level_start", difficulty="easy")], t + 1)
    assert e.scene(t + 1).edge == EDGE_COLORS["easy"]
    e.update(gd(connected=False), [ev("level_exit")], t + 2)
    assert e.scene(t + 2).edge is None
    # joining mid-level (no level_start seen): taken from the state
    e = GDEffects(idle_color=IDLE)
    e.update(gd(difficulty="demon"), [], T0)
    assert e.scene(T0).edge == EDGE_COLORS["demon"]
    # an event that arrives before the first state is not lost
    e = GDEffects(idle_color=IDLE)
    e.update(GDState(), [ev("level_start", difficulty="normal")], T0)
    assert e.scene(T0).edge is None
    e.update(gd(difficulty="normal"), [], T0 + 0.05)
    assert e.scene(T0 + 0.05).edge == EDGE_COLORS["normal"]


def test_never_raises() -> None:
    e = GDEffects()
    assert e.scene(0.0).edge is None
    e.update(GDState(), [], 0.0)
    assert e.scene(0.0).edge is None
    e.update(GDState(connected=True), [], 0.0)           # empty but connected
    s = e.scene(0.0)
    assert s is not None and s.edge is not None
    e.update(gd(), [ev("death"), ev("complete"), ev("bogus"), ev("level_start")], 1.0)
    e.scene(1.0)
    e.scene(-5.0)                                        # time going backwards
    e._state = object()                                  # garbage: falls back to idle
    logging.disable(logging.CRITICAL)                    # the crash is logged once
    try:
        s = e.scene(2.0)
    finally:
        logging.disable(logging.NOTSET)
    assert s.base == e.idle_color
    e = GDEffects()
    e.update(gd(bg=(300, -4, 9), p1=(0, 0, 0), pct=1e9, best=-3, pulse=99, pulse2=-5,
                pulse3=1e9, accent=(999, -1, 5)), [], 0.0)
    for c in (e.scene(0.0).base, *e.scene(0.0).keys.values()):
        assert len(c) == 3 and all(isinstance(x, int) and 0 <= x <= 255 for x in c)
    # time going backwards through the beat tracker, and wild pulse values
    for i, t in enumerate((5.0, 4.0, 0.0, 100.0, 100.0, 99.0)):
        e.update(gd(pulse=float(i * 3), pulse2=99.0, pulse3=-1.0), [], t)
        s = e.scene(t)
        assert all(0 <= x <= 255 for x in s.base)


def test_scene_is_fast_and_deterministic() -> None:
    e = fx(pct=63, best=80, pulse=0.7, flip=True, mode="ship", accent=(255, 40, 40))
    e.update(gd(pct=63, best=80, pulse=0.7, flip=True, mode="ship", accent=(255, 40, 40)),
             [ev("death", pct=63.0, new_best=False)], T0 + 1)
    start = time.perf_counter()
    for i in range(1000):
        e.scene(T0 + 1.1 + (i % 5) * 0.001)               # worst case: burst running
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"1000 scene() calls took {elapsed * 1000:.0f} ms"
    plain = fx(pct=63, best=80, pulse=0.7)
    for _dt, _s in play(plain, T0 + 1, 3.0, pct=63, best=80):   # beats running
        pass
    start = time.perf_counter()
    for i in range(1000):
        plain.scene(T0 + 5 + i * 0.001)
    assert time.perf_counter() - start < 0.1
    # update() (with the beat tracker) keeps up with a 60 Hz engine as well
    start = time.perf_counter()
    for i in range(1000):
        plain.update(gd(pct=63, best=80, pulse=kick(i / 60), pulse2=1.0, pulse3=2.0),
                     [], T0 + 10 + i / 60)
        plain.scene(T0 + 10 + i / 60)
    assert time.perf_counter() - start < 0.5

    # deterministic: two identical runs agree
    def run():
        x = GDEffects(idle_color=IDLE)
        x.update(gd(pct=10), [ev("level_start", difficulty="harder")], T0)
        out = []
        for i in range(40):
            x.update(gd(pct=10 + i, bg=(i * 5, 30, 100), pulse=kick(i * 0.033),
                        accent=(255, i * 6, 0) if i % 7 else None),
                     [ev("death", pct=1.0, new_best=False)] if i == 10 else [], T0 + i * 0.033)
            s = x.scene(T0 + i * 0.033)
            out.append((s.base, tuple(sorted(s.keys.items())), s.edge))
        return out
    assert run() == run()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("gd effects tests passed")
