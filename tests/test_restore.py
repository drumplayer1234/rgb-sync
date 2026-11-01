"""Restore tests with fake backends / a fake board. Touches no real hardware.

Run: python tests/test_restore.py   (plain asserts, no pytest needed)
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rgbsync.backends import nuphy  # noqa: E402
from rgbsync.backends.base import LightingBackend  # noqa: E402
from rgbsync.manager import Manager  # noqa: E402
from test_nuphy_backup import FakeBoard, make_backend  # noqa: E402


class FakeBackend(LightingBackend):
    """Records calls; `restore_error` makes restore() raise."""

    def __init__(self, name: str, status: str = "connected",
                 restore_error: Exception | None = None) -> None:
        self.name = name
        self.status = status
        self.restore_error = restore_error
        self.calls: list[str] = []

    def connect(self) -> bool:
        self.calls.append("connect")
        self.status = "connected"
        return True

    def list_devices(self) -> list:
        return []

    def set_color(self, r: int, g: int, b: int) -> None:
        self.calls.append("set_color")

    def close(self) -> None:
        self.calls.append("close")
        self.status = "not connected"

    def restore(self) -> str:
        self.calls.append("restore")
        if self.restore_error:
            raise self.restore_error
        return f"{self.name} restored"


def make_manager(*backends: LightingBackend) -> Manager:
    manager = Manager()
    manager.backends = list(backends)
    return manager


def test_restore_all_skips_disconnected() -> None:
    on = FakeBackend("On")
    off = FakeBackend("Off", status="not found: iCUE not running")
    results = make_manager(on, off).restore_all()
    assert results == {"On": "On restored", "Off": "not connected"}, results
    assert on.calls == ["restore"], on.calls
    assert off.calls == [], off.calls


def test_restore_all_one_failure_does_not_stop_others() -> None:
    first = FakeBackend("First")
    bad = FakeBackend("Bad", restore_error=RuntimeError("boom"))
    last = FakeBackend("Last")
    results = make_manager(first, bad, last).restore_all()
    assert results["First"] == "First restored", results
    assert results["Bad"] == "error: boom", results
    assert results["Last"] == "Last restored", results
    assert last.calls == ["restore"], last.calls
    assert bad.status == "error: boom", bad.status


def test_default_restore_reconnects_without_color() -> None:
    class Plain(FakeBackend):
        restore = LightingBackend.restore  # use the real default

    backend = Plain("Plain")
    backend.restored_text = "back to Plain lighting"
    assert backend.restore() == "back to Plain lighting"
    assert backend.calls == ["close", "connect"], backend.calls
    assert backend.status == "connected"


def test_nuphy_no_backup_is_not_an_error(folder: pathlib.Path) -> None:
    board = FakeBoard()
    backend = make_backend(board)
    backend.status = "connected"
    manager = make_manager(backend)
    results = manager.restore_all()
    assert "nothing to restore" in results["NuPhy"], results
    assert "never changed" in results["NuPhy"], results
    assert "error" not in results["NuPhy"].lower(), results
    assert board.writes == [], "must not write without a backup"
    assert backend.status == "connected" and backend._boards == [board], \
        "NuPhy must stay connected"


def main() -> int:
    plain = [test_restore_all_skips_disconnected,
             test_restore_all_one_failure_does_not_stop_others,
             test_default_restore_reconnects_without_color]
    for test in plain:
        test()
        print(f"ok   {test.__name__}")
    saved = nuphy.BACKUP_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            nuphy.BACKUP_DIR = folder
            test_nuphy_no_backup_is_not_an_error(folder)
            print("ok   test_nuphy_no_backup_is_not_an_error")
    finally:
        nuphy.BACKUP_DIR = saved
    print(f"{len(plain) + 1} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
