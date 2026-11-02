"""NuPhy backend (Halo65 / Halo75 / Halo96, V1 firmware) over raw USB HID.

Protocol source and credit
--------------------------
The HID protocol below is a Python port of the reverse-engineering work in
**mishamyrt/nuga-lib** (https://github.com/mishamyrt/nuga-lib, v0.9.3,
MIT License, Copyright (c) 2024 Mikhael Khrustik). Files ported:

* ``hid/device.go``           - device discovery (VID 0x05AC / PID 0x024F,
  usage page 0xFF00 / usage 0x01, product string "NuPhy <model>"), feature
  report transport, 50 ms sync delay, 5 retries.
* ``hid/deviceinfo.go``       - "NuPhy " product-name prefix check.
* ``features/light/feature.go`` / ``constants.go`` - read/write commands and
  packet layouts for the lighting parameters ("effects") and the color palette.
* ``features/light/effect.go`` / ``domain.go`` - byte layout of the effects
  block and the backlight/halo/sidelight mode codes.
* ``docs/protocol.md``        - protocol notes.
The Nuga.app source (mishamyrt/nuga-app) was consulted only to learn that the
palette row index equals the mode code and that Win rows are 0-23, Mac 24-47.

License: nuga-lib is MIT licensed; its copyright notice and full license text are
reproduced in THIRD_PARTY_NOTICES.md at the repository root, as the license
requires.

Differences from nuga-lib (deliberate)
--------------------------------------
* On Windows each vendor-defined top-level HID collection is a separate
  device path: feature report 5 (6 bytes, command) lives in one collection and
  feature report 6 (1032 bytes, data) in another. nuga-lib (written for
  macOS/Linux) opens a single path. We pick the collections by parsing the HID
  report descriptors, and fall back to a single handle when one path exposes
  both reports.
* nuga-lib re-serializes the whole effects block from a parsed model, which
  would overwrite bytes it does not understand (this firmware reports
  different values in the tail and in one header byte than nuga-lib's
  constants). We instead do a byte-level read-modify-write that changes only
  the bytes we mean to change and echoes everything else back verbatim.

Protocol summary (all feature reports, report ID first)
-------------------------------------------------------
* Command (report 5, 6 bytes):  ``05 <cmd> <sub> 00 00 00``
  - ``05 83 b6 00 00 00`` read lighting parameters ("effects")
  - ``05 88 b8 00 00 00`` read backlight color palette
  After sending, wait 50 ms, then GET_FEATURE report 6 (1032 bytes max). The
  first payload byte after the report ID echoes ``<cmd>`` (0x83 / 0x88).
* Effects response (report 6 minus ID): ``83 b6 00*5 | 00*6 5a a5 |``
  123 effect bytes at offset 15, then a second identical copy. Effect block:
  ``[0:2]=03 03 [2]=debounce [4]=1 if custom mode [5]=backlight mode code
  [6:12]=misc header [12:16]=sidelight(mode,color,brightness,speed)
  [16:20]=halo(...) [20:22]=ff ff [22:80]=29 x (color, speed*16+brightness)
  indexed by mode code-1, [80:123]=suffix ending 5a a5``.
* Palette response: payload ``[7:7+1024]`` = 48 rows x 7 colors x RGB (1008
  bytes; rows 0-23 Windows, 24-47 Mac, row index = mode code) + 16 tail bytes.
* Write effects (report 6, 1032 bytes): ``06 03 b6 00*11 5a a5`` (16) +
  new effects (123) + previous effects (123) + 770 zero bytes.
* Write palette (report 6, 1032 bytes): ``06 08 b8 00 40 00 00 00`` (8) +
  palette payload (1024).

Arbitrary RGB: the main backlight in Static mode (code 1) shows palette slot
``color`` (0-6) of its palette row; slot 7 = random. The palette is editable
(this is what Nuga.app's color picker does), so ``set_color`` writes the exact
(r, g, b) into one dedicated slot of the Static row and selects it. Nothing is
rounded to a fixed palette.

Edge lights (sidelight + halo) are different: they have NO editable palette.
Their color byte (effects block [13] / [17]) is an index 0-6 into a fixed list
hard-coded in the Nuga UI (red, green, blue, yellow, purple, cyan, white; 7 =
random is rejected for these lights by nuga-lib). Nuga's color editor only
exists for the backlight. So ``set_color`` switches both edge lights to their
static mode and picks the *nearest* of those 7 colors (``_nearest_fixed_color``).

Live lighting (real time, not saved)
------------------------------------
Besides the saved settings above, the key backlight accepts a 30 fps stream:
HID **feature report 8** on the vendor collection whose descriptor declares
report 8 with 381 payload bytes. Frame = ``08 0A 7A 01`` + 378 bytes, i.e. 126
LED slots x (R, G, B) interleaved (``slot = column * 6 + row``). Frames go to
RAM only (nothing is written to flash, ~6 ms per send) and only the key
backlight reacts; side lights and halo are not part of the frame, so
``Scene.edge`` goes through the slow saved-settings path instead (see
``_apply_edge``). Verified on a Halo65 V1, firmware 1.1.8; slot maps for other
models are not known yet (``KEY_SLOTS_BY_MODEL``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import colorsys
import math
from datetime import datetime
from pathlib import Path

from .base import Device, LightingBackend
from ..scene import RGB, Scene

log = logging.getLogger(__name__)

try:
    import hid
    _IMPORT_ERROR: str | None = None
except Exception as exc:  # package missing or hidapi DLL cannot load
    _IMPORT_ERROR = str(exc)

# --- discovery (nuga-lib hid/device.go, hid/deviceinfo.go) ------------------
VENDOR_ID = 0x05AC           # Apple's ID, spoofed by NuPhy V1 boards
PRODUCT_ID = 0x024F
USAGE_PAGE = 0xFF00
USAGE = 0x01
VENDOR_PREFIX = "NuPhy "     # real Apple keyboards never match this
SUPPORTED_MODELS = ("Halo65", "Halo75", "Halo96")

# --- transport (hid/device.go) ----------------------------------------------
CMD_REPORT_ID = 0x05
DATA_REPORT_ID = 0x06
DATA_REPORT_LEN = 1032       # report 6 incl. ID byte (1031-byte feature item)
REQUEST_RETRIES = 5
SYNC_DELAY_S = 0.05

# --- commands (features/light/constants.go) ---------------------------------
CMD_GET_PARAMS = bytes([0x05, 0x83, 0xB6, 0x00, 0x00, 0x00])
CMD_GET_COLORS = bytes([0x05, 0x88, 0xB8, 0x00, 0x00, 0x00])
CMD_SET_PARAMS = bytes([0x06, 0x03, 0xB6, 0x00, 0x00, 0x00, 0x00, 0x00,
                        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x5A, 0xA5])
CMD_SET_COLORS = bytes([0x06, 0x08, 0xB8, 0x00, 0x40, 0x00, 0x00, 0x00])

EFFECTS_OFFSET = 15          # in the response with the report ID removed
EFFECTS_LEN = 123
COLORS_OFFSET = 7
COLORS_LEN = 1024            # 48 * 7 * 3 = 1008 table bytes + 16 tail bytes
MODES_COUNT = 48
MODES_PER_OS = MODES_COUNT // 2
COLORS_PER_MODE = 7
TABLE_LEN = MODES_COUNT * COLORS_PER_MODE * 3

# --- effects block layout (features/light/effect.go) ------------------------
HEADER = bytes([0x03, 0x03])
END_MARK = bytes([0x5A, 0xA5])
I_DEBOUNCE = 2
I_CUSTOM_FLAG = 4
I_BACKLIGHT_MODE = 5
I_SIDELIGHT = 12             # mode, color, brightness, speed
I_HALO = 16                  # mode, color, brightness, speed
I_BACKLIGHT_PARAMS = 22      # 29 x (color, speed*16 + brightness), by code-1
BACKLIGHT_PARAMS_COUNT = 29

#: Also color the edge lights (sidelight + halo) in ``set_color``. They can only
#: show 7 preset colors, so the nearest one is used. Set False to leave them alone.
SYNC_EDGE_LIGHTS = True

MODE_STATIC = 1              # features/light/domain.go BacklightStatic
SIDELIGHT_FIXED = 3          # domain.go SidelightFixed ("Fixed on")
HALO_FIXED = 3               # domain.go HalolightFixed ("Fixed")
#: Misc-light brightness is 0-4 (effect-misc.go SetBrightness); used only when
#: the current value is 0.
MISC_MID_BRIGHTNESS = 2
#: Fixed color list of sidelight/halo (Nuga UI ``defaultColors``); the index is
#: the color byte. Index 7 (random) is never used for these lights.
FIXED_COLORS = (
    ("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255)),
    ("yellow", (255, 255, 0)), ("purple", (255, 0, 255)),
    ("cyan", (0, 255, 255)), ("white", (255, 255, 255)),
)
#: Palette slot rewritten with the requested color (the "white" swatch of the
#: Static row; slot 7 would mean random and must never be used).
SYNC_SLOT = 6
#: Brightness/speed byte used only when the Static entry is 0 (lights dark).
#: Both nibbles equal because nuga-lib's parser and serializer disagree on the
#: nibble order; 0x22 is mid brightness either way.
MID_VALUE = 0x22

#: Folder holding ``nuphy_backup_<model>.json`` (the app root, next to main.py).
BACKUP_DIR = Path(__file__).resolve().parents[2]
BACKUP_PREFIX = "nuphy_backup_"

# --- live frames (feature report 8) -----------------------------------------
LIVE_REPORT_ID = 0x08
LIVE_REPORT_PAYLOAD = 381    # descriptor size of report 8 (without the ID)
LIVE_HEADER = bytes([0x08, 0x0A, 0x7A, 0x01])
LIVE_FRAME_LEN = 382         # report ID + 381
LIVE_SLOTS = 126             # 21 columns x 6 rows, 3 bytes each = 378
LIVE_KEEPALIVE_S = 1.0       # resend an unchanged frame after this long
#: Minimum time between two flash writes of the edge lights.
EDGE_MIN_INTERVAL_S = 10.0

#: Halo65 key name (``Scene.keys`` names) -> LED slot (``column * 6 + row``),
#: taken from NuPhy Console's configuration for this model.
HALO65_KEY_SLOTS: dict[str, int] = {
    "ESC": 1, "1": 7, "2": 13, "3": 19, "4": 25, "5": 31, "6": 37, "7": 43,
    "8": 49, "9": 55, "0": 61, "MINUS": 67, "EQUAL": 73, "BACKSPACE": 79,
    "TAB": 2, "Q": 8, "W": 14, "E": 20, "R": 26, "T": 32, "Y": 38, "U": 44,
    "I": 50, "O": 56, "P": 62, "LBRACKET": 68, "RBRACKET": 74,
    "BACKSLASH": 80,
    "CAPS": 3, "A": 9, "S": 15, "D": 21, "F": 27, "G": 33, "H": 39, "J": 45,
    "K": 51, "L": 57, "SEMICOLON": 63, "QUOTE": 69, "ENTER": 81,
    "LSHIFT": 4, "Z": 10, "X": 16, "C": 22, "V": 28, "B": 34, "N": 40,
    "M": 46, "COMMA": 52, "PERIOD": 58, "SLASH": 64, "RSHIFT": 82, "UP": 88,
    "LCTRL": 5, "LWIN": 11, "LALT": 17, "SPACE": 35, "RALT": 53, "FN": 59,
    "LEFT": 83, "DOWN": 89, "RIGHT": 95,
    "DEL": 91, "PGUP": 92, "PGDN": 93, "END": 94,
}
#: Model name (as in ``SUPPORTED_MODELS``) -> key slot map. Add Halo75/96 here
#: once their maps are known; models without an entry get no live mode.
KEY_SLOTS_BY_MODEL: dict[str, dict[str, int]] = {"Halo65": HALO65_KEY_SLOTS}

BACKLIGHT_MODES = {
    0: "Lights off", 1: "Static", 2: "Respire", 3: "Rainbow", 4: "Flash away",
    6: "Rainbow wheel", 7: "Ripples shining", 8: "Stars twinkle",
    11: "Neon stream", 12: "Reaction", 13: "Sine wave", 15: "Rotating windmill",
    16: "Colorful waterfall", 17: "Blossoming", 18: "Custom",
}
HALO_MODES = {0: "Lights off", 1: "Rainbow wheel", 2: "Rainbow", 3: "Fixed",
              4: "Respire"}
SIDELIGHT_MODES = {0: "Lights off", 1: "Rainbow stream", 2: "Rainbow",
                   3: "Fixed on", 4: "Respire", 5: "Neon stream"}


class _ProtocolError(Exception):
    """Unexpected or missing reply from the keyboard."""


def _feature_report_sizes(descriptor: bytes) -> dict[int, int]:
    """Parse a HID report descriptor -> {report ID: feature payload bytes}."""
    sizes: dict[int, int] = {}
    report_id = size = count = 0
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item: skip
            i += 3 + (descriptor[i + 1] if i + 1 < len(descriptor) else 0)
            continue
        nbytes = (0, 1, 2, 4)[prefix & 3]
        value = int.from_bytes(descriptor[i + 1:i + 1 + nbytes], "little")
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        if kind == 1:  # global
            if tag == 7:
                size = value
            elif tag == 8:
                report_id = value
            elif tag == 9:
                count = value
        elif kind == 0 and tag == 0xB:  # main: Feature
            sizes[report_id] = sizes.get(report_id, 0) + size * count // 8
        i += 1 + nbytes
    return sizes


def _group_key(path: bytes) -> str:
    """Identify the physical board a collection path belongs to (Windows
    paths of one board differ only in ``ColNN`` and the trailing counter)."""
    match = re.search(rb"#([0-9a-fA-F]+&[0-9a-fA-F]+&\d+)&[0-9a-fA-F]+#", path)
    return match.group(1).decode("ascii", "replace") if match else "default"


def _format_version(release: int) -> str:
    """nuga-lib FormatVersion: 0x118 -> '1.1.8'."""
    return ".".join(format(release, "x"))


class _Board:
    """One opened keyboard: command handle (report 5), data handle (report 6)."""

    def __init__(self, key: str, product: str, model: str, firmware: str,
                 cmd_dev, data_dev, live_dev=None) -> None:
        self.key = key
        self.product = product
        self.model = model
        self.firmware = firmware
        self.cmd_dev = cmd_dev
        self.data_dev = data_dev
        self.live_dev = live_dev  # report-8 collection, None if absent
        self.last_frame: bytes | None = None   # last live frame sent
        self.last_sent_t = 0.0

    def close(self) -> None:
        seen = []
        for dev in (self.cmd_dev, self.data_dev, self.live_dev):
            if dev is None or any(dev is s for s in seen):
                continue
            seen.append(dev)
            try:
                dev.close()
            except Exception:
                log.debug("NuPhy handle close failed", exc_info=True)
        self.cmd_dev = self.data_dev = self.live_dev = None

    # -- transport ---------------------------------------------------------
    def _send(self, dev, payload: bytes) -> None:
        last: Exception | None = None
        for _ in range(REQUEST_RETRIES):
            try:
                sent = dev.send_feature_report(payload)
                if sent == len(payload) or sent is None:
                    time.sleep(SYNC_DELAY_S)
                    return
                last = _ProtocolError(f"sent {sent} of {len(payload)} bytes")
            except (OSError, ValueError) as exc:
                last = exc
        raise _ProtocolError(f"feature report send failed: {last}")

    def request(self, command: bytes) -> bytes:
        """Send a read command, return the report-6 payload without its ID."""
        last: Exception | None = None
        for _ in range(REQUEST_RETRIES):
            try:
                self._send(self.cmd_dev, command)
                raw = bytes(self.data_dev.get_feature_report(
                    DATA_REPORT_ID, DATA_REPORT_LEN))
                time.sleep(SYNC_DELAY_S)
            except (OSError, ValueError, _ProtocolError) as exc:
                last = exc
                continue
            if len(raw) > 1 and raw[0] == DATA_REPORT_ID and raw[1] == command[1]:
                return raw[1:]
            last = _ProtocolError(f"unexpected reply header {raw[:2].hex()}")
        raise _ProtocolError(f"read 0x{command[1]:02x} failed: {last}")

    def write(self, packet: bytes) -> None:
        if len(packet) != DATA_REPORT_LEN or packet[0] != DATA_REPORT_ID:
            raise _ProtocolError("refusing malformed write packet")
        self._send(self.data_dev, packet)

    # -- decoded reads -------------------------------------------------------
    def read_effects(self) -> bytes:
        resp = self.request(CMD_GET_PARAMS)
        effects = resp[EFFECTS_OFFSET:EFFECTS_OFFSET + EFFECTS_LEN]
        if (len(effects) != EFFECTS_LEN or effects[:2] != HEADER
                or effects[-2:] != END_MARK):
            raise _ProtocolError(
                f"unrecognised lighting block ({len(effects)} bytes, "
                f"starts {effects[:4].hex()})")
        return effects

    def read_colors(self) -> bytes:
        resp = self.request(CMD_GET_COLORS)
        colors = resp[COLORS_OFFSET:COLORS_OFFSET + COLORS_LEN]
        if len(colors) != COLORS_LEN:
            raise _ProtocolError(f"palette reply too short ({len(colors)} bytes)")
        return colors

    def write_effects(self, new: bytes, previous: bytes) -> None:
        packet = CMD_SET_PARAMS + new + previous + bytes(770)
        self.write(packet)

    def write_colors(self, colors: bytes) -> None:
        self.write(CMD_SET_COLORS + colors)

    def send_frame(self, frame: bytes) -> None:
        """Send one live frame (report 8). One attempt, no sync delay, no retry:
        the next frame is 33 ms away anyway."""
        dev = self.live_dev
        if dev is None:
            raise _ProtocolError("no live-frame handle")
        if len(frame) != LIVE_FRAME_LEN or frame[:4] != LIVE_HEADER:
            raise _ProtocolError("refusing malformed live frame")
        sent = dev.send_feature_report(frame)
        if sent is not None and sent != len(frame):
            raise _ProtocolError(f"sent {sent} of {len(frame)} live bytes")


def _clamp_rgb(color) -> RGB:
    r, g, b = color
    return (max(0, min(255, int(r))), max(0, min(255, int(g))),
            max(0, min(255, int(b))))


def build_live_frame(base: RGB, keys: dict[str, RGB],
                     slots: dict[str, int]) -> bytes:
    """Report-8 frame: ``base`` in all 126 slots, then ``keys`` by slot.

    Key names missing from ``slots`` are ignored. Pure function.
    """
    payload = bytearray(bytes(_clamp_rgb(base)) * LIVE_SLOTS)
    for name, color in keys.items():
        slot = slots.get(str(name).upper())
        if slot is not None:
            payload[slot * 3:slot * 3 + 3] = bytes(_clamp_rgb(color))
    return LIVE_HEADER + bytes(payload) + bytes(LIVE_FRAME_LEN - 4 - len(payload))


def _nearest_fixed_color(r: int, g: int, b: int) -> int:
    """Index (0-6) of the edge-light preset color closest to (r, g, b).

    Compared in HSV with brightness ignored (the lights have their own
    brightness setting), so dark blue still maps to blue, not to black/white.
    Hue and saturation form a point on the color wheel (radius = saturation):
    the six hues sit on the rim and white at the centre. Grays, near-grays and
    black therefore map to white.
    """
    h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    x, y = s * math.cos(h * 2 * math.pi), s * math.sin(h * 2 * math.pi)
    best, best_dist = 6, math.hypot(x, y)  # white (centre) wins ties
    for index, (_name, (pr, pg, pb)) in enumerate(FIXED_COLORS[:6]):
        ph, _ps, _pv = colorsys.rgb_to_hsv(pr / 255, pg / 255, pb / 255)
        dist = math.hypot(x - math.cos(ph * 2 * math.pi),
                          y - math.sin(ph * 2 * math.pi))
        if dist < best_dist - 1e-9:
            best, best_dist = index, dist
    return best


def _set_edge_bytes(new: bytearray, rgb: bytes) -> int:
    """Put both edge lights in fixed mode with the preset nearest to ``rgb``.

    Edits ``new`` (an effects block) in place and touches only the sidelight
    and halo bytes [12:20]; brightness is kept unless it is 0 and speed is left
    alone. Returns the preset index (0-6).
    """
    color = _nearest_fixed_color(*rgb)
    for index, fixed_mode in ((I_SIDELIGHT, SIDELIGHT_FIXED),
                              (I_HALO, HALO_FIXED)):
        new[index] = fixed_mode
        new[index + 1] = color
        if new[index + 2] == 0:  # brightness 0-4; speed is left as is
            new[index + 2] = MISC_MID_BRIGHTNESS
    return color


def _apply_edge(board: _Board, rgb: bytes) -> bool:
    """Read-modify-write ONLY the edge-light bytes (saved to flash!).

    Returns True when something was written. Nothing is written when the edge
    lights already show that preset. The original lighting is backed up before
    the first write (an OSError aborts before anything is sent).
    """
    effects = board.read_effects()
    new = bytearray(effects)
    _set_edge_bytes(new, rgb)
    if bytes(new) == effects:
        return False
    _ensure_backup(board, effects, board.read_colors())
    board.write_effects(bytes(new), effects)
    if board.read_effects() != bytes(new):
        raise _ProtocolError("read-back of lighting block differs")
    return True


def _palette_row(colors: bytes, row: int) -> list[str]:
    base = row * COLORS_PER_MODE * 3
    return ["#" + colors[base + i * 3:base + i * 3 + 3].hex()
            for i in range(COLORS_PER_MODE)]


def _decode_misc(effects: bytes, index: int, modes: dict[int, str]) -> dict:
    mode, color, brightness, speed = effects[index:index + 4]
    return {"mode": modes.get(mode, f"unknown({mode})"), "mode_code": mode,
            "color_index": color, "brightness": brightness, "speed": speed}


def decode_state(effects: bytes, colors: bytes) -> dict:
    """Turn raw effects + palette bytes into a readable dict (pure function)."""
    code = effects[I_BACKLIGHT_MODE]
    state: dict = {
        "debounce_ms": effects[I_DEBOUNCE],
        "custom_flag": effects[I_CUSTOM_FLAG],
        "backlight": {"mode": BACKLIGHT_MODES.get(code, f"unknown({code})"),
                      "mode_code": code},
        "sidelight": _decode_misc(effects, I_SIDELIGHT, SIDELIGHT_MODES),
        "halo": _decode_misc(effects, I_HALO, HALO_MODES),
        "misc_header_hex": effects[6:12].hex(),
    }
    if 1 <= code <= BACKLIGHT_PARAMS_COUNT:
        off = I_BACKLIGHT_PARAMS + (code - 1) * 2
        color, value = effects[off], effects[off + 1]
        back = state["backlight"]
        back["color_index"] = color  # 0-6 palette slot, 7 = random
        back["value_byte"] = f"0x{value:02x}"
        # nuga-lib's serializer says speed*16+brightness, its parser the
        # opposite; both readings are shown (identical when the nibbles match).
        back["brightness_if_low_nibble"] = value & 0x0F
        back["speed_if_high_nibble"] = value >> 4
        if color <= 6:
            back["rgb_windows_bank"] = _palette_row(colors, code)[color]
            back["rgb_mac_bank"] = _palette_row(
                colors, MODES_PER_OS + code)[color]
        else:
            back["rgb_windows_bank"] = back["rgb_mac_bank"] = "random"
    state["static_palette_windows_row"] = _palette_row(colors, MODE_STATIC)
    state["static_palette_mac_row"] = _palette_row(
        colors, MODES_PER_OS + MODE_STATIC)
    state["effects_hex"] = effects.hex()
    return state


def _backup_path(board: _Board) -> Path:
    """Backup file of this board's model (looked up in ``BACKUP_DIR`` now)."""
    model = re.sub(r"[^A-Za-z0-9_.-]", "_", board.model) or "unknown"
    return Path(BACKUP_DIR) / f"{BACKUP_PREFIX}{model}.json"


