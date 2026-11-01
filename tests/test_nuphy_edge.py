"""NuPhy edge-light (sidelight + halo) tests with a fake board. No hardware.

Uses real Halo65 V1 lighting bytes from tests/fixtures/halo65_v1_state.json
(read-only). All backups are written to temp dirs, never the repo root.
Run: python tests/test_nuphy_edge.py   (plain asserts, no pytest needed)
"""
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rgbsync.backends import nuphy  # noqa: E402
from rgbsync.backends.nuphy import NuPhyBackend, _Board, _nearest_fixed_color  # noqa: E402

RED, GREEN, BLUE, YELLOW, PURPLE, CYAN, WHITE = range(7)


def load_real() -> tuple[bytes, bytes]:
    data = json.loads((ROOT / "tests" / "fixtures" / "halo65_v1_state.json").read_text("utf-8"))
    return bytes.fromhex(data["effects_hex"]), bytes.fromhex(data["colors_hex"])


class FakeBoard(_Board):
    def __init__(self, effects: bytes, colors: bytes) -> None:
        super().__init__("fake", "NuPhy Halo65", "Halo65", "1.1.8", None, None)
        self.effects, self.colors = effects, colors
        self.writes: list[str] = []

    def read_effects(self) -> bytes:
        return self.effects

    def read_colors(self) -> bytes:
        return self.colors

    def write_effects(self, new: bytes, previous: bytes) -> None:
        self.writes.append("effects")
        self.effects = new

    def write_colors(self, colors: bytes) -> None:
        self.writes.append("colors")
        self.colors = colors


def diff(a: bytes, b: bytes) -> list[int]:
    assert len(a) == len(b)
    return [i for i in range(len(a)) if a[i] != b[i]]


def apply(rgb, effects=None, colors=None) -> tuple[FakeBoard, bytes, bytes]:
    real_e, real_c = load_real()
    board = FakeBoard(effects or real_e, colors or real_c)
    before = (board.effects, board.colors)
    backend = NuPhyBackend()
    backend._boards = [board]
    backend.set_color(*rgb)
    assert backend.status == "connected", backend.status
    return board, *before


def test_nearest_colors() -> None:
    assert _nearest_fixed_color(255, 0, 0) == RED
    assert _nearest_fixed_color(0, 255, 0) == GREEN
    assert _nearest_fixed_color(0, 0, 255) == BLUE
    assert _nearest_fixed_color(255, 255, 0) == YELLOW
    assert _nearest_fixed_color(255, 0, 255) == PURPLE
    assert _nearest_fixed_color(0, 255, 255) == CYAN
    assert _nearest_fixed_color(255, 255, 255) == WHITE
    assert _nearest_fixed_color(200, 30, 20) == RED            # dim red
    assert _nearest_fixed_color(0, 0, 60) == BLUE              # dark blue, not black/white
    assert _nearest_fixed_color(10, 0, 40) == BLUE
    assert _nearest_fixed_color(255, 165, 0) == YELLOW         # orange, yellow side
    assert _nearest_fixed_color(255, 69, 0) == RED             # orange-red
    assert _nearest_fixed_color(128, 0, 128) == PURPLE
    assert _nearest_fixed_color(255, 255, 255) == WHITE
    assert _nearest_fixed_color(128, 128, 128) == WHITE        # gray
    assert _nearest_fixed_color(40, 40, 40) == WHITE           # dark gray
    assert _nearest_fixed_color(0, 0, 0) == WHITE              # black: no hue
    assert _nearest_fixed_color(230, 220, 225) == WHITE        # near-white pink
    assert _nearest_fixed_color(180, 190, 200) == WHITE        # slightly blue-gray
    for rgb in ((1, 2, 3), (250, 10, 130), (90, 200, 20), (0, 128, 255)):
        assert _nearest_fixed_color(*rgb) in range(7)         # never 7 (random)


def test_apply_real_bytes_blue() -> None:
    board, old_e, old_c = apply((0, 0, 255))
    new_e = board.effects
    assert old_e[12:20].hex() == "0500040001000400"            # fixture's real state
    # Backlight as before.
    assert new_e[nuphy.I_BACKLIGHT_MODE] == nuphy.MODE_STATIC
    assert new_e[nuphy.I_CUSTOM_FLAG] == 0
    off = nuphy.I_BACKLIGHT_PARAMS
    assert new_e[off] == nuphy.SYNC_SLOT
    # Edge lights: fixed mode, blue (index 2), brightness (4) and speed (0) kept.
    assert new_e[12:16] == bytes([3, BLUE, 4, 0]), new_e[12:16].hex()
    assert new_e[16:20] == bytes([3, BLUE, 4, 0]), new_e[16:20].hex()
    # Nothing else in the effects block changed.
    assert diff(old_e, new_e) == [5, 12, 13, 16, 17, 22], diff(old_e, new_e)
    # Palette: only slot 6 of Static rows 1 and 25 changed, to the exact RGB.
    changed = diff(old_c, board.colors)
    expected = {(row * 7 + nuphy.SYNC_SLOT) * 3 + k
                for row in (1, 25) for k in range(3)}
    assert set(changed) <= expected, changed
    for row in (1, 25):
        base = (row * 7 + nuphy.SYNC_SLOT) * 3
        assert board.colors[base:base + 3] == bytes([0, 0, 255])


def test_brightness_preserved_and_mid_when_zero() -> None:
    real_e, _ = load_real()
    e = bytearray(real_e)
    e[14], e[18] = 2, 3                                        # custom brightness
    e[15], e[19] = 1, 2                                        # custom speed
    board, _, _ = apply((255, 0, 0), bytes(e))
    assert board.effects[12:16] == bytes([3, RED, 2, 1])
    assert board.effects[16:20] == bytes([3, RED, 3, 2])

    e[14] = e[18] = 0
    board, _, _ = apply((255, 0, 0), bytes(e))
    assert board.effects[14] == nuphy.MISC_MID_BRIGHTNESS
    assert board.effects[18] == nuphy.MISC_MID_BRIGHTNESS


def test_toggle_off() -> None:
    saved = nuphy.SYNC_EDGE_LIGHTS
    nuphy.SYNC_EDGE_LIGHTS = False
    try:
        board, old_e, _ = apply((0, 0, 255))
    finally:
        nuphy.SYNC_EDGE_LIGHTS = saved
    assert board.effects[12:20] == old_e[12:20], "edge lights must stay untouched"
    assert board.effects[nuphy.I_BACKLIGHT_MODE] == nuphy.MODE_STATIC


def test_second_apply_is_noop() -> None:
    board, _, _ = apply((0, 0, 255))
    board.writes.clear()
    backend = NuPhyBackend()
    backend._boards = [board]
    backend.set_color(0, 0, 255)
    assert board.writes == [], "same color again must write nothing"


def main() -> int:
    tests = [test_nearest_colors, test_apply_real_bytes_blue,
             test_brightness_preserved_and_mid_when_zero, test_toggle_off,
             test_second_apply_is_noop]
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
