"""League of Legends data sources: the League Client (LCU) and the Live Client API.

* Champ select / lobby / post-game come from the League Client's local HTTPS
  API (``riot:<password>`` basic auth, port and password from the lockfile).
* In game, the official Live Client Data API on ``https://127.0.0.1:2999``
  gives health, death state, skin and the event feed. It needs no auth.

Both use self-signed certificates, so TLS verification is off for these local
calls only (and the matching urllib3 warning is silenced only around them).
League being closed is the normal case: every error just means "not
available" and is logged at DEBUG. The LCU password is never logged or stored
beyond memory, and is redacted from any exception text.

Parsing is done by pure functions (``parse_*``, ``EventTracker``) so it can be
tested from fixture JSON without League running.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
import warnings
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from urllib3.exceptions import InsecureRequestWarning

log = logging.getLogger(__name__)

DEFAULT_LOCKFILE = r"C:\Riot Games\League of Legends\lockfile"
LIVE_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
TIMEOUT = 0.5

PHASES = ("offline", "lobby", "champ_select", "in_game", "post_game")

#: Raw Live Client event name -> LeagueEvent.kind
EVENT_KINDS = {
    "ChampionKill": "champion_kill",
    "Multikill": "multikill",
    "Ace": "ace",
    "DragonKill": "dragon_kill",
    "HeraldKill": "herald_kill",
    "BaronKill": "baron_kill",
    "TurretKilled": "turret_killed",
    "InhibKilled": "inhib_killed",
    "FirstBlood": "first_blood",
    "GameEnd": "game_end",
}

#: On the first poll of a game, events older than this (seconds of game time)
#: are history (e.g. we started the app mid-game) and are not replayed.
STALE_EVENT_SECONDS = 5.0


@dataclass
class LeagueState:
    """Snapshot of what League is doing right now."""

    #: "offline", "lobby", "champ_select", "in_game" or "post_game".
    phase: str = "offline"
    #: Data Dragon champion id, e.g. "Ahri" or "MonkeyKing" (None if unknown).
    champion: str | None = None
    #: Numeric champion key (champ select only; None in game).
    champion_key: int | None = None
    skin_num: int | None = None
    #: 0-100.
    health_pct: float | None = None
    is_dead: bool = False
    respawn_in: float | None = None
    #: "ORDER" or "CHAOS".
    my_team: str | None = None
    game_time: float | None = None


@dataclass
class LeagueEvent:
    """A game event, already classified relative to the local player."""

    #: One of ``EVENT_KINDS`` values, e.g. "champion_kill", "dragon_kill".
    kind: str
    #: The local player did it (killer or assister).
    mine: bool = False
    #: The local player's team did it.
    my_team: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    event_id: int | None = None
    event_time: float | None = None


# ---------------------------------------------------------------- credentials


@dataclass(frozen=True)
class LcuCredentials:
    """Port and password of the running League Client. Password never shown."""

    port: int
    password: str = field(repr=False)
    pid: int | None = None
    protocol: str = "https"

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"


def parse_lockfile(text: str) -> LcuCredentials | None:
    """Parse ``name:pid:port:password:protocol``. Returns None if malformed."""
    parts = text.strip().split(":")
    if len(parts) != 5:
        return None
    _name, pid, port, password, protocol = parts
    if not (port.isdigit() and password):
        return None
    return LcuCredentials(int(port), password, int(pid) if pid.isdigit() else None,
                          protocol or "https")


def parse_cmdline(cmdline: str) -> LcuCredentials | None:
    """Pull ``--app-port`` / ``--remoting-auth-token`` out of a LeagueClientUx command line."""
    port = re.search(r"--app-port=(\d+)", cmdline)
    token = re.search(r"--remoting-auth-token=([^\s\"']+)", cmdline)
    if not (port and token):
        return None
    return LcuCredentials(int(port.group(1)), token.group(1))


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _credentials_from_process() -> LcuCredentials | None:
    """Fallback: read the LeagueClientUx.exe command line (PowerShell CIM, then wmic)."""
    attempts = (
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\").CommandLine"],
        ["wmic", "process", "where", "name='LeagueClientUx.exe'", "get", "commandline"],
    )
    for cmd in attempts:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=6,
                                 creationflags=_creation_flags())
        except (OSError, subprocess.SubprocessError):
            continue
        creds = parse_cmdline(out.stdout or "")
        if creds:
            return creds
    return None


def credentials_from_lockfile(lockfile_paths: list[str] | None = None) -> LcuCredentials | None:
    """Read the first readable, well-formed lockfile (cheap; safe to call every second)."""
    for path in lockfile_paths or [DEFAULT_LOCKFILE]:
        try:
            with open(path, encoding="utf-8") as fh:
                creds = parse_lockfile(fh.read())
        except OSError:
            continue
        if creds:
            return creds
    return None


def default_install_present(lockfile_paths: list[str] | None = None) -> bool:
    """True if a lockfile directory exists (League installed where we expect)."""
    return any(os.path.isdir(os.path.dirname(p)) for p in lockfile_paths or [DEFAULT_LOCKFILE])


def find_credentials(lockfile_paths: list[str] | None = None,
                     use_process_fallback: bool = True) -> LcuCredentials | None:
    """Locate the League Client: lockfile first, then (if allowed) its command line.

    The slow process lookup is only tried when the expected install directory
    does not exist; with a default install and no lockfile the client simply
    is not running.
    """
    creds = credentials_from_lockfile(lockfile_paths)
    if creds or not use_process_fallback or default_install_present(lockfile_paths):
        return creds
    return _credentials_from_process()


def redact(text: str, *secrets: str | None) -> str:
    """Remove secrets (and anything that looks like an auth token) from text."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(r"(remoting-auth-token=)[^\s\"']+", r"\1***", text)
    text = re.sub(r"(Authorization: ?Basic )\S+", r"\1***", text, flags=re.I)
    return text


