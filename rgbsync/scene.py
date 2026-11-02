"""A Scene is one frame of live lighting, independent of any brand.

Effect sources (e.g. League mode) produce Scenes; the live engine pushes them
to every backend that supports live lighting. Backends map what they can:
a keyboard lights individual keys, fans and other devices show ``base``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

RGB = tuple[int, int, int]

#: Logical key names used in ``Scene.keys`` (upper case). Backends ignore
#: names they cannot map.
KEY_NAMES = (
    "ESC", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "MINUS", "EQUAL",
    "BACKSPACE", "TAB", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P",
    "LBRACKET", "RBRACKET", "BACKSLASH", "CAPS", "A", "S", "D", "F", "G", "H",
    "J", "K", "L", "SEMICOLON", "QUOTE", "ENTER", "LSHIFT", "Z", "X", "C", "V",
    "B", "N", "M", "COMMA", "PERIOD", "SLASH", "RSHIFT", "LCTRL", "LWIN",
    "LALT", "SPACE", "RALT", "FN", "LEFT", "DOWN", "RIGHT", "UP", "DEL",
    "PGUP", "PGDN", "END",
)


@dataclass
class Scene:
    """What every device should show right now."""

    #: Color for everything not listed in ``keys`` (and for non-keyboards).
    base: RGB
    #: Per-key overrides, by name from ``KEY_NAMES``.
    keys: dict[str, RGB] = field(default_factory=dict)
    #: Requested color for slow, saved-setting lights (NuPhy side light and
    #: halo). Backends apply it only when it changes, never per frame.
    edge: RGB | None = None
