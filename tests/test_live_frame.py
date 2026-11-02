"""Pure frame-building tests for tools/experimental/nuphy_live_probe.py. No hardware.

Run: python tests/test_live_frame.py   (plain asserts, no pytest needed)
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "nuphy_live_probe", ROOT / "tools" / "experimental" / "nuphy_live_probe.py")
probe = importlib.util.module_from_spec(_spec)
sys.modules["nuphy_live_probe"] = probe
_spec.loader.exec_module(probe)

HEADER = bytes([0x08, 0x0A, 0x7A, 0x01])


def raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return True
    return False


def test_build_frame_pads_and_header():
    f = probe.build_frame(b"")
    assert len(f) == 382 and f[:4] == HEADER and f[4:] == bytes(378)
    f = probe.build_frame(b"\x01\x02\x03")
    assert len(f) == 382 and f[:4] == HEADER and f[4:7] == b"\x01\x02\x03"
    assert f[7:] == bytes(375)
    assert len(probe.build_frame(bytes(378))) == 382
    assert raises(ValueError, probe.build_frame, bytes(379))


def test_all_ff_and_zero():
    f = probe.build_frame(probe.all_ff_payload())
    assert len(f) == 382 and f[:4] == HEADER and f[4:] == b"\xff" * 378
    z = probe.build_frame(bytes(378))
    assert z[4:] == bytes(378)


def test_solid_interleaved():
    p = probe.solid(255, 0, 0, "interleaved")
    assert len(p) == 378
    for slot in range(126):
        assert p[slot * 3:slot * 3 + 3] == b"\xff\x00\x00"
    p = probe.solid(1, 2, 3, "interleaved")
    assert p == bytes([1, 2, 3]) * 126
    f = probe.build_frame(p)
    assert f[4:7] == bytes([1, 2, 3]) and f[-3:] == bytes([1, 2, 3])


def test_solid_planar():
    p = probe.solid(1, 2, 3, "planar")
    assert len(p) == 378
    assert p[0:126] == bytes([1]) * 126
    assert p[126:252] == bytes([2]) * 126
    assert p[252:378] == bytes([3]) * 126
    red = probe.solid(255, 0, 0, "planar")
    assert red[:126] == b"\xff" * 126 and red[126:] == bytes(252)
    assert red != probe.solid(255, 0, 0, "interleaved")


def test_single_interleaved():
    p = probe.single(1, (255, 255, 255), "interleaved")
    assert len(p) == 378
    assert p[3:6] == b"\xff\xff\xff"
    assert sum(p) == 3 * 255
    f = probe.build_frame(p)
    assert f[4 + 3:4 + 6] == b"\xff\xff\xff" and f[:4] == HEADER and len(f) == 382
    p = probe.single(125, (10, 20, 30), "interleaved")
    assert p[375:378] == bytes([10, 20, 30]) and sum(p) == 60


def test_single_planar():
    p = probe.single(1, (10, 20, 30), "planar")
    assert p[1] == 10 and p[127] == 20 and p[253] == 30
    assert sum(p) == 60 and len(p) == 378
    p = probe.single(125, (1, 2, 3), "planar")
    assert p[125] == 1 and p[251] == 2 and p[377] == 3


def test_column_and_keys():
    p = probe.column(0, (255, 255, 255), "interleaved")
    assert p[:18] == b"\xff" * 18 and p[18:] == bytes(360)
    p = probe.column(20, (7, 8, 9), "interleaved")
    assert p[360:378] == bytes([7, 8, 9]) * 6 and p[:360] == bytes(360)
    assert probe.KEY_SLOTS["Esc"] == 1 and probe.KEY_SLOTS["Right"] == 95
    # slot = col * 6 + row
    assert probe.KEY_SLOTS["Space"] == 5 * 6 + 5
    assert probe.KEY_SLOTS["Enter"] == 13 * 6 + 3


def test_validation():
    assert raises(ValueError, probe.solid, 256, 0, 0, "interleaved")
    assert raises(ValueError, probe.solid, 0, 0, 0, "diagonal")
    assert raises(ValueError, probe.single, 126, (0, 0, 0), "interleaved")
    assert raises(ValueError, probe.single, -1, (0, 0, 0), "planar")
    assert raises(ValueError, probe.single, 0, (0, 0), "planar")
    assert raises(ValueError, probe.column, 21, (0, 0, 0), "planar")


def test_static_steps_frames():
    steps = probe.static_steps()
    assert [s[0] for s in steps] == [1, 2, 3, 4, 5]
    for _n, _title, frame, _look in steps:
        assert len(frame) == 382 and frame[:4] == HEADER
    assert steps[0][2][4:] == b"\xff" * 378
    assert steps[1][2][4:] == bytes(378)
    assert steps[4][2][7:10] == b"\xff\xff\xff" and sum(steps[4][2][4:]) == 765
    # animation budget stays under the cap: 5 static + 150 animation <= 170
    assert 5 + int(probe.ANIM_SECONDS * probe.ANIM_FPS) <= probe.MAX_FRAME_SENDS


def test_send_guards():
    b = probe.Board("NuPhy Halo65", 0x118, None, None, 381)
    # only the 0x81 identity read may ever go out on report 5
    for bad in (bytes([5, 1, 0, 0, 0, 0]), bytes([5, 0x81, 1, 0, 0, 0]),
                bytes([6, 0x81, 0, 0, 0, 0]), bytes([5, 0x81, 0, 0]),
                bytes([5, 0x83, 0xB6, 0, 0, 0])):
        assert raises(RuntimeError, b.send_cmd5, bad), bad.hex()
    assert b.cmd5_sends == 0
    # report 8 frames must have the right header and length
    assert raises(RuntimeError, b.send_frame, bytes(382))
    assert raises(RuntimeError, b.send_frame, probe.FRAME_HEADER)
    assert b.frame_sends == 0


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
