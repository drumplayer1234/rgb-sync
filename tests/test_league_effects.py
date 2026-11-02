"""League effects tests: deterministic timelines driven by explicit ``now`` values.

No network, no League, no hardware. Run: python tests/test_league_effects.py
"""
import colorsys
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.league.client import LeagueEvent, LeagueState  # noqa: E402
from rgbsync.league.effects import (  # noqa: E402
    DEAD_GREY, DRAGON_COLORS, GOLD, RED, LeagueEffects, mix, pulse_hz, scale)
from rgbsync.scene import KEY_NAMES  # noqa: E402

PRIMARY = (255, 79, 163)
SECONDARY = (122, 60, 255)
BASE70 = scale(PRIMARY, 0.7)
WHITE = (255, 255, 255)


class FakeColors:
    """ChampionColors stand-in: instant peek, records prefetch calls."""

    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.palettes = {"Ahri": (PRIMARY, SECONDARY), "Zed": ((255, 20, 20), (60, 60, 255))}
        self.skin_palettes: dict = {}
        self.keys = {103: "Ahri", 238: "Zed"}
        self.prefetched: list = []

    def _id(self, ref):
        return self.keys.get(ref) if isinstance(ref, int) else ref

    def peek(self, champion, skin_num=0):
        if not self.ready:
            return None
        champ = self._id(champion)
        if (champ, skin_num) in self.skin_palettes:
            return self.skin_palettes[(champ, skin_num)]
        return self.palettes.get(champ) if not skin_num or (champ, skin_num) in self.skin_palettes \
            or skin_num == 0 else None

    def prefetch(self, champion, skin_num=0) -> None:
        self.prefetched.append((champion, skin_num))


def game(hp=100.0, dead=False, resp=None, champ="Ahri") -> LeagueState:
    return LeagueState(phase="in_game", champion=champ, skin_num=0, health_pct=hp, is_dead=dead,
                       respawn_in=resp, my_team="ORDER", game_time=100.0)


def ev(kind, mine=False, my_team=False, **detail) -> LeagueEvent:
    return LeagueEvent(kind, mine, my_team, detail)


def new(state=None, now=0.0, colors=None) -> LeagueEffects:
    fx = LeagueEffects(colors or FakeColors())
    fx.update(state or game(), [], now)
    return fx


def crossings(fx: LeagueEffects, t0: float, seconds: float) -> int:
    """Count upward crossings of the pulse midpoint on the base color's green channel."""
    samples = [fx.scene(t0 + i * 0.005).base[1] for i in range(int(seconds / 0.005))]
    lo, hi = min(samples), max(samples)
    mid = (lo + hi) / 2
    return sum(1 for a, b in zip(samples, samples[1:]) if a < mid <= b)


def hue(color) -> float:
    return colorsys.rgb_to_hsv(*(c / 255 for c in color))[0] * 360


def test_none_when_not_playing() -> None:
    fx = LeagueEffects(FakeColors())
    assert fx.scene(0.0) is None                                   # before any update
    for phase in ("offline", "lobby", "post_game"):
        fx.update(LeagueState(phase=phase), [], 1.0)
        assert fx.scene(1.0) is None, phase
    fx.update(LeagueState(phase="champ_select"), [], 2.0)         # nothing hovered yet
    assert fx.scene(2.0) is None


def test_champ_select_scene() -> None:
    colors = FakeColors()
    fx = LeagueEffects(colors)
    fx.update(LeagueState(phase="champ_select", champion_key=103, skin_num=0), [], 0.0)
    sc = fx.scene(0.0)
    assert sc.base == PRIMARY
    for k in "QWERDF":
        assert sc.keys[k] == SECONDARY, k
    assert sc.edge is None                          # not stable yet: slow setting waits
    fx.update(LeagueState(phase="champ_select", champion_key=103, skin_num=0), [], 1.2)
    assert fx.scene(1.2).edge == PRIMARY
    # hovering a different champion changes colors at once but edge only after it settles
    fx.update(LeagueState(phase="champ_select", champion_key=238, skin_num=0), [], 1.4)
    sc = fx.scene(1.4)
    assert sc.base == (255, 20, 20) and sc.edge == PRIMARY
    fx.update(LeagueState(phase="champ_select", champion_key=238, skin_num=0), [], 2.6)
    assert fx.scene(2.6).edge == (255, 20, 20)
    # leaving to the lobby gives back control
    fx.update(LeagueState(phase="lobby"), [], 3.0)
    assert fx.scene(3.0) is None


