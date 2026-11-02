"""EXPERIMENTAL hardware probe: NuPhy Halo65 V1 live-lighting channel (report 8).

UNVERIFIED. Everything below comes from static analysis of NuPhy Console and has
never been run against a keyboard. Read this whole docstring before using it.

What it tries
-------------
NuPhy Console's "Music" mode seems to stream LED frames as one feature report:

    HidD_SetFeature(handle, buf, 382)   buf = 08 0A 7A 01 + 378-byte payload

The payload is 126 LED slots x 3 bytes. The byte order is NOT verified:

* "interleaved": slot*3 + channel (R, G, B)             <- most likely
* "planar":      R at slot, G at slot+126, B at slot+252

A key's slot is ``col * 6 + row`` (21 columns x 6 rows). Halo65 examples:
Esc=1, Q=8, W=14, A=9, S=15, D=21, Space=35, Enter=81, Up=88, Left=83,
Down=89, Right=95.

What it sends (and only this)
-----------------------------
* Report 5, command 0x81 only (identity read): ``05 81 00 00 00 00 00 00 00``
  (9 bytes, as the Console does; if Windows rejects that length, 6 bytes
  ``05 81 00 00 00 00``), then GetFeature report 5. Expected reply for a
  Halo65: ``05 01 | 02 00 00 00 00 f7``. Report 5 command 0x01 (writes the
  password/PSD) is NEVER sent, no other report-5 command is ever sent, and
  nothing is ever sent on report 6. The code enforces this in ``send_cmd5``.
* Report 8, 382-byte frames, only with ``--yes``: at most 5 single frames
  (steps 1-5) plus a 5 s x 30 fps animation (150 frames), hard cap 170.

Whether a frame is temporary (RAM only) or gets committed to flash is exactly
what this probe is meant to find out, so it measures each send. If sends look
slow (a flash commit would be), step 6 stops the run before streaming.

Steps (``--step N`` runs only one; default runs all in order)
-------------------------------------------------------------
0  (always) discovery + identity read
1  all payload bytes 0xFF (white, layout independent)
2  all zero (lights off)
3  all red, interleaved layout
4  all red, planar layout   (if 3 and 4 differ, the one that is red is right)
5  Esc only, white, interleaved (payload bytes 3..5 = FF)
6  timing gate (median of steps 1-5 > 25 ms or any failure -> stop)
7  5 s sweep of one lit column at 30 fps (only if the gate passed; needs steps
   1-5 timing data from the same run, so ``--step 7`` alone is refused)
8  stop sending; watch whether the keyboard returns to its own lighting

Without ``--yes`` nothing is sent on report 8: only discovery and the identity
read run, and the frames that WOULD be sent are printed. NOTE: the identity read
itself is a send (report 5, command 0x81, which only reads).

Usage:  python tools/experimental/nuphy_live_probe.py [--yes] [--step N]
                                                      [--hold SECONDS] [--any-model]
Close NuPhy Console first, and use the USB cable (not wireless).
"""
from __future__ import annotations

import argparse
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from rgbsync.backends.nuphy import _feature_report_sizes, _group_key  # noqa: E402

# --- discovery constants (same as rgbsync/backends/nuphy.py) -------------------
VENDOR_ID = 0x05AC
PRODUCT_ID = 0x024F
USAGE_PAGE = 0xFF00
USAGE = 0x01
VENDOR_PREFIX = "NuPhy "

# --- report 5: identity read only ---------------------------------------------
CMD_REPORT_ID = 0x05
CMD_IDENTITY = 0x81
IDENTITY_CMD_9 = bytes([0x05, 0x81, 0, 0, 0, 0, 0, 0, 0])
IDENTITY_CMD_6 = bytes([0x05, 0x81, 0, 0, 0, 0])
EXPECTED_PSD = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0xF7])  # Halo65
MAX_CMD5_SENDS = 4

# --- report 8: live frame ------------------------------------------------------
LIVE_REPORT_ID = 0x08
FRAME_LEN = 382                      # incl. report ID
FRAME_HEADER = bytes([0x08, 0x0A, 0x7A, 0x01])
PAYLOAD_LEN = FRAME_LEN - len(FRAME_HEADER)   # 378
SLOTS = 126
COLS, ROWS = 21, 6
LAYOUTS = ("interleaved", "planar")
MAX_FRAME_SENDS = 170

