"""Geometry Dash simulator tests: the scripted sender talks to a real listener over
loopback UDP at high speed. No game, no hardware.

Run: python tests/test_gd_sim.py   (plain asserts, no pytest needed)
"""
import pathlib
import socket
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.gd import sim  # noqa: E402
from rgbsync.gd.listener import GDListener  # noqa: E402

EXPECTED = ["level_start", "death", "restart", "death", "restart", "checkpoint",
            "checkpoint", "level_exit", "level_start", "complete"]


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_script_is_consistent() -> None:
    assert [e[1]["t"] for e in sim.TIMELINE_EVENTS] == EXPECTED
    times = [e[0] for e in sim.TIMELINE_EVENTS]
    assert times == sorted(times) and times[-1] < sim.SCRIPT_END
    # level 1: 0.5 s from the death to the restart, i.e. restart inside the burst
    assert sim.TIMELINE_EVENTS[2][0] - sim.TIMELINE_EVENTS[1][0] < 0.6
    assert sim.state_at(-1) is None and sim.state_at(27.6) is None
    assert sim.state_at(sim.TIMELINE_EVENTS[-1][0]) is None      # silent after completion
    st = sim.state_at(8.4)
    assert 30 < st["pct"] < 34 and st["best"] == 40 and not st["practice"]
    assert sim.state_at(20.9)["pct"] > 50 and sim.state_at(22.5)["practice"]
    assert sim.state_at(5.5)["flip"] and not sim.state_at(7.0)["flip"]
    assert {sim.state_at(t)["mode"] for t in (1, 4, 7)} == {"cube", "ship", "wave"}
    assert sim.state_at(sim.TIMELINE_EVENTS[-1][0] - 0.01)["pct"] == 100.0
    for t in (0.0, 3.3, 9.5, 17.2, 25.0, 31.0):
        st = sim.state_at(t)
        assert st is not None
        assert all(0 <= c <= 255 for c in st["bg"] + st["p1"])
        assert 0 <= st["pulse"] <= 16 and 0 <= st["pct"] <= 100   # raw meters, not 0-1
        assert 0 <= st["pulse2"] <= 16 and 0 <= st["pulse3"] <= 16
        assert st["accent"] is None or all(0 <= c <= 255 for c in st["accent"])
    # like the real recording: black backgrounds most of the time, some white flashes
    ts = [i / 30 for i in range(30 * 27)]
    bgs = [tuple(sim.state_at(t)["bg"]) for t in ts]
    assert bgs.count((0, 0, 0)) / len(bgs) > 0.5 and (255, 255, 255) in bgs
    assert max(max(b) for b in bgs if b != (255, 255, 255)) <= 32
    # accent: mostly vivid colors, null for a while (the app falls back to p1)
    accents = [sim.state_at(t)["accent"] for t in ts]
    assert None in accents and sum(a is not None for a in accents) > len(accents) / 2
    # real beat shapes: the raw pulse swings between ~0.6 and ~14 at 2 Hz
    pulses = [sim.state_at(t)["pulse"] for t in ts[:90]]
    assert min(pulses) < 1.0 and max(pulses) > 12.0
    rises = sum(1 for a, b in zip(pulses, pulses[1:]) if a < 7 <= b)
    assert 5 <= rises <= 7, rises          # 3 s at 2 Hz
    assert {sim.state_at(t)["pulse3"] for t in ts[:90]} == {0.0}


def test_sender_through_real_listener() -> None:
    listener = GDListener(port=0)
    assert listener.start()
    seen_events, modes, flips, pcts = [], set(), set(), []
    stop_poll = threading.Event()

    def poll() -> None:
        while not stop_poll.is_set():
            seen_events.extend(e for e in listener.drain_events())
            st = listener.state()
            if st.connected:
                modes.add(st.mode)
                flips.add(st.flip)
                pcts.append(st.pct)
            time.sleep(0.002)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    try:
        sender = sim.FakeGDSender(port=listener.port, speed=25)
        t0 = time.monotonic()
        assert sender.run() is True
        assert time.monotonic() - t0 < 5.0                  # 38.5 s script at 25x
        time.sleep(0.3)
        stop_poll.set()
        poller.join(1)
        seen_events.extend(listener.drain_events())
        assert [e.kind for e in seen_events] == EXPECTED, [e.kind for e in seen_events]
        starts = [e for e in seen_events if e.kind == "level_start"]
        assert starts[0].detail["difficulty"] == "hard" and starts[0].detail["name"] == "Sim Rush"
        assert starts[1].detail["difficulty"] == "normal"
        deaths = [e for e in seen_events if e.kind == "death"]
        assert [d.detail["new_best"] for d in deaths] == [False, True]
        assert deaths[0].detail["pct"] == 34.0 and deaths[1].detail["pct"] == 51.0
        assert seen_events[-1].detail == {"new_best": True}
        assert {"cube", "ship", "wave"} <= modes and True in flips and False in flips
        assert max(pcts) >= 90
        assert listener.received == sender.sent and listener.ignored == 0, \
            (listener.received, sender.sent)
        # after the complete event the game goes quiet: the listener times out
        assert listener.state().pct >= 90
    finally:
        stop_poll.set()
        listener.stop()


def test_sender_stop_and_nobody_listening() -> None:
    # no listener on the port: sending must not raise
    sender = sim.FakeGDSender(port=free_port(), speed=100)
    assert sender.run() is True
    # stop() aborts a slow run quickly
    sender = sim.FakeGDSender(port=free_port(), speed=1)
    result = []
    thread = threading.Thread(target=lambda: result.append(sender.run()))
    thread.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    sender.stop()
    thread.join(2)
    assert not thread.is_alive() and result == [False] and time.monotonic() - t0 < 1.0
    try:
        sim.FakeGDSender(speed=0)
        raise AssertionError("speed 0 accepted")
    except ValueError:
        pass


def test_print_mode_runs() -> None:
    lines: list[str] = []
    assert sim.run_print(speed=40, port=free_port(), every=0.03, out=lines.append)
    text = "\n".join(lines)
    assert "event: level_start" in text and "event: death" in text
    assert "event: checkpoint" in text and "event: complete" in text
    assert "edge=#ffff00" in text and "edge=#00ff00" in text and "edge=-" in text
    assert "ship" in text and "practice" in text and "idle" in text
    assert len(lines) > 10
    # busy port is reported, not raised
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        blocker.bind(("127.0.0.1", 0))
        out: list[str] = []
        assert sim.run_print(speed=40, port=blocker.getsockname()[1], out=out.append) is False
        assert "in use" in out[0]
    finally:
        blocker.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("gd sim tests passed")
