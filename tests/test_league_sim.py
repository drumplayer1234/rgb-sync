"""League simulator tests: the scripted fake watcher drives the real effects. No network.

Run: python tests/test_league_sim.py   (plain asserts, no pytest needed)
"""
import contextlib
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.league import sim  # noqa: E402
from rgbsync.league.effects import LeagueEffects  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_fake_watcher_follows_script() -> None:
    clock = Clock()
    w = sim.FakeLeagueWatcher(clock=clock)
    assert w.state().phase == "lobby"                      # not started
    w.start()
    clock.t = 1.0
    st = w.state()
    assert st.phase == "champ_select" and st.champion == "Ahri" and st.champion_key == 103
    clock.t = 6.0
    assert w.state().phase == "in_game" and w.state().health_pct == 100.0
    clock.t = 11.0
    assert 20 < w.state().health_pct < 60
    assert w.drain_events() == []
    clock.t = 16.5
    kinds = [e.kind for e in w.drain_events()]
    assert kinds == ["first_blood", "champion_kill", "champion_kill", "multikill"], kinds
    assert w.drain_events() == []                          # each event delivered once
    clock.t = 20.5
    ev = w.drain_events()
    assert ev[0].kind == "dragon_kill" and ev[0].my_team and ev[0].detail["dragon"] == "Fire"
    clock.t = 36.0
    st = w.state()
    assert st.is_dead and abs(st.respawn_in - 8.0) < 1e-9 and st.health_pct == 0
    clock.t = 45.0
    assert not w.state().is_dead
    clock.t = 60.0
    assert [e.kind for e in w.drain_events()] == ["dragon_kill", "baron_kill", "game_end"]
    assert w.state().phase == "post_game"
    w.stop()
    assert w.state().phase == "lobby"
    # same interface as the real watcher
    from rgbsync.league.client import LeagueWatcher
    for name in ("start", "stop", "state", "drain_events"):
        assert callable(getattr(sim.FakeLeagueWatcher, name)) and callable(getattr(LeagueWatcher, name))


def test_script_drives_effects() -> None:
    clock = Clock()
    w = sim.FakeLeagueWatcher(clock=clock)
    fx = LeagueEffects(sim._StaticColors())
    w.start()
    seen = {"none": 0, "dead_grey": 0, "white": 0, "orange": 0, "purple": 0, "gold": 0}
    while clock.t < sim.SCRIPT_END + 1:
        fx.update(w.state(), w.drain_events(), clock.t)
        sc = fx.scene(clock.t)
        if sc is None:
            seen["none"] += 1
        elif sc.base == (40, 40, 40):
            seen["dead_grey"] += 1
        elif sc.base == (255, 255, 255):
            seen["white"] += 1
        elif sc.base[0] > 230 and sc.base[1] < 100 and sc.base[2] < 40:
            seen["orange"] += 1
        elif sc.base[2] > 200 and sc.base[1] < 40:
            seen["purple"] += 1
        elif len(set(sc.keys.values())) > 20:
            seen["gold"] += 1
        clock.t = round(clock.t + 0.1, 6)
    assert all(v > 0 for v in seen.values()), seen


def test_print_mode_runs() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        sim.run_print()
    text = out.getvalue()
    assert "champ_select" in text and "dead respawn" in text and "event: multikill" in text
    assert "scene=None" in text and "edge=#ff4fa3" in text
    assert len(text.splitlines()) > 100


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("league sim tests passed")
