"""League client parsing tests from synthetic fixture JSON. No network, no League.

Run: python tests/test_league_client.py   (plain asserts, no pytest needed)
"""
import copy
import json
import logging
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.league import client  # noqa: E402
from rgbsync.league.client import (  # noqa: E402
    EventTracker, LeagueWatcher, Unavailable, parse_champ_select, parse_cmdline,
    parse_gameflow_phase, parse_live_game, parse_lockfile, redact)

FIX = ROOT / "tests" / "fixtures" / "league"
FAKE_PASSWORD = "not-a-real-password-123"


def load(name: str):
    return json.loads((FIX / name).read_text("utf-8"))


def test_lockfile_and_redaction() -> None:
    creds = parse_lockfile(f"LeagueClient:4242:51234:{FAKE_PASSWORD}:https\n")
    assert creds and creds.port == 51234 and creds.pid == 4242
    assert creds.password == FAKE_PASSWORD and creds.base_url == "https://127.0.0.1:51234"
    assert FAKE_PASSWORD not in repr(creds) and FAKE_PASSWORD not in str(creds)
    assert parse_lockfile("garbage") is None
    assert parse_lockfile("a:1:notaport:pw:https") is None
    cmd = ('"C:\\Riot Games\\League of Legends\\LeagueClientUx.exe" --app-port=51234 '
           f'--remoting-auth-token={FAKE_PASSWORD} --riotclient-app-port=1 --locale=en_US')
    c2 = parse_cmdline(cmd)
    assert c2 and c2.port == 51234 and c2.password == FAKE_PASSWORD
    assert parse_cmdline("no args here") is None
    text = redact(f"conn failed to riot:{FAKE_PASSWORD}@host token={FAKE_PASSWORD}", FAKE_PASSWORD)
    assert FAKE_PASSWORD not in text
    assert FAKE_PASSWORD not in redact(f"--remoting-auth-token={FAKE_PASSWORD}")


def test_http_errors_are_redacted() -> None:
    real_get = client.requests.get

    def boom(url, **kw):
        raise RuntimeError(f"cannot reach {url} using {kw['auth'][1]}")
    client.requests.get = boom
    try:
        try:
            client.http_get_json("https://127.0.0.1:1/x", ("riot", FAKE_PASSWORD))
            raise AssertionError("expected Unavailable")
        except Unavailable as exc:
            assert FAKE_PASSWORD not in str(exc)
            assert exc.__cause__ is None  # original (with password) not chained
    finally:
        client.requests.get = real_get


def test_gameflow_and_champ_select() -> None:
    assert parse_gameflow_phase(load("gameflow_phase.json")) == "champ_select"
    for raw, want in (("None", "lobby"), ("Lobby", "lobby"), ("Matchmaking", "lobby"),
                      ("ReadyCheck", "lobby"), ("InProgress", "in_game"),
                      ("WaitingForStats", "post_game"), ("EndOfGame", "post_game"), (None, "lobby")):
        assert parse_gameflow_phase(raw) == want, raw
    st = parse_champ_select(load("champ_select_session.json"))
    # local cell 2 is only hovering champion 103 (pick intent) with skin id 103003
    assert st.phase == "champ_select" and st.champion_key == 103 and st.skin_num == 3
    session = load("champ_select_session.json")
    session["myTeam"][1]["championId"] = 238  # locked in
    assert parse_champ_select(session).champion_key == 238
    session["myTeam"][1].update(championId=0, championPickIntent=0)
    assert parse_champ_select(session).champion_key is None  # nothing hovered
    assert parse_champ_select({}).champion_key is None