#: A few known Halo65 slots (col * 6 + row), for reference.
KEY_SLOTS = {"Esc": 1, "Q": 8, "W": 14, "A": 9, "S": 15, "D": 21, "Space": 35,
             "Enter": 81, "Up": 88, "Left": 83, "Down": 89, "Right": 95}

# --- timing --------------------------------------------------------------------
GATE_MEDIAN_MS = 25.0                # step 6: median above this -> stop
ABORT_SINGLE_MS = 200.0              # animation: any single send above this -> stop
ANIM_SECONDS = 5.0
ANIM_FPS = 30
OBSERVE_SECONDS = 10
SYNC_DELAY_S = 0.05


# ==============================================================================
# Pure frame helpers (no hardware; unit-tested in tests/test_live_frame.py)
# ==============================================================================
def build_frame(payload: bytes) -> bytes:
    """``08 0A 7A 01`` + payload, zero-padded to 382 bytes total."""
    payload = bytes(payload)
    if len(payload) > PAYLOAD_LEN:
        raise ValueError(f"payload is {len(payload)} bytes, max {PAYLOAD_LEN}")
    return FRAME_HEADER + payload + bytes(PAYLOAD_LEN - len(payload))


def _check_layout(layout: str) -> None:
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}, got {layout!r}")


def _check_rgb(rgb) -> tuple[int, int, int]:
    rgb = tuple(rgb)
    if len(rgb) != 3 or any(not isinstance(c, int) or not 0 <= c <= 255 for c in rgb):
        raise ValueError(f"rgb must be 3 ints 0..255, got {rgb!r}")
    return rgb  # type: ignore[return-value]


def slot_offsets(slot: int, layout: str) -> tuple[int, int, int]:
    """Payload byte offsets of (R, G, B) for an LED slot."""
    _check_layout(layout)
    if not isinstance(slot, int) or not 0 <= slot < SLOTS:
        raise ValueError(f"slot must be 0..{SLOTS - 1}, got {slot!r}")
    if layout == "interleaved":
        return slot * 3, slot * 3 + 1, slot * 3 + 2
    return slot, slot + SLOTS, slot + 2 * SLOTS


def _paint(buf: bytearray, slot: int, rgb: tuple[int, int, int], layout: str) -> None:
    for off, value in zip(slot_offsets(slot, layout), rgb):
        buf[off] = value


def solid(r: int, g: int, b: int, layout: str) -> bytes:
    """378-byte payload with every one of the 126 slots set to (r, g, b)."""
    rgb = _check_rgb((r, g, b))
    buf = bytearray(PAYLOAD_LEN)
    for slot in range(SLOTS):
        _paint(buf, slot, rgb, layout)
    return bytes(buf)


def single(slot: int, rgb, layout: str) -> bytes:
    """378-byte payload with only ``slot`` lit in ``rgb``, everything else off."""
    rgb = _check_rgb(rgb)
    buf = bytearray(PAYLOAD_LEN)
    _paint(buf, slot, rgb, layout)
    return bytes(buf)


def column(col: int, rgb, layout: str) -> bytes:
    """378-byte payload with the 6 slots of keyboard column ``col`` lit."""
    rgb = _check_rgb(rgb)
    if not isinstance(col, int) or not 0 <= col < COLS:
        raise ValueError(f"col must be 0..{COLS - 1}, got {col!r}")
    buf = bytearray(PAYLOAD_LEN)
    for row in range(ROWS):
        _paint(buf, col * ROWS + row, rgb, layout)
    return bytes(buf)


def all_ff_payload() -> bytes:
    """Layout independent 'white': every payload byte 0xFF."""
    return b"\xff" * PAYLOAD_LEN


def hex16(frame: bytes) -> str:
    return " ".join(f"{b:02x}" for b in frame[:16])


def timing_stats(ms: list[float]) -> str:
    if not ms:
        return "no data"
    ordered = sorted(ms)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return (f"n={len(ms)} min={ordered[0]:.2f} median={statistics.median(ms):.2f} "
            f"mean={statistics.fmean(ms):.2f} p95={p95:.2f} max={ordered[-1]:.2f} ms")


