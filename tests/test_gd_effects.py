"""Geometry Dash effects tests: deterministic timelines driven by explicit ``now``.

No game, no network, no hardware. Run: python tests/test_gd_effects.py
"""
import colorsys
import logging
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.gd.effects import (  # noqa: E402
    ACCENT_KEYS, EDGE_COLORS, GOLD, KEY_COLUMN, MAX_COLUMN, MODE_COLORS,
    PRACTICE_GREEN, GDEffects, boost_bg, boost_player, scale)
from rgbsync.gd.listener import GDEvent, GDState  # noqa: E402
from rgbsync.scene import KEY_NAMES  # noqa: E402

IDLE = (12, 34, 56)
BG = (20, 40, 120)
P1 = (255, 0, 128)
WHITE = (255, 255, 255)
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


def lit_columns(scene, color) -> set[int]:
    return {KEY_COLUMN[k] for k, v in scene.keys.items()
            if v == color and k not in ACCENT_KEYS}


def hue_of(c) -> float:
    return colorsys.rgb_to_hsv(*(x / 255 for x in c))[0]


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
    assert e.scene(T0 + 1).base == (1, 2, 3)
    # playing -> disconnected -> idle again, edge gone
    e.update(gd(pct=30), [ev("level_start", difficulty="demon")], T0 + 2)
    assert e.scene(T0 + 2).edge == EDGE_COLORS["demon"]
    e.update(gd(pct=30, connected=False), [], T0 + 3)
    s = e.scene(T0 + 3)
    assert s.base == (1, 2, 3) and s.keys == {} and s.edge is None


def test_color_sync_and_boost() -> None:
    e = fx()
    assert e.scene(T0).base == boost_bg(BG)
    # dim colors become visible, hue kept; bright ones keep headroom for the pulse
    dim = boost_bg((5, 8, 25))
    assert 0.34 < max(dim) / 255 < 0.55 and abs(hue_of(dim) - hue_of((5, 8, 25))) < 0.02
    assert max(boost_bg((0, 0, 0))) >= 85 and len(set(boost_bg((0, 0, 0)))) == 1
    assert max(boost_bg((255, 255, 255))) <= 210
    assert max(boost_player((10, 0, 5))) >= 160
    # smoothed over ~0.15 s: not instant, but converged quickly
    e.scene(T0 + 1)                                  # frames are drawn continuously
    e.update(gd(bg=(200, 0, 0)), [], T0 + 1)
    assert e.scene(T0 + 1).base == boost_bg(BG)      # same instant: nothing moved yet
    mid = e.scene(T0 + 1.03).base
    assert boost_bg(BG)[0] < mid[0] < boost_bg((200, 0, 0))[0]
    end = e.scene(T0 + 1.5).base
    assert max(abs(a - b) for a, b in zip(end, boost_bg((200, 0, 0)))) <= 2
    # reconnecting snaps to the level color (no fade from the old one)
    e.update(gd(connected=False), [], T0 + 2)
    e.update(gd(bg=(0, 90, 0)), [], T0 + 3)
    assert e.scene(T0 + 3).base == boost_bg((0, 90, 0))


def test_beat_pulse() -> None:
    flat = fx(bg=(100, 100, 100)).scene(T0)
    pulsed = fx(bg=(100, 100, 100), pulse=1.0).scene(T0)
    half = fx(bg=(100, 100, 100), pulse=0.5).scene(T0)
    assert flat.base == boost_bg((100, 100, 100))
    ratio = pulsed.base[0] / flat.base[0]
    assert 1.38 < ratio < 1.42, ratio                 # +40%
    assert flat.base[0] < half.base[0] < pulsed.base[0]
    assert pulsed.keys["W"][0] >= flat.keys["W"][0]   # keys pulse too (clamped at 255)
    for c in pulsed.base:
        assert 0 <= c <= 255