def test_live_game_state() -> None:
    st = parse_live_game(load("allgamedata_in_game.json"))
    assert st.phase == "in_game" and st.champion == "Ahri" and st.skin_num == 3
    assert abs(st.health_pct - 62.0) < 1e-6 and not st.is_dead
    assert st.my_team == "ORDER" and abs(st.game_time - 905.5) < 1e-6
    dead = parse_live_game(load("allgamedata_dead.json"))
    assert dead.is_dead and dead.respawn_in == 12.5 and dead.health_pct == 0.0
    # raw name gives the Data Dragon id, not the display name
    data = load("allgamedata_in_game.json")
    data["activePlayer"]["riotId"] = "AllyOne#NA1"
    data["activePlayer"]["summonerName"] = "AllyOne"
    data["activePlayer"]["riotIdGameName"] = "AllyOne"
    assert parse_live_game(data).champion == "MonkeyKing"
    # fallback matching: riotId unknown, summonerName matches
    data = load("allgamedata_in_game.json")
    data["activePlayer"]["riotId"] = "Someone#ZZ"
    assert parse_live_game(data).champion == "Ahri"
    # fallback: only riotIdGameName matches
    data["activePlayer"]["summonerName"] = ""
    assert parse_live_game(data).champion == "Ahri"
    # nobody matches: still in_game, no champion
    data["activePlayer"]["riotIdGameName"] = "Nobody"
    st = parse_live_game(data)
    assert st.phase == "in_game" and st.champion is None


def test_events_dedup_and_classification() -> None:
    full = load("allgamedata_in_game.json")
    # Started mid-game: history is not replayed.
    assert EventTracker().process(copy.deepcopy(full)) == []

    # Tracker sees the game from early on, then later polls.
    early = copy.deepcopy(full)
    early["gameData"]["gameTime"] = 241.0
    early["events"]["Events"] = full["events"]["Events"][:4]  # ids 0..3
    tr = EventTracker()
    first = tr.process(early)
    assert [e.kind for e in first] == ["first_blood", "champion_kill"]
    assert tr.process(early) == []  # nothing new

    second = tr.process(full)       # ids 4..9 are new
    assert [e.event_id for e in second] == [4, 5, 6, 7, 8, 9]
    assert tr.process(full) == []   # de-duplicated

    later = copy.deepcopy(full)
    later["gameData"]["gameTime"] = 926.0
    later["events"]["Events"] += load("events_later.json")["Events"]
    third = tr.process(later)
    assert [e.event_id for e in third] == [10, 11, 12, 13]
    assert tr.process(later) == []

    # classification (mine / my team)
    fb, kill = first
    assert fb.mine and fb.my_team and kill.mine and kill.my_team
    assert kill.detail["victim"] == "EnemyOne" and not kill.detail["assist"]
    enemy_kill, assist_kill, fire, water, turret, multi = second
    assert enemy_kill.kind == "champion_kill" and not enemy_kill.mine and not enemy_kill.my_team
    assert assist_kill.mine and assist_kill.my_team and assist_kill.detail["assist"]
    assert fire.kind == "dragon_kill" and fire.my_team and not fire.mine
    assert fire.detail["dragon"] == "Fire" and fire.detail["stolen"] is False
    assert water.my_team is False and water.detail["dragon"] == "Water"
    assert turret.kind == "turret_killed" and not turret.my_team  # killed by a minion: unknown team
    assert multi.kind == "multikill" and multi.mine and multi.detail["kill_streak"] == 3
    ace, baron, penta, end = third
    assert ace.kind == "ace" and ace.my_team and not ace.mine
    assert baron.kind == "baron_kill" and not baron.my_team
    assert penta.detail["kill_streak"] == 5 and penta.mine
    assert end.kind == "game_end" and end.detail["result"] == "Win" and end.my_team

    # a new game (clock and ids restart) resets the tracker
    new_game = copy.deepcopy(full)
    new_game["gameData"]["gameTime"] = 3.0
    new_game["events"]["Events"] = [{"EventID": 0, "EventName": "GameStart", "EventTime": 0.05},
                                    {"EventID": 1, "EventName": "FirstBlood", "EventTime": 2.0,
                                     "Recipient": "TestPlayer"}]
    assert [e.kind for e in tr.process(new_game)] == ["first_blood"]