def test_in_game_base_scene_and_edge() -> None:
    fx = new(now=0.0)
    sc = fx.scene(0.0)
    assert sc.base == BASE70
    for k in ("Q", "W", "E", "R"):
        assert sc.keys[k] == SECONDARY
    tint = mix(PRIMARY, WHITE, 0.7)
    assert sc.keys["D"] == tint and sc.keys["F"] == tint
    assert sum(tint) > sum(PRIMARY)                  # white-ish tint of primary
    assert sc.edge == PRIMARY
    # edge never changes while the champion stays the same, whatever else happens
    edges = set()
    for i, st in enumerate((game(100), game(20), game(0, True, 8.0), game(100))):
        fx.update(st, [], 1.0 + i)
        edges.add(fx.scene(1.0 + i).edge)
    assert edges == {PRIMARY}
    # a different champion changes it
    fx.update(game(champ="Zed"), [], 10.0)
    assert fx.scene(10.0).edge == (255, 20, 20)


def test_pulse_frequency_curve() -> None:
    assert pulse_hz(100) == 0 and pulse_hz(35) == 0 and pulse_hz(None) == 0
    assert abs(pulse_hz(34.999) - 0.8) < 0.01
    assert abs(pulse_hz(10) - 3.0) < 1e-9 and abs(pulse_hz(2) - 3.0) < 1e-9
    assert pulse_hz(30) < pulse_hz(20) < pulse_hz(12)


def test_low_health_pulse_speeds_up() -> None:
    steady = new(game(60))
    assert len({steady.scene(t * 0.1).base for t in range(50)}) == 1      # no pulse above 35%
    assert new(game(35.0)).scene(0.3).base == BASE70
    counts = {}
    for hp in (34, 25, 15, 10):
        fx = new(game(hp), now=0.0)
        counts[hp] = crossings(fx, 0.0, 10.0)
        want = pulse_hz(hp) * 10
        assert abs(counts[hp] - want) <= 1.01, (hp, counts[hp], want)
    assert counts[34] < counts[25] < counts[15] <= counts[10]
    # at phase 0 it is the normal color, half a period later it is red
    fx = new(game(30))
    hz = pulse_hz(30)
    assert fx.scene(0.0).base == BASE70
    assert fx.scene(0.5 / hz).base == RED
    # rate changes do not make the color jump
    fx = new(game(30), now=0.0)
    before = fx.scene(5.0).base
    fx.update(game(12), [], 5.0)
    assert fx.scene(5.0).base == before


def test_critical_health_pulses_ability_keys_red() -> None:
    fx = new(game(20))                              # low but not critical
    assert all(fx.scene(i * 0.05).keys["Q"] == SECONDARY for i in range(40))
    fx = new(game(12))
    hz = pulse_hz(12)
    assert fx.scene(0.0).keys["Q"] == SECONDARY
    assert fx.scene(0.5 / hz).keys["Q"] == RED and fx.scene(0.5 / hz).keys["R"] == RED


def test_dead_scene_and_respawn_bar() -> None:
    fx = new(game(0, True, 10.0), now=0.0)
    sc = fx.scene(0.0)
    assert sc.base == DEAD_GREY == (40, 40, 40) and sc.edge == PRIMARY
    assert all(sc.keys.get(k) == PRIMARY for k in "1234567890")             # full bar
    for k in "QWERDF":
        assert sc.keys.get(k, sc.base) == DEAD_GREY
    fx.update(game(0, True, 5.0), [], 5.0)
    sc = fx.scene(5.0)
    lit = [k for k in "1234567890" if sc.keys.get(k) == PRIMARY]
    assert lit == list("12345"), lit
    assert all(sc.keys.get(k, sc.base) == DEAD_GREY for k in "67890")
    # the bar keeps draining between updates
    assert sum(sc.keys.get(k) == PRIMARY for k in "1234567890") == 5
    assert sum(fx.scene(7.6).keys.get(k) == PRIMARY for k in "1234567890") == 3   # 2.4 s of 10 left
    fx.update(game(0, True, 0.5), [], 9.5)
    assert sum(fx.scene(9.5).keys.get(k) == PRIMARY for k in "1234567890") == 1
    # respawned: back to the in-game scene
    fx.update(game(100), [], 10.0)
    assert fx.scene(10.0).base == BASE70
    # a longer respawn scales the bar to its own total
    fx = new(game(0, True, 40.0), now=0.0)
    fx.update(game(0, True, 20.0), [], 20.0)
    assert sum(fx.scene(20.0).keys.get(k) == PRIMARY for k in "1234567890") == 5


