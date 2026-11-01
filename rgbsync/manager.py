"""Holds all backends and fans operations out to them safely."""
from __future__ import annotations

import logging

from .backends.base import Device, LightingBackend
from .backends.corsair import CorsairBackend
from .backends.razer import RazerBackend

log = logging.getLogger(__name__)

# To add a brand, write a LightingBackend subclass and add it here.
BACKEND_CLASSES: list[type[LightingBackend]] = [CorsairBackend, RazerBackend]


class Manager:
    """Owns every backend; one failing backend never affects the others."""

    def __init__(self) -> None:
        self.backends: list[LightingBackend] = [cls() for cls in BACKEND_CLASSES]

    def connect_all(self) -> dict[str, bool]:
        """(Re)connect every backend. Returns {backend name: connected}."""
        results: dict[str, bool] = {}
        for backend in self.backends:
            try:
                results[backend.name] = backend.connect()
            except Exception as exc:
                log.exception("%s connect crashed", backend.name)
                backend.status = f"error: {exc}"
                results[backend.name] = False
            log.info("%s: %s", backend.name, backend.status)
        return results

    def statuses(self) -> dict[str, str]:
        """Current status line for each backend."""
        return {b.name: b.status for b in self.backends}

    def devices(self) -> list[Device]:
        """All devices from all backends."""
        found: list[Device] = []
        for backend in self.backends:
            try:
                found.extend(backend.list_devices())
            except Exception as exc:
                log.exception("%s list_devices crashed", backend.name)
                backend.status = f"error: {exc}"
        return found

    def apply_color(self, r: int, g: int, b: int) -> None:
        """Set a static color on every connected backend."""
        for backend in self.backends:
            try:
                backend.set_color(r, g, b)
            except Exception as exc:
                log.exception("%s set_color crashed", backend.name)
                backend.status = f"error: {exc}"

    def close_all(self) -> None:
        """Release control on every backend."""
        for backend in self.backends:
            try:
                backend.close()
            except Exception:
                log.exception("%s close crashed", backend.name)