class FakeServer:
    """Stands in for http_get_json: serves fixtures by URL, or raises Unavailable."""

    def __init__(self) -> None:
        self.live: dict | None = None
        self.phase: str | None = "None"
        self.session = load("champ_select_session.json")
        self.auths: list = []

    def __call__(self, url, auth=None, timeout=0.5):
        if url == client.LIVE_URL:
            if self.live is None:
                raise Unavailable("ConnectionError")
            return self.live
        self.auths.append(auth)
        if self.phase is None:
            raise Unavailable("ConnectionError")
        if url.endswith("/lol-gameflow/v1/gameflow-phase"):
            return self.phase
        if url.endswith("/lol-champ-select/v1/session"):
            return self.session
        raise Unavailable("HTTP 404")


def test_watcher_flow() -> None:
    server = FakeServer()
    with tempfile.TemporaryDirectory() as tmp:
        lock = pathlib.Path(tmp) / "lockfile"
        w = LeagueWatcher(lockfile_paths=[str(lock)], use_process_fallback=False, fetch=server)
        # League closed: no lockfile, no live API
        w.poll_once()
        assert w.state().phase == "offline" and w.drain_events() == []
        # client running, in lobby
        lock.write_text(f"LeagueClient:1:51234:{FAKE_PASSWORD}:https", "utf-8")
        w.poll_once()
        assert w.state().phase == "lobby"
        assert server.auths[-1] == ("riot", FAKE_PASSWORD)
        # champ select
        server.phase = "ChampSelect"
        w.poll_once()
        st = w.state()
        assert st.phase == "champ_select" and st.champion_key == 103 and st.skin_num == 3
        # game starts: loading screen (LCU says in progress, live API not up yet)
        server.phase = "InProgress"
        w.poll_once()
        assert w.state().phase == "in_game" and w.state().champion is None
        # live API up
        full = load("allgamedata_in_game.json")
        full["gameData"]["gameTime"] = 241.0
        full["events"]["Events"] = full["events"]["Events"][:4]
        server.live = full
        delay = w.poll_once()
        assert delay == w.live_interval
        st = w.state()
        assert st.phase == "in_game" and st.champion == "Ahri"
        assert [e.kind for e in w.drain_events()] == ["first_blood", "champion_kill"]
        assert w.drain_events() == []
        # same data again: no duplicate events
        w.poll_once()
        assert w.drain_events() == []
        # transient live failures keep the in-game state ...
        server.live = None
        for _ in range(w.LIVE_GRACE_FAILS):
            w.poll_once()
            assert w.state().phase == "in_game" and w.state().champion == "Ahri"
        # ... then it falls back to the client
        server.phase = "WaitingForStats"
        w.poll_once()
        w.poll_once()
        assert w.state().phase == "post_game"
        # everything gone
        server.phase = None
        w.poll_once()
        assert w.state().phase == "offline"
        # state() returns a copy
        s1 = w.state()
        s1.phase = "tampered"
        assert w.state().phase != "tampered"


def test_watcher_never_logs_password() -> None:
    class Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.DEBUG)
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(record.getMessage())

    def failing(url, auth=None, timeout=0.5):
        raise Unavailable(f"boom {auth[1] if auth else ''}")

    cap = Capture()
    log = logging.getLogger("rgbsync.league.client")
    old_level = log.level
    log.addHandler(cap)
    log.setLevel(logging.DEBUG)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            lock = pathlib.Path(tmp) / "lockfile"
            lock.write_text(f"LeagueClient:1:51234:{FAKE_PASSWORD}:https", "utf-8")
            w = LeagueWatcher(lockfile_paths=[str(lock)], use_process_fallback=False, fetch=failing)
            w.poll_once()
    finally:
        log.removeHandler(cap)
        log.setLevel(old_level)
    assert cap.lines, "expected debug logging"
    assert all(FAKE_PASSWORD not in line for line in cap.lines), cap.lines


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("league client tests passed")
