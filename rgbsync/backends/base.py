"""Abstract interface every lighting backend implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


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