# ---------------------------------------------------------------------- HTTP


class Unavailable(Exception):
    """A League endpoint could not be reached or answered badly."""


def http_get_json(url: str, auth: tuple[str, str] | None = None,
                  timeout: float = TIMEOUT) -> Any:
    """GET a local League endpoint as JSON. Raises ``Unavailable`` with a redacted message."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            resp = requests.get(url, auth=auth, verify=False, timeout=timeout)
        if resp.status_code != 200:
            raise Unavailable(f"HTTP {resp.status_code}")
        return resp.json()
    except Unavailable:
        raise
    except Exception as exc:  # network errors, bad JSON, ...
        raise Unavailable(redact(f"{type(exc).__name__}: {exc}", auth[1] if auth else None)) from None


# -------------------------------------------------------------- LCU parsing


def parse_gameflow_phase(value: Any) -> str:
    """Map an LCU gameflow phase string to one of ``PHASES`` (never "offline")."""
    phase = value if isinstance(value, str) else ""
    if phase == "ChampSelect":
        return "champ_select"
    if phase in ("GameStart", "InProgress", "Reconnect"):
        return "in_game"
    if phase in ("WaitingForStats", "PreEndOfGame", "EndOfGame"):
        return "post_game"
    return "lobby"


def parse_champ_select(session: dict[str, Any]) -> LeagueState:
    """Build a champ-select state from ``/lol-champ-select/v1/session``."""
    state = LeagueState(phase="champ_select")
    cell = session.get("localPlayerCellId")
    for member in session.get("myTeam") or []:
        if member.get("cellId") != cell:
            continue
        key = member.get("championId") or member.get("championPickIntent") or 0
        if key:
            state.champion_key = int(key)
            skin = member.get("selectedSkinId") or 0
            # LCU skin ids are champion_key * 1000 + skin number.
            state.skin_num = int(skin) % 1000 if skin else 0
        break
    return state


# ------------------------------------------------------- Live Client parsing


def _norm(name: Any) -> str:
    return name.strip().lower() if isinstance(name, str) else ""


class Roster:
    """Who is who in an ``allgamedata`` payload."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.players: list[dict[str, Any]] = list(data.get("allPlayers") or [])
        active = data.get("activePlayer") or {}
        self.active = active
        self.me = self._find_me()
        self.my_names: set[str] = set()
        if self.me is not None:
            for key in ("riotId", "summonerName", "riotIdGameName"):
                for src in (self.me, active):
                    self._add(self.my_names, src.get(key))
        self.my_team: str | None = self.me.get("team") if self.me else None
        self._by_name: dict[str, dict[str, Any]] = {}
        for player in self.players:
            for key in ("riotId", "summonerName", "riotIdGameName"):
                self._add_player(player, player.get(key))

    @staticmethod
    def _add(names: set[str], value: Any) -> None:
        n = _norm(value)
        if n:
            names.add(n)
            names.add(n.split("#")[0])

    def _add_player(self, player: dict[str, Any], value: Any) -> None:
        n = _norm(value)
        if n:
            self._by_name.setdefault(n, player)
            self._by_name.setdefault(n.split("#")[0], player)

    def _find_me(self) -> dict[str, Any] | None:
        for key in ("riotId", "summonerName", "riotIdGameName"):
            wanted = _norm(self.active.get(key))
            if not wanted:
                continue
            for player in self.players:
                if _norm(player.get(key)) == wanted:
                    return player
        return None

    def player(self, name: Any) -> dict[str, Any] | None:
        n = _norm(name)
        return self._by_name.get(n) or self._by_name.get(n.split("#")[0]) if n else None

    def is_me(self, name: Any) -> bool:
        n = _norm(name)
        return bool(n) and (n in self.my_names or n.split("#")[0] in self.my_names)

    def team_of(self, name: Any) -> str | None:
        player = self.player(name)
        return player.get("team") if player else None