# ==============================================================================
# Hardware access
# ==============================================================================
class Board:
    """The opened keyboard: report-5 handle and report-8 handle (may be the same)."""

    def __init__(self, product: str, release: int, dev5, dev8, size8: int | None):
        self.product = product
        self.release = release
        self.dev5 = dev5
        self.dev8 = dev8
        self.size8 = size8            # report 8 payload bytes (without ID)
        self.cmd5_sends = 0
        self.frame_sends = 0

    def close(self) -> None:
        seen = []
        for dev in (self.dev5, self.dev8):
            if dev is None or any(dev is s for s in seen):
                continue
            seen.append(dev)
            try:
                dev.close()
            except Exception as exc:  # noqa: BLE001 - best effort on exit
                print(f"  (close failed: {exc})")
        self.dev5 = self.dev8 = None

    # -- report 5: identity read ONLY -----------------------------------------
    def send_cmd5(self, data: bytes) -> tuple[bool, str]:
        """Send an identity-read command on report 5. Refuses anything else."""
        if (len(data) not in (6, 9) or data[0] != CMD_REPORT_ID
                or data[1] != CMD_IDENTITY or any(data[2:])):
            raise RuntimeError(f"refusing report-5 data {data.hex()}: only the "
                               "0x81 identity read may be sent")
        if self.cmd5_sends >= MAX_CMD5_SENDS:
            raise RuntimeError("report-5 send cap reached")
        self.cmd5_sends += 1
        try:
            ret = self.dev5.send_feature_report(data)
        except (OSError, ValueError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if ret is not None and ret != len(data):
            return False, f"returned {ret}, expected {len(data)}"
        return True, f"returned {ret}"

    # -- report 8: live frames --------------------------------------------------
    def send_frame(self, frame: bytes) -> tuple[bool, float, object, str]:
        """Send one 382-byte frame on report 8 -> (ok, ms, return value, error)."""
        if len(frame) != FRAME_LEN or frame[:4] != FRAME_HEADER:
            raise RuntimeError("refusing malformed live frame")
        if self.frame_sends >= MAX_FRAME_SENDS:
            raise RuntimeError(f"frame send cap ({MAX_FRAME_SENDS}) reached")
        self.frame_sends += 1
        ret: object = None
        err = ""
        t0 = time.perf_counter()
        try:
            ret = self.dev8.send_feature_report(frame)
        except (OSError, ValueError) as exc:
            err = f"{type(exc).__name__}: {exc}"
        ms = (time.perf_counter() - t0) * 1000.0
        if not err and ret is not None and ret != FRAME_LEN:
            err = f"returned {ret}, expected {FRAME_LEN}"
        return not err, ms, ret, err


def _path_str(path) -> str:
    return path.decode("ascii", "replace") if isinstance(path, bytes) else str(path)


def _close_quietly(devs) -> None:
    seen = []
    for dev in devs:
        if dev is None or any(dev is s for s in seen):
            continue
        seen.append(dev)
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass


def discover(hid) -> Board | None:
    """Find the NuPhy vendor collections, parse their descriptors, open 5 and 8."""
    groups: dict[str, list[dict]] = {}
    for info in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        product = info.get("product_string") or ""
        print(f"  enumerated: usage_page=0x{info.get('usage_page', 0):04x} "
              f"usage={info.get('usage')} interface={info.get('interface_number')} "
              f"product={product!r}")
        if info.get("usage_page") == USAGE_PAGE and info.get("usage") == USAGE:
            groups.setdefault(_group_key(info["path"]), []).append(info)

    # Only boards whose name starts with "NuPhy " are ever opened (a genuine
    # Apple keyboard shares the VID/PID and must never be touched).
    candidates = [(k, v) for k, v in groups.items()
                  if (v[0].get("product_string") or "").startswith(VENDOR_PREFIX)]
    if not candidates:
        print("No NuPhy vendor collection found (is it plugged in by USB, "
              "not wireless?).")
        return None
    if len(candidates) > 1:
        print(f"NOTE: {len(candidates)} NuPhy boards found; using the first.")
    key, infos = candidates[0]
    product = infos[0].get("product_string") or ""
    release = infos[0].get("release_number", 0)
    print(f"Using {product!r} (group {key}, release 0x{release:04x}), "
          f"{len(infos)} vendor collection(s):")

    opened: list[tuple[object, dict[int, int]]] = []
    try:
        for info in infos:
            dev = hid.device()
            dev.open_path(info["path"])
            opened.append((dev, {}))
            sizes = _feature_report_sizes(bytes(dev.get_report_descriptor()))
            opened[-1] = (dev, sizes)
            shown = ", ".join(f"report {rid}: {n} B (+ID)"
                              for rid, n in sorted(sizes.items()))
            print(f"    interface={info.get('interface_number')} "
                  f"feature reports [{shown or 'none'}]")
            print(f"      path={_path_str(info['path'])}")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open/parse a collection: {type(exc).__name__}: {exc}")
        print("Close NuPhy Console and any other app using the keyboard, then retry.")
        _close_quietly([d for d, _ in opened])
        return None

    dev5 = next((d for d, s in opened if CMD_REPORT_ID in s), None)
    dev8 = next((d for d, s in opened if LIVE_REPORT_ID in s), None)
    size8 = next((s[LIVE_REPORT_ID] for _, s in opened if LIVE_REPORT_ID in s), None)
    # release collections we do not use (this includes the report-6 data one)
    _close_quietly([d for d, _ in opened if d is not dev5 and d is not dev8])
    if dev5 is None or dev8 is None:
        print(f"Report 5 found: {dev5 is not None}, report 8 found: "
              f"{dev8 is not None}. Unsupported firmware/layout; nothing will be sent.")
        _close_quietly([dev5, dev8])
        return None
    print("  report 5 and report 8 handles are "
          + ("the SAME collection" if dev5 is dev8 else "separate collections"))
    return Board(product, release, dev5, dev8, size8)


def read_identity(board: Board) -> dict:
    """Console-style identity read: SetFeature 05 81.. then GetFeature report 5.

    Tries the 9-byte command first, then 6 bytes if the OS rejects the length.
    """
    attempts = [("9-byte", IDENTITY_CMD_9, (8, 6)), ("6-byte", IDENTITY_CMD_6, (6,))]
    result: dict = {"send_len": None, "get_len": None, "reply": None, "psd": None}
    for name, cmd, get_lens in attempts:
        ok, detail = board.send_cmd5(cmd)
        print(f"  identity: SetFeature report 5, {name} command "
              f"{cmd.hex(' ')} -> {'ok' if ok else 'FAILED'} ({detail})")
        if not ok:
            continue
        time.sleep(SYNC_DELAY_S)
        for glen in get_lens:
            try:
                reply = bytes(board.dev5.get_feature_report(CMD_REPORT_ID, glen))
            except (OSError, ValueError) as exc:
                print(f"  identity: GetFeature report 5, {glen} bytes -> FAILED "
                      f"({type(exc).__name__}: {exc})")
                continue
            print(f"  identity: GetFeature report 5, {glen} bytes -> "
                  f"{len(reply)} bytes: {reply.hex(' ')}")
            result.update(send_len=len(cmd), get_len=glen, reply=reply)
            if len(reply) >= 2 and reply[0] == CMD_REPORT_ID and reply[1] == 0x01:
                result["psd"] = reply[2:8]
            return result
    return result


# ==============================================================================
# Steps
# ==============================================================================
def static_steps() -> list[tuple[int, str, bytes, str]]:
    """(number, title, frame, what to look for) for steps 1-5."""
    return [
        (1, "ALL slots white (every payload byte 0xFF)",
         build_frame(all_ff_payload()),
         "every key (and maybe the side lights) WHITE at full brightness"),
        (2, "ALL zeros (lights off)",
         build_frame(bytes(PAYLOAD_LEN)),
         "keyboard lighting OFF (or its own effect if the frame is ignored)"),
        (3, "ALL red, INTERLEAVED layout",
         build_frame(solid(255, 0, 0, "interleaved")),
         "note the colors: solid red = interleaved works; other colors or a "
         "stripy pattern = it does not"),
        (4, "ALL red, PLANAR layout",
         build_frame(solid(255, 0, 0, "planar")),
         "compare with step 3: whichever of 3 and 4 showed solid RED is the "
         "right layout"),
        (5, "ONLY Esc white (interleaved: payload bytes 3..5 = FF)",
         build_frame(single(KEY_SLOTS["Esc"], (255, 255, 255), "interleaved")),
         "only Esc white. If a different key lights (or several in odd colors), "
         "the layout is planar/different"),
    ]


class Run:
    def __init__(self, board: Board, hold: float) -> None:
        self.board = board
        self.hold = hold
        self.step_ms: dict[int, list[float]] = {}
        self.failed = False
        self.gate_ok: bool | None = None
        self.anim_ms: list[float] = []
        self.anim_fps: float | None = None

    def all_static_ms(self) -> list[float]:
        return [ms for n in range(1, 6) for ms in self.step_ms.get(n, [])]


def run_static(run: Run, number: int, title: str, frame: bytes, look: str) -> bool:
    print(f"\n=== Step {number}: {title}")
    print(f"  frame: {hex16(frame)} ...  length={len(frame)}")
    ok, ms, ret, err = run.board.send_frame(frame)
    run.step_ms.setdefault(number, []).append(ms)
    print(f"  send_feature_report: {ms:.2f} ms, return value = {ret!r}"
          + (f", ERROR: {err}" if err else ""))
    if not ok:
        run.failed = True
        print("  Send FAILED; no further frames will be sent.")
        return False
    print(f"  NOW SHOWING: {title}")
    print(f"  LOOK FOR: {look}")
    print(f"  (holding {run.hold:g} s; the frame is not re-sent during the hold)")
    time.sleep(run.hold)
    return True


def evaluate_gate(run: Run) -> tuple[bool, str]:
    times = run.all_static_ms()
    if run.failed:
        return False, "a send failed"
    if not times:
        return False, "no timing data (run steps 1-5 first, in the same run)"
    med = statistics.median(times)
    if med > GATE_MEDIAN_MS:
        return False, (f"median send time {med:.1f} ms > {GATE_MEDIAN_MS:g} ms: "
                       "frames look slow (possible flash commit)")
    return True, f"median send time {med:.2f} ms <= {GATE_MEDIAN_MS:g} ms, no failures"


def run_gate(run: Run) -> None:
    print("\n=== Step 6: timing gate")
    ok, reason = evaluate_gate(run)
    run.gate_ok = ok
    if ok:
        print(f"  GATE PASSED: {reason}")
    else:
        print(f"  GATE FAILED: {reason}")
        print("  Frames look slow or a send failed (possible flash commit). "
              "STOPPING: will NOT stream the animation.")


def run_animation(run: Run) -> None:
    print("\n=== Step 7: 5 s sweep, one lit column left to right, 30 fps, interleaved")
    ok, reason = evaluate_gate(run)
    if not ok:
        run.gate_ok = False
        print(f"  SKIPPED: gate not passed ({reason}). Not streaming.")
        return
    total = int(ANIM_SECONDS * ANIM_FPS)
    period = 1.0 / ANIM_FPS
    print(f"  {total} frames. NOW SHOWING: a white column moving across the keyboard.")
    start = time.perf_counter()
    sent = 0
    for i in range(total):
        delay = start + i * period - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        col = i * COLS // total
        frame = build_frame(column(col, (255, 255, 255), "interleaved"))
        ok, ms, ret, err = run.board.send_frame(frame)
        sent += 1
        run.anim_ms.append(ms)
        print(f"  frame {i:3d} col {col:2d}: {ms:6.2f} ms ret={ret!r}"
              + (f" ERROR: {err}" if err else ""))
        if not ok:
            run.failed = True
            print("  Send FAILED; stopping the animation.")
            break
        if ms > ABORT_SINGLE_MS:
            print(f"  A single send took {ms:.0f} ms (> {ABORT_SINGLE_MS:g} ms); "
                  "stopping the animation.")
            break
    elapsed = time.perf_counter() - start
    run.anim_fps = sent / elapsed if elapsed > 0 else 0.0
    print(f"  sent {sent} of {total} frames in {elapsed:.2f} s = "
          f"{run.anim_fps:.1f} fps achieved (target {ANIM_FPS})")
    print(f"  send time: {timing_stats(run.anim_ms)}")


def run_observe() -> None:
    print("\n=== Step 8: stop sending")
    print("Stopped sending. Watch whether the keyboard returns to its normal "
          "lighting by itself; note how many seconds.")
    for left in range(OBSERVE_SECONDS, 0, -1):
        print(f"  {left}...", end="", flush=True)
        time.sleep(1)
    print(" done")


def print_dry_run(steps: list[int]) -> None:
    print("\n--yes NOT given: nothing will be sent on report 8. "
          "Frames that WOULD be sent:")
    table = {n: (title, frame) for n, title, frame, _ in static_steps()}
    for n in steps:
        if n in table:
            title, frame = table[n]
            print(f"  step {n}: {title}\n          {hex16(frame)} ...  "
                  f"length={len(frame)}")
        elif n == 6:
            print("  step 6: timing gate (no send)")
        elif n == 7:
            sample = build_frame(column(0, (255, 255, 255), "interleaved"))
            print(f"  step 7: {int(ANIM_SECONDS * ANIM_FPS)} frames at {ANIM_FPS} "
                  f"fps, a white column sweeping (only if the gate passes); "
                  f"first frame:\n          {hex16(sample)} ...  "
                  f"length={len(sample)}")
        elif n == 8:
            print("  step 8: stop sending and watch (no send)")


def print_summary(run: Run) -> None:
    print("\n=== Summary")
    for n in sorted(run.step_ms):
        print(f"  step {n}: {timing_stats(run.step_ms[n])}")
    static = run.all_static_ms()
    if static:
        print(f"  steps 1-5 overall: {timing_stats(static)}")
    if run.gate_ok is not None:
        print(f"  gate: {'passed' if run.gate_ok else 'FAILED'}")
    if run.anim_ms:
        print(f"  step 7 animation: {timing_stats(run.anim_ms)}; "
              f"achieved {run.anim_fps:.1f} fps")
    print(f"  report-8 frames sent: {run.board.frame_sends}; "
          f"report-5 identity sends: {run.board.cmd5_sends}")
    print("  failures: " + ("YES" if run.failed else "none"))
    print("To check it isn't saved: unplug the keyboard, plug it back in, and see "
          "if it shows your normal lighting.")


# ==============================================================================
# main
# ==============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--step", type=int, choices=range(1, 9), metavar="N",
                   help="run only step N (1-8); default runs all in order")
    p.add_argument("--yes", action="store_true",
                   help="REQUIRED to send anything on report 8")
    p.add_argument("--hold", type=float, default=4.0,
                   help="seconds to hold each static step (default 4)")
    p.add_argument("--any-model", action="store_true",
                   help="continue even if the identity (PSD) is not the Halo65's")
    args = p.parse_args(argv)
    if not 0 <= args.hold <= 60:
        p.error("--hold must be between 0 and 60")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    steps = [args.step] if args.step else list(range(1, 9))

    try:
        import hid
    except Exception as exc:  # noqa: BLE001
        print(f"hidapi unavailable: {exc}")
        return 2

    board: Board | None = None
    run: Run | None = None
    try:
        print("=== Step 0: discovery + identity read")
        board = discover(hid)
        if board is None:
            return 2
        if board.size8 != FRAME_LEN - 1:
            print(f"Report 8 payload is {board.size8} bytes in the descriptor, "
                  f"expected {FRAME_LEN - 1} (382 incl. ID). Not sending anything "
                  "on report 8.")
            return 2

        ident = read_identity(board)
        psd = ident["psd"]
        if ident["reply"] is None:
            print("Identity read FAILED (no reply).")
        elif psd is None:
            print(f"Identity reply not understood: {ident['reply'].hex(' ')}")
        else:
            print(f"  identity read worked with: SetFeature {ident['send_len']} B, "
                  f"GetFeature {ident['get_len']} B")
        print(f"  PSD = {psd.hex(' ') if psd else 'unknown'} "
              f"(expected {EXPECTED_PSD.hex(' ')} for Halo65)")
        if psd != EXPECTED_PSD:
            if not args.any_model:
                print("PSD does not match a Halo65. STOPPING "
                      "(use --any-model to override).")
                return 2
            print("PSD mismatch ignored because of --any-model.")

        if not args.yes:
            print_dry_run(steps)
            print("\nNothing was sent on report 8. Re-run with --yes to send.")
            return 0

        run = Run(board, args.hold)
        for n, title, frame, look in static_steps():
            if n not in steps:
                continue
            if run.failed:
                print(f"\n=== Step {n}: skipped (an earlier send failed)")
                continue
            run_static(run, n, title, frame, look)
        if 6 in steps:
            run_gate(run)
        if 7 in steps:
            run_animation(run)
        if 8 in steps:
            run_observe()
        print_summary(run)
        return 3 if run.failed or run.gate_ok is False else 0
    except KeyboardInterrupt:
        print("\nInterrupted. No further sends.")
        if run is not None:
            print_summary(run)
        return 130
    finally:
        if board is not None:
            board.close()


if __name__ == "__main__":
    sys.exit(main())