def test_progress_bar() -> None:
    boosted = boost_player(P1)
    s = fx(pct=50).scene(T0)
    assert lit_columns(s, boosted) == set(range(0, 8))          # col <= 7.5
    assert all(k not in s.keys or k in ACCENT_KEYS or s.keys[k] == boosted
               for k in KEY_COLUMN)                              # others show base
    assert s.base == boost_bg(BG)
    assert lit_columns(fx(pct=100).scene(T0), boosted) == ALL_COLS
    assert lit_columns(fx(pct=0).scene(T0), boosted) == set()    # nothing at the start
    assert lit_columns(fx(pct=7).scene(T0), boosted) == {0, 1}
    # gravity flip: fills from the right
    s = fx(pct=50, flip=True).scene(T0)
    assert lit_columns(s, boosted) == set(range(8, 16)) - {14}
    assert lit_columns(fx(pct=100, flip=True).scene(T0), boosted) == ALL_COLS
    # platformer levels have no percent
    e = GDEffects(idle_color=IDLE)
    e.update(gd(pct=50, platformer=True), [], T0)
    assert lit_columns(e.scene(T0), boosted) == set()


def test_best_marker() -> None:
    s = fx(pct=20, best=60).scene(T0)                # column round(0.6 * 15) = 9
    marked = {k for k, v in s.keys.items() if v == WHITE and k not in ACCENT_KEYS}
    assert marked and {KEY_COLUMN[k] for k in marked} == {9}
    assert marked == {k for k, c in KEY_COLUMN.items() if c == 9 and k not in ACCENT_KEYS}
    assert lit_columns(s, boost_player(P1)) == {0, 1, 2, 3}
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
            assert s.keys[k] == color, (mode, k)
        assert s.keys["Q"] == boost_player(P1)           # other keys unaffected
    assert fx(mode="nonsense").scene(T0).keys["W"] == MODE_COLORS["cube"]


def test_practice() -> None:
    e = fx(practice=True, pct=60, best=80)
    s = e.scene(T0)
    assert s.base == PRACTICE_GREEN == (30, 160, 70)
    assert non_accent(s) == {}                           # progress bar hidden
    assert s.keys["W"] == MODE_COLORS["cube"]
    # checkpoint: everything bright green for 0.25 s
    e.update(gd(practice=True, pct=60), [ev("checkpoint")], T0 + 1)
    s = e.scene(T0 + 1.05)
    assert s.base == (0, 255, 60)
    assert all(v == (0, 255, 60) for v in s.keys.values()) and set(s.keys) == set(KEY_COLUMN)
    s = e.scene(T0 + 1.2)                                # fading out
    assert 0 < s.base[0] + 1 and s.base[1] > PRACTICE_GREEN[1] - 1 and s.base != (0, 255, 60)
    s = e.scene(T0 + 1.3)
    assert s.base == PRACTICE_GREEN and non_accent(s) == {}


def test_paused() -> None:
    normal = fx(pct=50, pulse=1.0, bg=(100, 100, 100)).scene(T0)
    s = fx(pct=50, pulse=1.0, paused=True, bg=(100, 100, 100)).scene(T0)
    flat = fx(pct=50, bg=(100, 100, 100)).scene(T0)
    assert s.base == scale(flat.base, 0.25)               # pulse ignored while paused
    assert s.keys["Q"] == scale(boost_player(P1), 0.25)
    assert s.keys["W"] == scale(MODE_COLORS["cube"], 0.25)
    assert max(s.base) < max(normal.base) / 3
    assert s.edge == normal.edge                          # edge untouched


def redness(c) -> int:
    return c[0] - max(c[1], c[2])


def burst_front(scene) -> float:
    """Distance from the keyboard center of the outermost clearly red key."""
    from rgbsync.gd.effects import _KEY_RADIUS
    return max((_KEY_RADIUS[k] for k, c in scene.keys.items() if redness(c) > 150),
               default=-1.0)


