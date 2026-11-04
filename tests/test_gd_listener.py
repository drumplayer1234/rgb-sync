"""Geometry Dash listener tests: real UDP on loopback, no game.

Run: python tests/test_gd_listener.py   (plain asserts, no pytest needed)
"""
import json
import pathlib
import socket
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.gd.listener import GDListener, parse_message  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def wait_until(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class Rig:
    """A started listener on a free port plus a sending socket."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.listener = GDListener(port=0, clock=self.clock)
        assert self.listener.start(), self.listener.status
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, msg, raw: bool = False) -> None:
        data = msg if raw else json.dumps(msg).encode()
        self.sock.sendto(data, ("127.0.0.1", self.listener.port))

    def settle(self, accepted: int, ignored: int) -> None:
        li = self.listener
        assert wait_until(lambda: (li.received, li.ignored) == (accepted, ignored)), \
            (li.received, li.ignored, accepted, ignored)

    def close(self) -> None:
        self.sock.close()
        self.listener.stop()


def state_msg(**kw) -> dict:
    msg = {"v": 1, "t": "state", "pct": 12.5, "best": 40.0, "mode": "ship",
           "flip": True, "speed": 1.5, "bg": [10, 20, 30], "p1": [200, 100, 50],
           "pulse": 0.5, "practice": False, "paused": False}
    msg.update(kw)
    return msg


def test_valid_state_and_events() -> None:
    rig = Rig()
    try:
        li = rig.listener
        assert li.status == "listening" and li.port > 0
        assert not li.state().connected
        rig.send({"v": 1, "t": "level_start", "name": "Bloodbath", "stars": 10,
                  "difficulty": "demon", "platformer": False})
        rig.send(state_msg())
        rig.settle(2, 0)
        st = li.state()
        assert st.connected
        assert (st.pct, st.best, st.mode, st.flip, st.speed) == (12.5, 40.0, "ship", True, 1.5)
        assert st.bg == (10, 20, 30) and st.p1 == (200, 100, 50) and st.pulse == 0.5
        assert not st.practice and not st.paused
        assert (st.level_name, st.difficulty, st.stars) == ("Bloodbath", "demon", 10)
        events = li.drain_events()
        assert [e.kind for e in events] == ["level_start"]
        assert events[0].detail["difficulty"] == "demon" and events[0].detail["name"] == "Bloodbath"
        assert li.drain_events() == []                       # delivered once
        rig.send({"v": 1, "t": "death", "pct": 34.2, "new_best": True})
        rig.send({"v": 1, "t": "restart"})
        rig.send({"v": 1, "t": "checkpoint"})
        rig.send({"v": 1, "t": "complete", "new_best": False})
        rig.settle(6, 0)
        events = li.drain_events()
        assert [e.kind for e in events] == ["death", "restart", "checkpoint", "complete"]
        assert events[0].detail == {"pct": 34.2, "new_best": True}
        assert events[3].detail == {"new_best": False}
        # state() hands out copies
        st.pct = 99
        assert li.state().pct == 12.5
        # level_exit: disconnected at once, level info forgotten
        rig.send({"v": 1, "t": "level_exit"})
        rig.settle(7, 0)
        assert [e.kind for e in li.drain_events()] == ["level_exit"]
        st = li.state()
        assert not st.connected and st.level_name == "" and st.pct == 0
    finally:
        rig.close()


def test_bad_input_is_ignored() -> None:
    rig = Rig()
    try:
        li = rig.listener
        rig.send(b"not json at all", raw=True)
        rig.send(b"\xff\xfe\x00 binary", raw=True)
        rig.send(b"[1, 2, 3]", raw=True)
        rig.send(b'"state"', raw=True)
        rig.send(b"", raw=True)
        rig.send({"v": 2, "t": "state", "pct": 50})            # wrong version
        rig.send({"t": "state", "pct": 50})                    # no version
        rig.send({"v": "1", "t": "state", "pct": 50})
        rig.send({"v": True, "t": "state", "pct": 50})
        rig.send({"v": 1, "t": "teleport"})                    # unknown type
        rig.send({"v": 1})                                     # no type
        rig.send({"v": 1, "t": ["state"]})
        rig.settle(0, 12)
        assert not li.state().connected and li.drain_events() == []
        # the thread is still alive and accepts good data afterwards
        rig.send(state_msg())
        rig.settle(1, 12)
        assert li.state().connected and li.state().pct == 12.5
        # parse_message directly
        assert parse_message(b'{"v":1,"t":"restart"}') == ("restart", {"v": 1, "t": "restart"})
        assert parse_message(b'{"v":1,"t":"state","pct":NaN}') is not None
        assert parse_message(b"{") is None
    finally:
        rig.close()


def test_clamping_and_wrong_types() -> None:
    rig = Rig()
    try:
        li = rig.listener
        rig.send(state_msg(pct=250, best=-5, speed=99, pulse=7, bg=[300, -20, 12.6],
                           p1=[0, 255, 256]))
        rig.settle(1, 0)
        st = li.state()
        assert (st.pct, st.best, st.speed, st.pulse) == (100.0, 0.0, 10.0, 1.0)
        assert st.bg == (255, 0, 13) and st.p1 == (0, 255, 255)
        # wrong types / shapes keep the previous value, never raise
        rig.send(state_msg(pct="lots", best=None, mode="hovercraft", flip="yes",
                           bg=[1, 2], p1="red", pulse=float("nan"), speed=True,
                           practice=5, paused=[]))
        rig.settle(2, 0)
        st = li.state()
        assert st.pct == 100.0 and st.mode == "ship" and st.flip is True
        assert st.bg == (255, 0, 13) and st.p1 == (0, 255, 255)
        assert st.pulse == 1.0 and st.speed == 10.0
        assert st.practice is False and st.paused is False
        # missing fields: only what is sent changes
        rig.send({"v": 1, "t": "state", "pct": 3})
        rig.settle(3, 0)
        st = li.state()
        assert st.pct == 3.0 and st.mode == "ship" and st.bg == (255, 0, 13)
        # event details: clamped / defaulted
        rig.send({"v": 1, "t": "level_start", "name": "x" * 500, "stars": 99,
                  "difficulty": "IMPOSSIBLE", "platformer": 1})
        rig.send({"v": 1, "t": "death", "pct": 400})
        rig.send({"v": 1, "t": "death", "pct": "?", "new_best": "yes"})
        rig.settle(6, 0)
        ev = li.drain_events()
        assert ev[0].detail["stars"] == 10 and ev[0].detail["difficulty"] == "unrated"
        assert len(ev[0].detail["name"]) <= 100 and ev[0].detail["platformer"] is True
        assert ev[1].detail == {"pct": 100.0, "new_best": False}
        assert ev[2].detail == {"pct": 0.0, "new_best": False}
        # level_start resets progress for the new attempt
        assert li.state().pct == 0.0 and li.state().difficulty == "unrated"
    finally:
        rig.close()


def test_connected_timeout() -> None:
    rig = Rig()
    try:
        li = rig.listener
        rig.send(state_msg())
        rig.settle(1, 0)
        rig.clock.t = 102.9
        assert li.state().connected
        rig.clock.t = 103.2
        st = li.state()
        assert not st.connected and st.pct == 12.5       # stale data kept, flagged
        rig.send(state_msg(pct=60))                      # arrives at t=103.2
        rig.settle(2, 0)
        assert li.state().connected and li.state().pct == 60
        rig.clock.t = 106.5
        assert not li.state().connected
    finally:
        rig.close()


def test_port_in_use_and_stop() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]
        li = GDListener(port=port)
        assert li.start() is False
        assert li.status == "port in use", li.status
        assert li.state().connected is False and li.drain_events() == []
        li.stop()                                        # safe even though never started
    finally:
        blocker.close()
    # after the blocker is gone the same port works, and stop() is prompt
    li = GDListener(port=port)
    assert li.start() and li.status == "listening"
    t0 = time.monotonic()
    li.stop()
    assert time.monotonic() - t0 < 1.0 and li.status == "stopped"
    # the port is free again
    again = GDListener(port=port)
    assert again.start()
    again.stop()
    # only loopback: the socket is bound to 127.0.0.1
    li = GDListener(port=0)
    assert li.start()
    try:
        assert li._sock.getsockname()[0] == "127.0.0.1"
    finally:
        li.stop()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("gd listener tests passed")
