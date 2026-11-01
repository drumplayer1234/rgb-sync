"""NuPhy backup/restore tests with a fake board. Touches no real hardware.

Run: python tests/test_nuphy_backup.py   (plain asserts, no pytest needed)
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rgbsync.backends import nuphy  # noqa: E402
from rgbsync.backends.nuphy import NuPhyBackend, _Board  # noqa: E402


def make_effects() -> bytes:
    """Realistic 123-byte effects block: 03 03 header ... 5a a5 end."""
    block = bytearray(nuphy.EFFECTS_LEN)
    block[:2] = nuphy.HEADER
    block[nuphy.I_DEBOUNCE] = 5
    block[nuphy.I_BACKLIGHT_MODE] = 3            # Rainbow, so _apply changes it
    block[nuphy.I_SIDELIGHT:nuphy.I_SIDELIGHT + 4] = bytes([3, 1, 4, 2])
    block[nuphy.I_HALO:nuphy.I_HALO + 4] = bytes([3, 2, 4, 2])
    for i in range(nuphy.BACKLIGHT_PARAMS_COUNT):
        block[nuphy.I_BACKLIGHT_PARAMS + 2 * i:nuphy.I_BACKLIGHT_PARAMS + 2 * i + 2] \
            = bytes([i % 7, 0x22])
    block[-2:] = nuphy.END_MARK
    return bytes(block)


def make_colors() -> bytes:
    table = bytes((i * 37 + 11) % 256 for i in range(nuphy.TABLE_LEN))
    return table + bytes(range(nuphy.COLORS_LEN - nuphy.TABLE_LEN))


class FakeBoard(_Board):
    """In-memory keyboard: reads return stored bytes, writes are recorded."""

    def __init__(self) -> None:
        super().__init__("fake", "NuPhy Halo75", "Halo75", "1.1.8", None, None)
        self.effects = make_effects()
        self.colors = make_colors()
        self.writes: list[tuple] = []
        self.on_write = None  # callback run before each write

    def read_effects(self) -> bytes:
        return self.effects

    def read_colors(self) -> bytes:
        return self.colors

    def write_effects(self, new: bytes, previous: bytes) -> None:
        if self.on_write:
            self.on_write()
        self.writes.append(("effects", new, previous))
        self.effects = new

    def write_colors(self, colors: bytes) -> None:
        if self.on_write:
            self.on_write()
        self.writes.append(("colors", colors))
        self.colors = colors


def make_backend(board: FakeBoard) -> NuPhyBackend:
    backend = NuPhyBackend()
    backend._boards = [board]
    return backend


def backup_file(folder: pathlib.Path) -> pathlib.Path:
    return folder / "nuphy_backup_Halo75.json"


def test_backup_created_before_first_write(folder: pathlib.Path) -> None:
    board = FakeBoard()
    original_effects, original_colors = board.effects, board.colors
    seen = []

    def check() -> None:
        seen.append(backup_file(folder).exists())

    board.on_write = check
    backend = make_backend(board)
    backend.set_color(10, 20, 30)
    assert backend.status == "connected", backend.status
    assert board.writes, "expected writes to happen"
    assert seen and all(seen), "backup must exist before every write"
    data = json.loads(backup_file(folder).read_text(encoding="utf-8"))
    assert set(data) == {"product", "model", "firmware", "saved_at",
                         "effects_hex", "colors_hex"}, sorted(data)
    assert data["product"] == "NuPhy Halo75" and data["model"] == "Halo75"
    assert data["firmware"] == "1.1.8" and data["saved_at"]
    assert bytes.fromhex(data["effects_hex"]) == original_effects
    assert bytes.fromhex(data["colors_hex"]) == original_colors
    assert board.effects != original_effects, "set_color should change the board"


def test_existing_backup_not_overwritten(folder: pathlib.Path) -> None:
    board = FakeBoard()
    path = backup_file(folder)
    path.write_text("ORIGINAL USER SETUP", encoding="utf-8")
    make_backend(board).set_color(1, 2, 3)
    assert board.writes, "writes should still happen"
    assert path.read_text(encoding="utf-8") == "ORIGINAL USER SETUP"
    assert not list(folder.glob("*.tmp")), "no temp files left behind"


def test_backup_failure_blocks_writes(folder: pathlib.Path) -> None:
    board = FakeBoard()
    saved = nuphy.BACKUP_DIR
    nuphy.BACKUP_DIR = folder / "does" / "not" / "exist"
    try:
        backend = make_backend(board)
        backend.set_color(9, 9, 9)
    finally:
        nuphy.BACKUP_DIR = saved
    assert board.writes == [], "must not write when backup fails"
    assert backend.status.startswith("error"), backend.status


def test_restore_writes_saved_bytes(folder: pathlib.Path) -> None:
    board = FakeBoard()
    original_effects, original_colors = board.effects, board.colors
    backend = make_backend(board)
    backend.set_color(200, 100, 50)
    assert board.effects != original_effects
    changed_effects = board.effects
    board.writes.clear()

    result = backend.restore_backup()
    assert "restored" in result and "Halo75" in result, result
    assert [w[0] for w in board.writes] == ["colors", "effects"], board.writes
    assert board.writes[0][1] == original_colors
    assert board.writes[1][1] == original_effects
    assert board.writes[1][2] == changed_effects, "previous = current effects"
    assert board.effects == original_effects and board.colors == original_colors

    # Invalid backup (wrong effects length) -> nothing is written.
    path = backup_file(folder)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["effects_hex"] = data["effects_hex"][:-2]
    path.write_text(json.dumps(data), encoding="utf-8")
    board.writes.clear()
    result = make_backend(board).restore_backup()
    assert "invalid" in result and board.writes == [], result

    # Bad end marker -> rejected too.
    data["effects_hex"] = original_effects[:-2].hex() + "0000"
    path.write_text(json.dumps(data), encoding="utf-8")
    result = make_backend(board).restore_backup()
    assert "invalid" in result and board.writes == [], result

    # No backup file -> nothing written.
    path.unlink()
    result = make_backend(board).restore_backup()
    assert "no backup" in result and board.writes == [], result


def main() -> int:
    tests = [test_backup_created_before_first_write,
             test_existing_backup_not_overwritten,
             test_backup_failure_blocks_writes,
             test_restore_writes_saved_bytes]
    saved = nuphy.BACKUP_DIR
    try:
        for test in tests:
            with tempfile.TemporaryDirectory() as tmp:
                folder = pathlib.Path(tmp)
                nuphy.BACKUP_DIR = folder
                test(folder)
            print(f"ok   {test.__name__}")
    finally:
        nuphy.BACKUP_DIR = saved
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