def test_death_burst() -> None:
    e = fx(pct=40, bg=(20, 40, 120))
    e.update(gd(pct=40), [ev("death", pct=40.0, new_best=False)], T0 + 5)
    quiet = fx(pct=40).scene(T0 + 5)
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
    # new best: gold instead of red
    e.update(gd(pct=51), [ev("death", pct=51.0, new_best=True)], T0 + 9)
    s = e.scene(T0 + 9.1)
    assert s.base[0] > 200 and s.base[1] > 120 and s.base[2] < 60
    gold_keys = [c for c in s.keys.values() if c[0] > 200 and c[1] > 120 and c[2] < 60]
    assert gold_keys and not any(redness(c) > 150 and c[1] < 60 for c in s.keys.values())
    after = e.scene(T0 + 9.7)
    assert after.base == boost_bg(BG) and not any(c[0] > 200 and c[1] > 120 and c[2] < 60
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
    assert s.base == boost_bg(BG) and s.keys["Q"] == boost_player(P1)
    # new best: gold for 0.5 s first, then the rainbow
    e = fx(pct=100)
    e.update(gd(pct=100), [ev("complete", new_best=True)], T0 + 20)
    s = e.scene(T0 + 20.3)
    assert s.base == GOLD and set(s.keys.values()) == {GOLD} and set(s.keys) == set(KEY_COLUMN)
    s = e.scene(T0 + 20.9)
    assert s.base != GOLD and len(set(s.keys.values())) > 8
    assert e.scene(T0 + 23.4).base != boost_bg(BG)        # 3.5 s total, still going
    assert e.scene(T0 + 23.6).base == boost_bg(BG)
    # the game stops sending state after completion: the celebration still finishes
    e = fx(pct=100)
    e.update(gd(pct=100), [ev("complete", new_best=False)], T0 + 30)
    e.update(gd(pct=100, connected=False), [], T0 + 33.0)
    s = e.scene(T0 + 31.0)
    assert len(set(s.keys.values())) > 8 and s.edge is None
    e.update(gd(connected=False), [], T0 + 34)
    assert e.scene(T0 + 34).base == IDLE and e.scene(T0 + 34).keys == {}


def test_edge_per_difficulty_and_constant() -> None:
    expected = {"auto": WHITE, "unrated": WHITE, "easy": (0, 255, 255),
                "normal": (0, 255, 0), "hard": (255, 255, 0), "harder": (255, 120, 0),
                "insane": (255, 0, 255), "demon": (255, 0, 0)}
    assert EDGE_COLORS == expected
    for diff, color in expected.items():
        e = GDEffects(idle_color=IDLE)
        e.update(gd(difficulty=diff), [ev("level_start", difficulty=diff)], T0)
        assert e.scene(T0).edge == color, diff
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
                    practice=i > 150, paused=i % 17 == 0, pulse=(i % 10) / 10,
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
    e.update(gd(bg=(300, -4, 9), p1=(0, 0, 0), pct=1e9, best=-3, pulse=99), [], 0.0)
    for c in (e.scene(0.0).base, *e.scene(0.0).keys.values()):
        assert len(c) == 3 and all(isinstance(x, int) and 0 <= x <= 255 for x in c)


def test_scene_is_fast_and_deterministic() -> None:
    e = fx(pct=63, best=80, pulse=0.7, flip=True, mode="ship")
    e.update(gd(pct=63, best=80, pulse=0.7, flip=True, mode="ship"),
             [ev("death", pct=63.0, new_best=False)], T0 + 1)
    start = time.perf_counter()
    for i in range(1000):
        e.scene(T0 + 1.1 + (i % 5) * 0.001)               # worst case: burst running
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"1000 scene() calls took {elapsed * 1000:.0f} ms"
    plain = fx(pct=63, best=80, pulse=0.7)
    start = time.perf_counter()
    for i in range(1000):
        plain.scene(T0 + i * 0.001)
    assert time.perf_counter() - start < 0.1
    # deterministic: two identical runs agree
    def run():
        x = GDEffects(idle_color=IDLE)
        x.update(gd(pct=10), [ev("level_start", difficulty="harder")], T0)
        out = []
        for i in range(40):
            x.update(gd(pct=10 + i, bg=(i * 5, 30, 100)),
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
