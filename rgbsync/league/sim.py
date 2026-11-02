"""League simulator: a scripted fake game for testing the effects without League.

``FakeLeagueWatcher`` has the same interface as ``LeagueWatcher`` (``start``,
``stop``, ``state``, ``drain_events``) but follows a scripted timeline: champ
select (Ahri) -> in game -> damage -> kill -> triple kill -> my team's fire
dragon -> death with a 10 s respawn -> respawn -> baron -> win.

Run ``python -m rgbsync.league.sim --print`` to see the scenes in the console
(no hardware, no network). Add ``--realtime`` to pace it in real time and
``--real-colors`` to use real extracted champion colors instead of the built-in
Ahri palette.
"""
from __future__ import annotations

import argparse
import threading
import time
from typing import Callable

from ..scene import RGB, Scene
from .client import LeagueEvent, LeagueState
from .colors import to_hex
from .effects import LeagueEffects

#: (time, event) pairs; times are seconds since the script started.
TIMELINE_EVENTS: list[tuple[float, LeagueEvent]] = [
    (12.0, LeagueEvent("first_blood", True, True, {"recipient": "TestPlayer#NA1"})),
    (12.0, LeagueEvent("champion_kill", True, True,
                       {"killer": "TestPlayer#NA1", "victim": "EnemyOne#NA1", "assisters": [],
                        "assist": False, "victim_is_me": False})),
    (16.0, LeagueEvent("champion_kill", True, True,
                       {"killer": "TestPlayer#NA1", "victim": "EnemyTwo#NA1", "assisters": [],
                        "assist": False, "victim_is_me": False})),
    (16.0, LeagueEvent("multikill", True, True, {"killer": "TestPlayer#NA1", "kill_streak": 3})),
    (20.0, LeagueEvent("dragon_kill", False, True,
                       {"killer": "AllyOne#NA1", "dragon": "Fire", "stolen": False})),
    (30.0, LeagueEvent("dragon_kill", False, False,
                       {"killer": "EnemyOne#NA1", "dragon": "Water", "stolen": False})),
    (46.0, LeagueEvent("baron_kill", False, True, {"killer": "AllyTwo#NA1", "stolen": False})),
    (50.0, LeagueEvent("game_end", True, True, {"result": "Win"})),
]

CHAMP_SELECT_END = 4.0
DEATH_AT, RESPAWN_SECONDS = 34.0, 10.0
GAME_END_AT = 50.0
SCRIPT_END = 56.0


def state_at(t: float) -> LeagueState:
    """The scripted LeagueState ``t`` seconds into the script."""
    if t < 0:
        return LeagueState(phase="lobby")
    if t < CHAMP_SELECT_END:
        return LeagueState(phase="champ_select", champion="Ahri", champion_key=103, skin_num=0)
    if t >= SCRIPT_END:
        return LeagueState(phase="post_game")
    hp = 100.0
    if 8.0 <= t < 12.0:
        hp = 100.0 - (t - 8.0) / 4.0 * 80.0     # damage down to 20%
    elif 12.0 <= t < DEATH_AT:
        hp = 20.0 if t < 22.0 else min(100.0, 20.0 + (t - 22.0) * 10.0)  # slowly heals
    st = LeagueState(phase="in_game", champion="Ahri", champion_key=None, skin_num=0,
                     health_pct=hp, my_team="ORDER", game_time=t - CHAMP_SELECT_END)
    if DEATH_AT <= t < DEATH_AT + RESPAWN_SECONDS:
        st.is_dead, st.health_pct = True, 0.0
        st.respawn_in = DEATH_AT + RESPAWN_SECONDS - t
    elif t >= DEATH_AT + RESPAWN_SECONDS:
        st.health_pct = 100.0
    return st


class FakeLeagueWatcher:
    """Drop-in stand-in for LeagueWatcher driven by the scripted timeline."""

    def __init__(self, speed: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 loop: bool = False) -> None:
        self.speed = speed
        self.loop = loop
        self._clock = clock
        self._t0: float | None = None
        self._next_event = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._t0 = self._clock()
            self._next_event = 0

    def stop(self) -> None:
        with self._lock:
            self._t0 = None

    def _elapsed(self) -> float:
        if self._t0 is None:
            return -1.0
        t = (self._clock() - self._t0) * self.speed
        if self.loop and t >= SCRIPT_END + 2:
            self._t0 += (SCRIPT_END + 2) / self.speed
            self._next_event = 0
            t = (self._clock() - self._t0) * self.speed
        return t

    def state(self) -> LeagueState:
        with self._lock:
            return state_at(self._elapsed())

    def drain_events(self) -> list[LeagueEvent]:
        with self._lock:
            t = self._elapsed()
            out = []
            while self._next_event < len(TIMELINE_EVENTS) and TIMELINE_EVENTS[self._next_event][0] <= t:
                out.append(TIMELINE_EVENTS[self._next_event][1])
                self._next_event += 1
            return out


class _StaticColors:
    """Offline palette source for the simulator (Ahri-like pink and violet)."""

    PALETTE: tuple[RGB, RGB] = ((255, 79, 163), (122, 60, 255))

    def peek(self, champion, skin_num=0):
        return self.PALETTE

    def prefetch(self, champion, skin_num=0) -> None:
        pass


def describe(scene: Scene | None) -> str:
    """One-line text form of a scene: base, a few keys and the edge color."""
    if scene is None:
        return "scene=None (app's normal color)"
    pick = ("Q", "R", "D", "1", "5", "0")
    keys = " ".join(f"{k}={to_hex(scene.keys.get(k, scene.base))}" for k in pick)
    edge = to_hex(scene.edge) if scene.edge else "-"
    return f"base={to_hex(scene.base)} {keys} edge={edge}"


def run_print(realtime: bool = False, real_colors: bool = False, step: float = 0.1,
              every: float = 0.5) -> None:
    """Drive the script and print scenes every ``every`` seconds (and each event)."""
    if real_colors:
        from .colors import ChampionColors
        colors = ChampionColors()
    else:
        colors = _StaticColors()
    now = 0.0
    watcher = FakeLeagueWatcher(clock=lambda: now)
    effects = LeagueEffects(colors)
    watcher.start()
    next_print = 0.0
    while now <= SCRIPT_END + 1:
        state = watcher.state()
        events = watcher.drain_events()
        effects.update(state, events, now)
        for ev in events:
            print(f"      event: {ev.kind} mine={ev.mine} my_team={ev.my_team} {ev.detail}")
        if now + 1e-9 >= next_print:
            hp = "-" if state.health_pct is None else f"{state.health_pct:3.0f}%"
            dead = f" dead respawn={state.respawn_in:4.1f}s" if state.is_dead else ""
            print(f"t={now:5.1f} {state.phase:<12} hp={hp}{dead} | {describe(effects.scene(now))}")
            next_print += every
        if realtime:
            time.sleep(step)
        now = round(now + step, 6)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a League game and print the scenes.")
    parser.add_argument("--print", dest="print_", action="store_true", help="print scenes to the console")
    parser.add_argument("--realtime", action="store_true", help="pace the output in real time")
    parser.add_argument("--real-colors", action="store_true",
                        help="use extracted champion colors (may use the network)")
    args = parser.parse_args()
    if not args.print_:
        parser.print_help()
        return
    run_print(realtime=args.realtime, real_colors=args.real_colors)


if __name__ == "__main__":
    main()
