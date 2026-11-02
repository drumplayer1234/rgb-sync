"""LiveEngine tests with fake backends. No hardware.

Run: python tests/test_live_engine.py   (plain asserts, no pytest needed)
"""
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.backends.base import LightingBackend  # noqa: E402
from rgbsync.live import LiveEngine  # noqa: E402
from rgbsync.manager import Manager  # noqa: E402
from rgbsync.scene import Scene  # noqa: E402


class FakeBackend(LightingBackend):
    supports_live = True

    def __init__(self, name: str, explode: bool = False) -> None:
        self.name = name
        self.status = "connected"
        self.explode = explode
        self.events: list[str] = []
        self.scenes: list[Scene] = []
        self.render_threads: set[str] = set()

    def connect(self) -> bool:
        return True

    def list_devices(self):
        return []

    def set_color(self, r, g, b) -> None:
        pass

    def close(self) -> None:
        pass

    def start_live(self) -> None:
        self.events.append("start")

    def render(self, scene: Scene) -> None:
        self.events.append("render")
        self.scenes.append(scene)
        self.render_threads.add(threading.current_thread().name)
        if self.explode:
            raise RuntimeError("boom")

    def stop_live(self) -> None:
        self.events.append("stop")


class NoLive(FakeBackend):
    supports_live = False


def manager_with(*backends) -> Manager:
    manager = Manager()
    manager.backends = list(backends)
    return manager


def test_manager_live_backends() -> None:
    a, b = FakeBackend("a"), NoLive("b")
    assert manager_with(a, b).live_backends() == [a]


def test_renders_at_fps() -> None:
    a, none = FakeBackend("a"), NoLive("skipped")
    engine = LiveEngine(manager_with(a, none), fps=50)
    assert not engine.running
    engine.start(lambda now: Scene(base=(1, 2, 3)))
    assert engine.running
    time.sleep(0.6)
    stats = engine.stats
    engine.stop()
    assert not engine.running
    renders = a.events.count("render")
    assert 18 <= renders <= 36, renders              # ~30 expected at 50 fps / 0.6 s
    assert a.events[0] == "start" and a.events[-1] == "stop"
    assert a.events.count("start") == 1 and a.events.count("stop") == 1
    assert none.events == [], "backend without supports_live must be untouched"
    assert 30 <= stats["fps"] <= 70, stats
    assert set(stats["render_ms"]) == {"a"} and stats["render_ms"]["a"] >= 0
    assert stats["frames"] >= 18
    assert a.render_threads == {"live-engine"}
    time.sleep(0.1)
    assert a.events.count("render") == renders, "no renders after stop()"
    engine.stop()                                    # idempotent
    assert a.events.count("stop") == 1


def test_raising_backend_does_not_stop_others() -> None:
    bad, good = FakeBackend("bad", explode=True), FakeBackend("good")
    engine = LiveEngine(manager_with(bad, good), fps=100)
    engine.start(lambda now: Scene(base=(0, 0, 0)))
    time.sleep(0.3)
    assert engine.running
    engine.stop()
    assert bad.events.count("render") >= 5 and good.events.count("render") >= 5
    assert good.events[-1] == "stop" and bad.events[-1] == "stop"
    # A source that raises does not kill the loop either.
    calls = []

    def source(now):
        calls.append(now)
        if len(calls) % 2:
            raise ValueError("bad source")
        return Scene(base=(0, 0, 0))

    good2 = FakeBackend("good2")
    engine = LiveEngine(manager_with(good2), fps=100)
    engine.start(source)
    time.sleep(0.3)
    stats = engine.stats
    engine.stop()
    assert good2.events.count("render") >= 5 and stats["source_errors"] >= 5


def test_source_none_renders_nothing() -> None:
    a = FakeBackend("a")
    engine = LiveEngine(manager_with(a), fps=100)
    engine.start(lambda now: None)
    time.sleep(0.2)
    assert engine.running and engine.stats["fps"] > 20
    engine.stop()
    assert a.events == ["start", "stop"], a.events


def test_source_gets_time_and_can_be_swapped() -> None:
    a = FakeBackend("a")
    engine = LiveEngine(manager_with(a), fps=100)
    seen: list[float] = []
    engine.start(lambda now: seen.append(now) or Scene(base=(1, 1, 1)))
    time.sleep(0.15)
    engine.start(lambda now: Scene(base=(9, 9, 9)))   # swap while running
    time.sleep(0.15)
    engine.stop()
    assert len(seen) >= 3 and all(b >= a_ for a_, b in zip(seen, seen[1:]))
    assert a.events.count("start") == 1, "swap must not restart backends"
    assert a.scenes[0].base == (1, 1, 1) and a.scenes[-1].base == (9, 9, 9)


def test_start_live_failure_skips_backend() -> None:
    class BadStart(FakeBackend):
        def start_live(self) -> None:
            raise RuntimeError("no")

    bad, good = BadStart("bad"), FakeBackend("good")
    engine = LiveEngine(manager_with(bad, good), fps=100)
    engine.start(lambda now: Scene(base=(0, 0, 0)))
    time.sleep(0.1)
    engine.stop()
    assert bad.events == [] and good.events.count("render") >= 3


def main() -> int:
    tests = [test_manager_live_backends, test_renders_at_fps,
             test_raising_backend_does_not_stop_others,
             test_source_none_renders_nothing,
             test_source_gets_time_and_can_be_swapped,
             test_start_live_failure_skips_backend]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