def test_kill_flash_timeline() -> None:
    fx = new(now=100.0)
    normal = fx.scene(100.0)
    fx.update(game(), [ev("champion_kill", True, True, assist=False)], 100.0)
    assert fx.scene(100.0).base == WHITE
    s = fx.scene(100.1)
    assert s.base == WHITE and all(c == WHITE for c in s.keys.values())    # white at t+0.1
    mid = fx.scene(100.30).base
    assert mid != WHITE and mid != normal.base                            # fading out
    assert fx.scene(100.5).base == normal.base and fx.scene(100.5).keys == normal.keys   # gone at t+0.5
    # somebody else's kill does nothing
    fx2 = new(now=0.0)
    fx2.update(game(), [ev("champion_kill", False, True), ev("champion_kill", False, False)], 0.0)
    assert fx2.scene(0.1).base == BASE70
    # an assist is a softer flash
    fx3 = new(now=0.0)
    fx3.update(game(), [ev("champion_kill", True, True, assist=True)], 0.0)
    assert fx3.scene(0.1).base != WHITE and sum(fx3.scene(0.1).base) > sum(BASE70)


def test_multikill_flashes_n_times() -> None:
    fx = new(now=200.0)
    fx.update(game(), [ev("champion_kill", True, True, assist=False),
                       ev("multikill", True, True, kill_streak=3)], 200.0)
    samples = [fx.scene(200.0 + i * 0.01).base == WHITE for i in range(300)]
    rising = sum(1 for a, b in zip([False] + samples, samples) if b and not a)
    assert rising == 3, rising
    assert fx.scene(200.45).base != WHITE                                  # gap between flashes
    assert fx.scene(201.1).base == WHITE                                   # third flash
    assert fx.scene(201.5).base == BASE70                                  # done
    fx = new(now=0.0)
    fx.update(game(), [ev("multikill", True, True, kill_streak=2)], 0.0)
    samples = [fx.scene(i * 0.01).base == WHITE for i in range(300)]
    assert sum(1 for a, b in zip([False] + samples, samples) if b and not a) == 2


def test_pentakill_gold_shimmer() -> None:
    fx = new(now=300.0)
    fx.update(game(), [ev("champion_kill", True, True, assist=False),
                       ev("multikill", True, True, kill_streak=5)], 300.0)
    sc = fx.scene(301.5)
    assert set(KEY_NAMES) <= set(sc.keys)
    assert len(set(sc.keys.values())) > 10                                 # per-key shimmer
    hues = [hue(c) for c in sc.keys.values()]
    assert all(15 <= h <= 75 for h in hues), (min(hues), max(hues))        # gold / amber range
    assert fx.scene(301.6).keys != sc.keys                                 # it moves
    assert fx.scene(303.2).base == BASE70 and fx.scene(303.2).keys["Q"] == SECONDARY   # over after 3 s


def test_first_blood_red_flash() -> None:
    fx = new(now=0.0)
    fx.update(game(), [ev("champion_kill", True, True, assist=False), ev("first_blood", True, True)], 0.0)
    assert fx.scene(0.1).base == RED                  # red beats the kill flash from the same instant
    assert fx.scene(0.6).base == BASE70
    fx = new(now=0.0)
    fx.update(game(), [ev("first_blood", False, True)], 0.0)
    assert fx.scene(0.1).base == BASE70


def test_dragon_pulses() -> None:
    for name, color in DRAGON_COLORS.items():
        fx = new(now=400.0)
        fx.update(game(), [ev("dragon_kill", False, True, dragon=name)], 400.0)
        peak = fx.scene(400.5)
        assert peak.base == mix(BASE70, color, 0.9), (name, peak.base)
        assert peak.keys["Q"] == mix(SECONDARY, color, 0.9)
        assert fx.scene(400.0).base == BASE70                              # smooth start
        assert fx.scene(401.0).base == BASE70                              # 1 s pulse is over
    assert DRAGON_COLORS["Fire"] == (255, 90, 0) and DRAGON_COLORS["Elder"] == (200, 160, 255)
    # enemy dragon: dimmer and half as long
    fx = new(now=0.0)
    fx.update(game(), [ev("dragon_kill", False, False, dragon="Water")], 0.0)
    enemy = fx.scene(0.25).base
    assert enemy == mix(BASE70, DRAGON_COLORS["Water"], 0.4)
    assert fx.scene(0.5).base == BASE70
    fx = new(now=0.0)
    fx.update(game(), [ev("dragon_kill", False, True, dragon="Water")], 0.0)
    assert abs(enemy[2] - BASE70[2]) < abs(fx.scene(0.5).base[2] - BASE70[2])  # dimmer than ours
    assert fx.scene(0.6).base != BASE70                                    # ours still going


