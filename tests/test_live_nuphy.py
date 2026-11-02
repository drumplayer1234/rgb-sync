"""NuPhy live-lighting tests with fake HID handles. No hardware.

Real Halo65 V1 lighting bytes come from tests/fixtures/halo65_v1_state.json
(read-only); backups go to temp dirs, never the repo root.
Run: python tests/test_live_nuphy.py   (plain asserts, no pytest needed)
"""
import json
import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.backends import nuphy  # noqa: E402
from rgbsync.backends.nuphy import NuPhyBackend, _Board, _backup_path  # noqa: E402
from rgbsync.scene import Scene  # noqa: E402

HEADER = bytes([0x08, 0x0A, 0x7A, 0x01])
RED = 0


def load_real() -> tuple[bytes, bytes]:
    data = json.loads((ROOT / "tests" / "fixtures" / "halo65_v1_state.json")
                      .read_text("utf-8"))
    return bytes.fromhex(data["effects_hex"]), bytes.fromhex(data["colors_hex"])


class FakeHid:
    """Records every feature report sent to it."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.fail = False

    def send_feature_report(self, data) -> int:
        if self.fail:
            raise OSError("unplugged")
        self.sent.append(bytes(data))
        return len(data)

    def close(self) -> None:
        pass


class FakeBoard(_Board):
    """Fake keyboard: report 8 goes to a FakeHid, settings to in-memory bytes."""

    def __init__(self, model: str = "Halo65", live: bool = True) -> None:
        self.hid = FakeHid()
        super().__init__("fake", f"NuPhy {model}", model, "1.1.8", None, None,
                         self.hid if live else None)
        self.effects, self.colors = load_real()
        self.writes: list[str] = []
        self.read_gate: threading.Event | None = None
        self.backup_seen_before_write = True

    def read_effects(self) -> bytes:
        if self.read_gate is not None:
            assert self.read_gate.wait(5), "read gate never opened"
        return self.effects

    def read_colors(self) -> bytes:
        return self.colors

    def write_effects(self, new: bytes, previous: bytes) -> None:
        if not self.writes and not _backup_path(self).exists():
            self.backup_seen_before_write = False
        self.writes.append("effects")
        self.effects = new

    def write_colors(self, colors: bytes) -> None:
        self.writes.append("colors")
        self.colors = colors


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(model: str = "Halo65", live: bool = True):
    board = FakeBoard(model, live)
    backend = NuPhyBackend()
    backend._boards = [board]
    backend._refresh_live_support()
    backend._edge_async = False
    clock = Clock()
    backend._clock = clock
    return backend, board, clock


def slot_rgb(frame: bytes, slot: int) -> bytes:
    return frame[4 + slot * 3:4 + slot * 3 + 3]


def test_frame_bytes() -> None:
    backend, board, _ = make()
    assert backend.supports_live and backend.status == "connected"
    backend.start_live()
    cyan = (0, 255, 255)
    backend.render(Scene(base=(0, 0, 60),
                         keys={"Q": cyan, "W": cyan, "E": cyan, "R": cyan}))
    assert len(board.hid.sent) == 1
    frame = board.hid.sent[0]
    assert len(frame) == 382 and frame[:4] == HEADER
    lit = {8, 14, 20, 26}                       # Q W E R
    for slot in range(126):
        want = bytes(cyan) if slot in lit else bytes([0, 0, 60])
        assert slot_rgb(frame, slot) == want, (slot, slot_rgb(frame, slot))
    # Interleaved: Q (slot 8) is payload bytes 24..26 as R, G, B.
    assert frame[4 + 24:4 + 27] == bytes([0, 255, 255])
    assert nuphy.HALO65_KEY_SLOTS["SPACE"] == 35 and nuphy.HALO65_KEY_SLOTS["ESC"] == 1
    # Unknown key names are ignored, lower case is accepted, values are clamped.
    f = nuphy.build_live_frame((1, 2, 3), {"nope": (9, 9, 9), "esc": (300, -5, 7)},
                               nuphy.HALO65_KEY_SLOTS)
    assert slot_rgb(f, 1) == bytes([255, 0, 7]) and slot_rgb(f, 0) == bytes([1, 2, 3])
    assert board.writes == [], "live frames must never write settings"


def test_skip_and_keepalive() -> None:
    backend, board, clock = make()
    backend.start_live()
    scene = Scene(base=(0, 0, 60), keys={"SPACE": (255, 0, 0)})
    backend.render(scene)
    assert len(board.hid.sent) == 1
    clock.t += 0.5
    backend.render(Scene(base=(0, 0, 60), keys={"SPACE": (255, 0, 0)}))
    assert len(board.hid.sent) == 1, "identical frame must be skipped"
    clock.t += 0.49
    backend.render(scene)
    assert len(board.hid.sent) == 1, "still inside the 1 s keepalive window"
    clock.t += 0.02                              # 1.01 s after the first send
    backend.render(scene)
    assert len(board.hid.sent) == 2, "keepalive after 1 s"
    clock.t += 0.03
    backend.render(Scene(base=(0, 0, 60), keys={"SPACE": (254, 0, 0)}))
    assert len(board.hid.sent) == 3, "changed frame is sent immediately"
    # A failing send is reported, not raised, and retried on the next tick.
    board.hid.fail = True
    clock.t += 0.03
    backend.render(Scene(base=(1, 1, 1)))
    assert backend.status.startswith("error: live frame failed"), backend.status
    board.hid.fail = False
    clock.t += 0.03
    backend.render(Scene(base=(1, 1, 1)))
    assert backend.status == "connected" and len(board.hid.sent) == 4


def test_render_inactive_does_nothing() -> None:
    backend, board, _ = make()
    backend.render(Scene(base=(1, 2, 3)))        # before start_live
    assert board.hid.sent == []
    backend.start_live()
    backend.stop_live()
    backend.render(Scene(base=(1, 2, 3)))        # after stop_live
    assert board.hid.sent == []


def test_edge_once_and_rate_limited() -> None:
    backend, board, clock = make()
    backend.start_live()
    real_e, _ = load_real()
    red = Scene(base=(0, 0, 0), edge=(255, 0, 0))
    backend.render(red)
    assert board.writes == ["effects"], board.writes
    assert board.backup_seen_before_write, "backup must exist before the first write"
    assert _backup_path(board).exists()
    changed = [i for i in range(len(real_e)) if real_e[i] != board.effects[i]]
    assert changed and all(12 <= i < 20 for i in changed), changed
    assert board.effects[12:16] == bytes([3, RED, 4, 0])
    assert board.effects[16:20] == bytes([3, RED, 4, 0])
    # Same edge again, and a different RGB with the same preset: nothing.
    clock.t += 3
    backend.render(red)
    backend.render(Scene(base=(0, 0, 0), edge=(250, 10, 10)))
    assert board.writes == ["effects"]
    # Different preset only 3 s after the last attempt: held back by the limit.
    blue = Scene(base=(0, 0, 0), edge=(0, 0, 255))
    backend.render(blue)
    assert board.writes == ["effects"], "rate limit: max one write per 10 s"
    clock.t += 6.9                               # 9.9 s since the first write
    backend.render(blue)
    assert board.writes == ["effects"]
    # ...but written once 10 s have passed since the last attempt.
    clock.t += 0.2
    backend.render(blue)
    assert board.writes == ["effects", "effects"]
    assert board.effects[13] == 2 and board.effects[17] == 2     # blue
    backend.render(blue)
    clock.t += 60
    backend.render(blue)
    assert board.writes == ["effects", "effects"], "no repeat writes"
    # edge=None never writes.
    backend.render(Scene(base=(0, 0, 0)))
    assert board.writes == ["effects", "effects"]
    # Frames kept flowing the whole time (edge writes do not use report 8).
    assert board.hid.sent and all(f[:4] == HEADER for f in board.hid.sent)


def test_edge_does_not_block_frames() -> None:
    backend, board, clock = make()
    backend._edge_async = True
    backend.start_live()
    board.read_gate = threading.Event()          # worker will block in read_effects
    t0 = time.perf_counter()
    backend.render(Scene(base=(0, 0, 0), edge=(0, 255, 0)))
    for i in range(20):                          # frames while the worker is stuck
        clock.t += 0.03
        backend.render(Scene(base=(0, 0, i + 1)))
    assert time.perf_counter() - t0 < 1.0, "render blocked on the edge write"
    assert len(board.hid.sent) == 21
    thread = backend._edge_thread
    assert thread is not None and thread.is_alive()
    board.read_gate.set()
    thread.join(5)
    assert not thread.is_alive()
    assert board.writes == ["effects"] and board.effects[13] == 1   # green
    clock.t += 0.03
    backend.render(Scene(base=(0, 0, 99)))       # still renders afterwards
    assert len(board.hid.sent) == 22


def test_stop_live_final_frame() -> None:
    # No set_color: the last live base is used.
    backend, board, clock = make()
    backend.start_live()
    backend.render(Scene(base=(0, 0, 60), keys={"Q": (255, 255, 255)}))
    clock.t += 0.03
    backend.stop_live()
    final = board.hid.sent[-1]
    assert len(board.hid.sent) == 2
    assert final[:4] == HEADER and final[4:] == bytes([0, 0, 60]) * 126
    assert len(final) == 382
    backend.render(Scene(base=(9, 9, 9)))        # ignored after stop
    assert len(board.hid.sent) == 2

    # With set_color: that color wins over the last base.
    backend, board, clock = make()
    backend.set_color(10, 20, 30)
    backend.start_live()
    backend.render(Scene(base=(0, 0, 60)))
    backend.stop_live()
    final = board.hid.sent[-1]
    assert final[4:] == bytes([10, 20, 30]) * 126, final[:8]

    # Never rendered, never set: nothing to send.
    backend, board, _ = make()
    backend.start_live()
    backend.stop_live()
    assert board.hid.sent == []


def test_support_detection() -> None:
    backend, _, _ = make("Halo65", live=True)
    assert backend.supports_live is True
    backend, _, _ = make("Halo75", live=True)            # no key map yet
    assert backend.supports_live is False
    assert backend.status.startswith("connected") and "Halo75" in backend.status
    backend, board, _ = make("Halo65", live=False)       # no report 8
    assert backend.supports_live is False
    assert backend.status.startswith("connected") and "live" in backend.status
    # Unsupported board: render does nothing and never raises.
    backend.start_live()
    backend.render(Scene(base=(1, 2, 3), keys={"Q": (1, 1, 1)}))
    assert board.hid.sent == []
    # Closing drops live support.
    backend, _, _ = make()
    backend.close()
    assert backend.supports_live is False


def main() -> int:
    tests = [test_frame_bytes, test_skip_and_keepalive,
             test_render_inactive_does_nothing,
             test_edge_once_and_rate_limited, test_edge_does_not_block_frames,
             test_stop_live_final_frame, test_support_detection]
    saved = nuphy.BACKUP_DIR
    try:
        for test in tests:
            with tempfile.TemporaryDirectory() as tmp:
                nuphy.BACKUP_DIR = pathlib.Path(tmp)  # never touch the real backup
                test()
            print(f"ok   {test.__name__}")
    finally:
        nuphy.BACKUP_DIR = saved
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