def _ensure_backup(board: _Board, effects: bytes, colors: bytes) -> None:
    """Save the board's original lighting once; never overwrite a backup.

    Raises OSError when the file cannot be written, so callers can abort
    before anything is sent to the keyboard.
    """
    path = _backup_path(board)
    if path.exists():
        return
    data = {
        "product": board.product,
        "model": board.model,
        "firmware": board.firmware,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "effects_hex": effects.hex(),
        "colors_hex": colors.hex(),
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():  # appeared meanwhile: keep the earlier one
            return
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    log.info("NuPhy original lighting backed up to %s", path)


def _load_backup(path: Path) -> tuple[bytes, bytes]:
    """Read and validate a backup file -> (effects, colors). Raises ValueError."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        effects = bytes.fromhex(data["effects_hex"])
        colors = bytes.fromhex(data["colors_hex"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"unreadable backup {path.name}: {exc}") from exc
    if len(effects) != EFFECTS_LEN:
        raise ValueError(f"effects length {len(effects)} != {EFFECTS_LEN}")
    if len(colors) != COLORS_LEN:
        raise ValueError(f"colors length {len(colors)} != {COLORS_LEN}")
    if effects[:2] != HEADER or effects[-2:] != END_MARK:
        raise ValueError("effects block has a bad header or end marker")
    return effects, colors


class NuPhyBackend(LightingBackend):
    """Controls NuPhy Halo65/75/96 (V1 firmware) connected by USB cable.

    ``set_color`` switches the main backlight to Static mode and shows the
    exact requested RGB (palette slot 6 of the Static row, in both the Windows
    and Mac banks, is rewritten with it). With ``SYNC_EDGE_LIGHTS`` the side
    lights and halo are set to their fixed mode in the nearest of their 7 preset
    colors (they cannot show arbitrary RGB). Keymaps, macros and all other
    lighting bytes are echoed back unchanged. Wireless / Bluetooth
    mode cannot be controlled. Close NuPhy Console while this app runs.
    """

    name = "NuPhy"

    def __init__(self) -> None:
        self.status = "not connected"
        self._boards: list[_Board] = []
        self._lock = threading.Lock()      # board list + report 5/6 transactions
        # Live state. _live_lock guards the report-8 handles and frame state;
        # it is only ever taken after _lock (close), never the other way round,
        # and render() takes only _live_lock, so a slow settings write on _lock
        # never stalls the frame loop.
        self._live_lock = threading.Lock()
        self._clock = time.monotonic       # replaced in tests
        self._live_active = False
        self._live_error = False
        self._last_base: RGB | None = None
        self._set_color_rgb: RGB | None = None
        self._edge_applied: int | None = None   # preset index last written
        self._edge_last_attempt = -1e9
        self._edge_thread: threading.Thread | None = None
        self._edge_async = True            # tests set False for determinism

    # -- connection --------------------------------------------------------
    def connect(self) -> bool:
        self.close()
        if _IMPORT_ERROR:
            self.status = f"error: hidapi unavailable ({_IMPORT_ERROR})"
            return False
        with self._lock:
            try:
                boards, skipped = self._open_boards()
            except Exception as exc:
                log.exception("NuPhy connect failed")
                self.status = f"error: {exc}"
                return False
            self._boards = boards
        if boards:
            self._refresh_live_support()
            return True
        if skipped:
            self.status = f"not found: unsupported NuPhy model ({skipped})"
        else:
            self.status = ("not found: no NuPhy keyboard on USB "
                           "(wireless mode can't be controlled)")
        return False

    @staticmethod
    def _open_boards() -> tuple[list[_Board], str]:
        groups: dict[str, list[dict]] = {}
        for info in hid.enumerate(VENDOR_ID, PRODUCT_ID):
            if info.get("usage_page") == USAGE_PAGE and info.get("usage") == USAGE:
                groups.setdefault(_group_key(info["path"]), []).append(info)
        boards: list[_Board] = []
        skipped: list[str] = []
        for key, infos in groups.items():
            product = infos[0].get("product_string") or ""
            if not product.startswith(VENDOR_PREFIX):
                continue  # a genuine Apple device: never touch it
            model = product[len(VENDOR_PREFIX):]
            if model not in SUPPORTED_MODELS:
                skipped.append(product)
                continue
            opened: list[tuple[object, dict[int, int]]] = []
            try:
                for info in infos:
                    dev = hid.device()
                    dev.open_path(info["path"])
                    try:
                        sizes = _feature_report_sizes(
                            bytes(dev.get_report_descriptor()))
                    except Exception:
                        dev.close()
                        raise
                    opened.append((dev, sizes))
                cmd_dev = next((d for d, s in opened if CMD_REPORT_ID in s), None)
                data_dev = next((d for d, s in opened
                                 if s.get(DATA_REPORT_ID) == DATA_REPORT_LEN - 1),
                                None)
                if cmd_dev is None or data_dev is None:
                    raise _ProtocolError("feature reports 5/6 not found in "
                                         "HID descriptors (unsupported firmware?)")
                live_dev = next((d for d, s in opened
                                 if s.get(LIVE_REPORT_ID) == LIVE_REPORT_PAYLOAD),
                                None)
                for dev, _ in opened:  # release collections we do not use
                    if dev is not cmd_dev and dev is not data_dev \
                            and dev is not live_dev:
                        dev.close()
                firmware = _format_version(infos[0].get("release_number", 0))
                boards.append(_Board(key, product, model, firmware,
                                     cmd_dev, data_dev, live_dev))
            except Exception:
                for dev, _ in opened:
                    try:
                        dev.close()
                    except Exception:
                        pass
                raise
        return boards, ", ".join(skipped)

    def list_devices(self) -> list[Device]:
        with self._lock:
            return [Device(self.name, b.key, b.product, "keyboard")
                    for b in self._boards]

    # -- lighting ----------------------------------------------------------
    def read_state(self, index: int = 0) -> dict | None:
        """Decoded current lighting state of board `index` (read-only).

        Sends only the two read commands (05 83 b6 / 05 88 b8). Returns None
        when not connected, and sets `status` on failure.
        """
        with self._lock:
            if index >= len(self._boards):
                return None
            board = self._boards[index]
            try:
                effects = board.read_effects()
                colors = board.read_colors()
            except Exception as exc:
                self._fail(exc)
                return None
            state = decode_state(effects, colors)
            state["model"] = board.model
            state["product"] = board.product
            state["firmware"] = board.firmware
            return state

    def set_color(self, r: int, g: int, b: int) -> None:
        rgb = bytes(max(0, min(255, int(c))) for c in (r, g, b))
        with self._lock:
            failures = []
            for board in self._boards:
                try:
                    self._apply(board, rgb)
                    self._set_color_rgb = (rgb[0], rgb[1], rgb[2])
                    self._edge_applied = (_nearest_fixed_color(*rgb)
                                          if SYNC_EDGE_LIGHTS else None)
                except Exception as exc:
                    log.warning("NuPhy set_color failed: %s", exc)
                    failures.append(f"{board.product}: {exc}")
                    self._close_boards()
                    break
            if failures:
                self.status = f"error: {'; '.join(failures)}"
            elif self._boards:
                self.status = "connected"

    @staticmethod
    def _apply(board: _Board, rgb: bytes) -> None:
        """Read-modify-write: Static mode + exact RGB (+ nearest preset on the edge
        lights), everything else kept.

        The first time, the original blocks are saved to a backup file; if
        that fails nothing is written to the keyboard.
        """
        effects = board.read_effects()
        colors = board.read_colors()
        _ensure_backup(board, effects, colors)

        # Palette: rewrite one slot in the Static row of both OS banks.
        new_colors = bytearray(colors)
        for row in (MODE_STATIC, MODES_PER_OS + MODE_STATIC):
            base = (row * COLORS_PER_MODE + SYNC_SLOT) * 3
            new_colors[base:base + 3] = rgb
        palette_changed = bytes(new_colors) != colors

        # Effects: Static mode, selected slot, keep brightness unless dark.
        new = bytearray(effects)
        new[I_CUSTOM_FLAG] = 0
        new[I_BACKLIGHT_MODE] = MODE_STATIC
        off = I_BACKLIGHT_PARAMS + (MODE_STATIC - 1) * 2
        new[off] = SYNC_SLOT
        if new[off + 1] == 0:
            new[off + 1] = MID_VALUE

        if SYNC_EDGE_LIGHTS:
            color = _set_edge_bytes(new, rgb)
            log.info("edge lights use preset colors; closest to #%s is %s",
                     rgb.hex(), FIXED_COLORS[color][0])
        effects_changed = bytes(new) != effects

        if palette_changed:
            board.write_colors(bytes(new_colors))
        if palette_changed or effects_changed:
            # Nuga.app rewrites the effects after a palette change so the
            # keyboard re-reads it; the "previous" copy is what we just read.
            board.write_effects(bytes(new), effects)
            if board.read_effects() != bytes(new):
                raise _ProtocolError("read-back of lighting block differs")
            if palette_changed and board.read_colors() != bytes(new_colors):
                raise _ProtocolError("read-back of palette differs")

    # -- live lighting -------------------------------------------------------
    def _refresh_live_support(self) -> None:
        """Set ``supports_live`` and the status line after (re)connecting."""
        usable = [b for b in self._boards
                  if b.live_dev is not None and b.model in KEY_SLOTS_BY_MODEL]
        self.supports_live = bool(usable)
        if usable:
            self.status = "connected"
            return
        reasons = []
        for b in self._boards:
            if b.live_dev is None:
                reasons.append(f"{b.model} has no live-frame report "
                               f"(firmware {b.firmware})")
            else:
                reasons.append(f"no key map for {b.model} yet")
        self.status = f"connected (live lighting unavailable: {'; '.join(reasons)})"

    def start_live(self) -> None:
        with self._live_lock:
            self._live_active = True
            self._live_error = False
            for board in self._boards:
                board.last_frame = None

    def render(self, scene: Scene) -> None:
        """Send one key-backlight frame; apply ``scene.edge`` if it changed.

        Frames go over report 8 (RAM only). An identical frame is not resent
        until ``LIVE_KEEPALIVE_S`` has passed. Never raises.
        """
        try:
            now = self._clock()
            base = _clamp_rgb(scene.base)
            with self._live_lock:
                if not self._live_active:
                    return
                self._last_base = base
                for board in self._boards:
                    slots = KEY_SLOTS_BY_MODEL.get(board.model)
                    if board.live_dev is None or slots is None:
                        continue
                    frame = build_live_frame(base, scene.keys, slots)
                    if (frame == board.last_frame
                            and now - board.last_sent_t < LIVE_KEEPALIVE_S):
                        continue
                    try:
                        board.send_frame(frame)
                    except Exception as exc:
                        board.last_frame = None  # retry next tick
                        if not self._live_error:
                            log.warning("NuPhy live frame failed: %s", exc)
                        self._live_error = True
                        self.status = f"error: live frame failed: {exc}"
                        continue
                    board.last_frame, board.last_sent_t = frame, now
                    if self._live_error:
                        self._live_error = False
                        self.status = "connected"
            self._maybe_apply_edge(scene.edge, now)
        except Exception as exc:
            log.warning("NuPhy render failed: %s", exc)
            self.status = f"error: {exc}"

    def _maybe_apply_edge(self, edge: RGB | None, now: float) -> None:
        """Start an edge-light write if the wanted preset differs from the last
        one written and at least ``EDGE_MIN_INTERVAL_S`` passed since the last
        attempt. Failed attempts count too, so errors cannot cause a write storm.

        The write (2 reads + 1 write + 1 read-back, 0.5 s or more) runs on a
        one-at-a-time worker thread so the frame loop keeps its rate.
        """
        if edge is None or not SYNC_EDGE_LIGHTS:
            return
        rgb = bytes(_clamp_rgb(edge))
        preset = _nearest_fixed_color(*rgb)
        if preset == self._edge_applied:
            return
        if now - self._edge_last_attempt < EDGE_MIN_INTERVAL_S:
            return
        thread = self._edge_thread
        if thread is not None and thread.is_alive():
            return
        self._edge_last_attempt = now
        if not self._edge_async:
            self._edge_job(rgb, preset)
            return
        self._edge_thread = threading.Thread(
            target=self._edge_job, args=(rgb, preset), daemon=True,
            name="nuphy-edge")
        self._edge_thread.start()

    def _edge_job(self, rgb: bytes, preset: int) -> None:
        with self._lock:  # serialises with set_color / restore on report 5/6
            if not self._boards:
                return
            try:
                for board in self._boards:
                    _apply_edge(board, rgb)
            except Exception as exc:
                self._fail(exc)
                return
            self._edge_applied = preset
        with self._live_lock:  # the write may have reset the board: resend
            for board in self._boards:
                board.last_frame = None

    def stop_live(self) -> None:
        """Send one last frame in the color from the last ``set_color`` (else the
        last live base), then stop. Frames are RAM only, so without this the
        keys would keep the final live frame."""
        with self._live_lock:
            if not self._live_active:
                return
            self._live_active = False
            rgb = self._set_color_rgb or self._last_base
            for board in self._boards:
                board.last_frame = None
                slots = KEY_SLOTS_BY_MODEL.get(board.model)
                if rgb is None or board.live_dev is None or slots is None:
                    continue
                try:
                    board.send_frame(build_live_frame(rgb, {}, slots))
                except Exception as exc:
                    log.warning("NuPhy final live frame failed: %s", exc)
                    self.status = f"error: final live frame failed: {exc}"

    def restore(self) -> str:
        """Put the keyboard back as saved; stays connected (no reconnect)."""
        return self.restore_backup()

    def restore_backup(self) -> str:
        """Write the saved original lighting back to each connected board.

        Returns a human-readable result (one line per board). A board with no
        backup file was never changed by this app, so nothing is written.
        """
        with self._lock:
            if not self._boards:
                return "NuPhy: not connected, nothing restored"
            lines: list[str] = []
            for board in list(self._boards):
                path = _backup_path(board)
                if not path.exists():
                    lines.append(f"{board.product}: nothing to restore "
                                 f"(never changed, no backup file)")
                    continue
                try:
                    effects, colors = _load_backup(path)
                except ValueError as exc:
                    lines.append(f"{board.product}: backup invalid ({exc}), "
                                 "nothing restored")
                    continue
                try:
                    current = board.read_effects()
                    board.write_colors(colors)
                    board.write_effects(effects, current)
                    if (board.read_effects() != effects
                            or board.read_colors() != colors):
                        raise _ProtocolError("read-back differs from backup")
                except Exception as exc:
                    self._fail(exc)
                    lines.append(f"{board.product}: restore failed: {exc}")
                    break
                lines.append(f"{board.product}: restored from {path.name}")
            return "\n".join(lines)

    def _fail(self, exc: Exception) -> None:
        log.warning("NuPhy I/O failed: %s", exc)
        self.status = f"error: {exc}"
        self._close_boards()

    def _close_boards(self) -> None:
        boards, self._boards = self._boards, []
        self.supports_live = False
        with self._live_lock:  # wait for an in-flight frame send
            for board in boards:
                board.close()

    def close(self) -> None:
        with self._lock:
            self._close_boards()