def champion_id_from_player(player: dict[str, Any]) -> str | None:
    """Data Dragon id from ``rawChampionName`` ("game_character_displayname_MonkeyKing")."""
    raw = player.get("rawChampionName")
    if isinstance(raw, str) and raw:
        return raw.rsplit("_", 1)[-1] or None
    name = player.get("championName")
    return re.sub(r"[^A-Za-z0-9]", "", name) if isinstance(name, str) and name else None


def parse_live_game(data: dict[str, Any]) -> LeagueState:
    """Build an in-game state from a Live Client ``allgamedata`` payload."""
    roster = Roster(data)
    state = LeagueState(phase="in_game")
    game = data.get("gameData") or {}
    if isinstance(game.get("gameTime"), (int, float)):
        state.game_time = float(game["gameTime"])
    stats = (roster.active.get("championStats") or {})
    cur, mx = stats.get("currentHealth"), stats.get("maxHealth")
    if isinstance(cur, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
        state.health_pct = max(0.0, min(100.0, cur / mx * 100.0))
    me = roster.me
    if me is not None:
        state.champion = champion_id_from_player(me)
        skin = me.get("skinID")
        state.skin_num = int(skin) if isinstance(skin, (int, float)) else 0
        state.is_dead = bool(me.get("isDead"))
        timer = me.get("respawnTimer")
        state.respawn_in = float(timer) if isinstance(timer, (int, float)) else None
        state.my_team = me.get("team")
        if state.is_dead:
            state.health_pct = 0.0
    return state


def _stamp(event: LeagueEvent, raw: dict[str, Any]) -> LeagueEvent:
    event.event_id = raw.get("EventID")
    et = raw.get("EventTime")
    event.event_time = float(et) if isinstance(et, (int, float)) else None
    return event


def classify_event(raw: dict[str, Any], roster: Roster) -> LeagueEvent | None:
    """Turn one raw Live Client event into a LeagueEvent (None if irrelevant)."""
    kind = EVENT_KINDS.get(raw.get("EventName"))
    if kind is None:
        return None
    team = roster.my_team

    def same_team(name: Any, fallback_victim: Any = None) -> bool:
        t = roster.team_of(name)
        if t is None and fallback_victim is not None:
            v = roster.team_of(fallback_victim)
            return v is not None and team is not None and v != team
        return t is not None and t == team

    ev = LeagueEvent(kind)
    if kind == "champion_kill":
        killer, victim = raw.get("KillerName"), raw.get("VictimName")
        assisters = list(raw.get("Assisters") or [])
        i_kill = roster.is_me(killer)
        i_assist = any(roster.is_me(a) for a in assisters)
        ev.mine = i_kill or i_assist
        ev.my_team = same_team(killer, victim)
        ev.detail = {"killer": killer, "victim": victim, "assisters": assisters,
                     "assist": i_assist and not i_kill, "victim_is_me": roster.is_me(victim)}
    elif kind == "multikill":
        killer = raw.get("KillerName")
        ev.mine = roster.is_me(killer)
        ev.my_team = same_team(killer)
        ev.detail = {"killer": killer, "kill_streak": int(raw.get("KillStreak") or 0)}
    elif kind == "ace":
        acer = raw.get("Acer")
        ev.mine = roster.is_me(acer)
        ev.my_team = bool(team) and raw.get("AcingTeam") == team
        ev.detail = {"acer": acer, "team": raw.get("AcingTeam")}
    elif kind in ("dragon_kill", "herald_kill", "baron_kill"):
        killer = raw.get("KillerName")
        ev.mine = roster.is_me(killer) or any(roster.is_me(a) for a in raw.get("Assisters") or [])
        ev.my_team = same_team(killer)
        ev.detail = {"killer": killer, "stolen": str(raw.get("Stolen")).lower() == "true"}
        if kind == "dragon_kill":
            ev.detail["dragon"] = raw.get("DragonType")
    elif kind in ("turret_killed", "inhib_killed"):
        killer = raw.get("KillerName")
        ev.mine = roster.is_me(killer) or any(roster.is_me(a) for a in raw.get("Assisters") or [])
        ev.my_team = same_team(killer)
        ev.detail = {"killer": killer, "target": raw.get("TurretKilled") or raw.get("InhibKilled")}
    elif kind == "first_blood":
        who = raw.get("Recipient")
        ev.mine = roster.is_me(who)
        ev.my_team = same_team(who)
        ev.detail = {"recipient": who}
    elif kind == "game_end":
        ev.mine = ev.my_team = True
        ev.detail = {"result": raw.get("Result")}
    return _stamp(ev, raw)


class EventTracker:
    """Emits each Live Client event once, by tracking the highest EventID seen."""

    def __init__(self) -> None:
        self._last_id: int | None = None
        self._last_time: float | None = None

    def reset(self) -> None:
        self._last_id = None
        self._last_time = None

    def process(self, data: dict[str, Any]) -> list[LeagueEvent]:
        raw_events = [e for e in ((data.get("events") or {}).get("Events") or [])
                      if isinstance(e, dict) and isinstance(e.get("EventID"), int)]
        raw_events.sort(key=lambda e: e["EventID"])
        gt = (data.get("gameData") or {}).get("gameTime")
        gt = float(gt) if isinstance(gt, (int, float)) else None
        # A new game restarts the clock / ids.
        if self._last_time is not None and gt is not None and gt < self._last_time - 5:
            self.reset()
        if raw_events and self._last_id is not None and raw_events[-1]["EventID"] < self._last_id:
            self.reset()
        first_poll = self._last_id is None
        if gt is not None:
            self._last_time = gt
        roster = Roster(data)
        out: list[LeagueEvent] = []
        for raw in raw_events:
            eid = raw["EventID"]
            if self._last_id is not None and eid <= self._last_id:
                continue
            self._last_id = eid
            et = raw.get("EventTime")
            if first_poll and gt is not None and isinstance(et, (int, float)) \
                    and et < gt - STALE_EVENT_SECONDS:
                continue
            ev = classify_event(raw, roster)
            if ev is not None:
                out.append(ev)
        if first_poll and self._last_id is None:
            self._last_id = -1  # primed: later events are all new
        return out


# ------------------------------------------------------------------ watcher


FetchFn = Callable[..., Any]


class LeagueWatcher:
    """Background poller exposing ``state()`` and ``drain_events()``.

    Polls the League Client about once a second while not in game and the
    Live Client API ~5 times a second in game. Never raises to the caller.
    """

    LIVE_GRACE_FAILS = 3  # transient live failures tolerated before leaving in_game

    def __init__(self, lockfile_paths: list[str] | None = None,
                 use_process_fallback: bool = True,
                 fetch: FetchFn | None = None,
                 lcu_interval: float = 1.0, live_interval: float = 0.2) -> None:
        self._lockfile_paths = lockfile_paths
        self._use_fallback = use_process_fallback
        self._fetch: FetchFn = fetch or http_get_json
        self.lcu_interval = lcu_interval
        self.live_interval = live_interval
        self._creds: LcuCredentials | None = None
        self._next_cred_lookup = 0.0
        self._lock = threading.Lock()
        self._state = LeagueState()
        self._events: deque[LeagueEvent] = deque(maxlen=200)
        self._tracker = EventTracker()
        self._live_up = False
        self._live_fails = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- public interface ------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="LeagueWatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def state(self) -> LeagueState:
        with self._lock:
            return LeagueState(**self._state.__dict__)

    def drain_events(self) -> list[LeagueEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    # -- polling ---------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.poll_once()
            except Exception as exc:  # never let the thread die
                log.debug("League poll failed: %s", type(exc).__name__)
                delay = self.lcu_interval
            self._stop.wait(delay)

    def poll_once(self) -> float:
        """Run one poll cycle; returns seconds to wait before the next."""
        if self._live_up:
            if self._poll_live():
                return self.live_interval
            self._live_fails += 1
            if self._live_fails <= self.LIVE_GRACE_FAILS:
                return self.live_interval
            self._live_up = False
            self._tracker.reset()
        phase = self._poll_lcu()
        if self._poll_live():
            return self.live_interval
        if phase is None:
            self._publish(LeagueState())
        return self.lcu_interval

    def _publish(self, state: LeagueState, events: list[LeagueEvent] | None = None) -> None:
        with self._lock:
            self._state = state
            if events:
                self._events.extend(events)

    def _poll_live(self) -> bool:
        try:
            data = self._fetch(LIVE_URL, None, TIMEOUT)
        except Exception as exc:
            log.debug("Live Client not available: %s", exc)
            return False
        if not isinstance(data, dict) or "allPlayers" not in data:
            return False
        self._live_up, self._live_fails = True, 0
        self._publish(parse_live_game(data), self._tracker.process(data))
        return True

    def _credentials(self) -> LcuCredentials | None:
        if self._creds is None:
            self._creds = credentials_from_lockfile(self._lockfile_paths)
            now = time.monotonic()
            if (self._creds is None and self._use_fallback and now >= self._next_cred_lookup
                    and not default_install_present(self._lockfile_paths)):
                self._next_cred_lookup = now + 20.0  # process lookup is slow
                self._creds = _credentials_from_process()
        return self._creds

    def _poll_lcu(self) -> str | None:
        """Poll the League Client and publish state. Returns the phase or None if unreachable."""
        creds = self._credentials()
        if creds is None:
            return None
        auth = ("riot", creds.password)
        try:
            raw_phase = self._fetch(f"{creds.base_url}/lol-gameflow/v1/gameflow-phase", auth, TIMEOUT)
            phase = parse_gameflow_phase(raw_phase)
            if phase == "champ_select":
                session = self._fetch(f"{creds.base_url}/lol-champ-select/v1/session", auth, TIMEOUT)
                state = parse_champ_select(session if isinstance(session, dict) else {})
            else:
                state = LeagueState(phase=phase)
        except Exception as exc:
            log.debug("League Client not available: %s", redact(str(exc), creds.password))
            self._creds = None  # re-read the lockfile next time (port changes per launch)
            return None
        self._publish(state)
        return phase