def test_baron_herald_ace() -> None:
    fx = new(now=0.0)
    fx.update(game(), [ev("baron_kill", False, True)], 0.0)
    assert fx.scene(1.0).base == mix(BASE70, (140, 0, 255), 0.9)           # 2 s purple, peak mid-way
    assert fx.scene(1.9).base != BASE70 and fx.scene(2.05).base == BASE70
    fx = new(now=0.0)
    fx.update(game(), [ev("baron_kill", False, False)], 0.0)
    assert fx.scene(1.0).base == BASE70                                    # enemy baron: short & dim
    assert fx.scene(0.5).base == mix(BASE70, (140, 0, 255), 0.4)
    fx = new(now=0.0)
    fx.update(game(), [ev("ace", False, True)], 0.0)
    assert fx.scene(0.3).base == GOLD and fx.scene(1.1).base == BASE70
    fx = new(now=0.0)
    fx.update(game(), [ev("ace", False, False)], 0.0)
    assert fx.scene(0.3).base == BASE70
    fx = new(now=0.0)
    fx.update(game(), [ev("herald_kill", False, True)], 0.0)
    assert fx.scene(0.5).base != BASE70 and fx.scene(1.1).base == BASE70


def test_game_end_win_and_lose() -> None:
    fx = new(now=700.0)
    fx.update(game(), [ev("game_end", True, True, result="Win")], 700.0)
    sc = fx.scene(702.0)
    assert sc is not None and set(KEY_NAMES) <= set(sc.keys)
    assert all(15 <= hue(c) <= 75 for c in sc.keys.values())               # gold shimmer
    assert fx.scene(703.9) is not None and fx.scene(704.0) is None         # then hands back control
    fx.update(game(), [], 705.0)                                           # live API still up
    assert fx.scene(705.0) is None
    fx.update(LeagueState(phase="post_game"), [], 706.0)
    assert fx.scene(706.0) is None
    fx.update(game(), [], 800.0)                                           # a fresh game works again
    assert fx.scene(800.0).base == BASE70

    fx = new(now=0.0)
    fx.update(game(), [ev("game_end", True, True, result="Lose")], 0.0)
    early, late = fx.scene(0.5).base, fx.scene(3.0).base
    assert early[0] > late[0] > 0 and late[1] < 20 and late[2] < 20        # fades to dim red
    assert abs(late[0] - 70) <= 2 and fx.scene(4.5) is not None
    assert fx.scene(5.0) is None
    # dead players still get the end effect then None
    fx = new(game(0, True, 20), now=0.0)
    fx.update(game(0, True, 19), [ev("game_end", True, True, result="Lose")], 1.0)
    assert fx.scene(2.0) is not None and fx.scene(6.1) is None


def test_palette_loading_and_fallback() -> None:
    colors = FakeColors(ready=False)
    fx = LeagueEffects(colors)
    fx.update(game(), [], 0.0)
    assert fx.scene(0.0) is None                        # no flash of wrong colors while loading
    assert colors.prefetched and colors.prefetched[0] == ("Ahri", 0)
    colors.ready = True
    fx.update(game(), [], 1.0)
    assert fx.scene(1.0).base == BASE70
    # skin palette still downloading: use the base skin's colors, but do not set edge yet
    colors = FakeColors()
    fx = LeagueEffects(colors)
    st = game()
    st.skin_num = 5
    fx.update(st, [], 0.0)
    sc = fx.scene(0.0)
    assert sc.base == BASE70 and sc.edge is None and ("Ahri", 5) in colors.prefetched
    colors.skin_palettes[("Ahri", 5)] = ((10, 200, 10), (200, 10, 10))
    fx.update(st, [], 0.2)
    sc = fx.scene(0.2)
    assert sc.base == scale((10, 200, 10), 0.7) and sc.edge == (10, 200, 10)


def test_events_ignored_outside_game() -> None:
    fx = LeagueEffects(FakeColors())
    fx.update(LeagueState(phase="champ_select", champion="Ahri"), [ev("champion_kill", True, True)], 0.0)
    assert fx.scene(0.1).base == PRIMARY
    fx.update(LeagueState(phase="lobby"), [ev("game_end", True, True, result="Lose")], 1.0)
    fx.update(game(), [], 2.0)
    assert fx.scene(2.0).base == BASE70


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("league effects tests passed")
