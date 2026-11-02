"""Abstract interface every lighting backend implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ..scene import Scene


@dataclass(frozen=True)
class Device:
    """One lighting device (or device group) reported by a backend."""

    backend: str  # brand / backend name, e.g. "Corsair"
    id: str       # backend-specific unique id
    name: str     # human-readable name
    kind: str     # e.g. "fan controller", "headset", "keyboard"


class LightingBackend(ABC):
    """A vendor SDK wrapper. Methods must not raise for expected failures."""

    #: Short brand name shown in the UI.
    name: str = "Unknown"
    #: Human-readable state: "connected", "not found: <why>" or "error: <why>".
    status: str = "not connected"
    #: Result text of the default `restore()` when reconnecting worked.
    restored_text: str = "back to default lighting"
    #: True when this backend can show real-time ``Scene`` frames (see
    #: ``render``). May be set per instance after ``connect()``.
    supports_live: bool = False

    @abstractmethod
    def connect(self) -> bool:
        """Connect to the vendor SDK. Returns True on success; sets `status`."""

    @abstractmethod
    def list_devices(self) -> list[Device]:
        """Return the devices currently known to this backend."""

    @abstractmethod
    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every device on this backend to a static color (0-255 each)."""

    @abstractmethod
    def close(self) -> None:
        """Release SDK control and free resources. Safe to call repeatedly."""

    def restore(self) -> str:
        """Hand lighting back to the vendor app; returns a short result.

        Default for SDKs that store nothing on the device: release control
        (the vendor app shows its own lighting again), then reconnect without
        setting any color so this app stays usable.
        """
        self.close()
        if self.connect():
            return self.restored_text
        return f"released control, but reconnect failed ({self.status})"

    # -- live (real-time) lighting -----------------------------------------
    # Driven by rgbsync.live.LiveEngine: start_live() once, then render() on
    # every tick, then stop_live() once. All three default to no-ops.

    def start_live(self) -> None:
        """Called once before the first ``render``. Cache per-device data here."""

    def render(self, scene: "Scene") -> None:
        """Show one live frame.

        Called from the engine thread at up to 30 fps, so it must be fast
        (skip work when nothing changed) and must NEVER raise: catch errors,
        set ``status`` and carry on. Anything slow or saved to the device must
        be rate limited or moved to a worker thread by the backend itself.
        """

    def stop_live(self) -> None:
        """Called once after the last ``render``; leave the device in a sane state."""
