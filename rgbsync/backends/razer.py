"""Razer backend using the Chroma REST API (no extra package needed)."""
from __future__ import annotations

import logging
import threading
import time

import requests

from .base import Device, LightingBackend
from ..scene import Scene

log = logging.getLogger(__name__)

BASE_URL = "http://localhost:54235/razer/chromasdk"
TIMEOUT_S = 2
HEARTBEAT_S = 10
#: Live mode: REST is slow, so send at most this often (and only on change).
LIVE_MIN_INTERVAL_S = 0.1
CATEGORIES = ["keyboard", "mouse", "headset", "mousepad", "keypad", "chromalink"]
_APP_INFO = {
    "title": "RGB Sync",
    "description": "Sync lighting across brands",
    "author": {"name": "RGB Sync", "contact": "local"},
    "device_supported": CATEGORIES,
    "category": "application",
}
_NOT_RUNNING = "Razer Synapse / Chroma not running"


class RazerBackend(LightingBackend):
    """Controls Razer Chroma devices through the local Synapse REST server."""

    name = "Razer"
    restored_text = "back to Synapse lighting"

    def __init__(self) -> None:
        self.status = "not connected"
        self._uri: str | None = None
        self._active: list[str] = []  # categories that accepted the last apply
        self._applied = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._clock = time.monotonic  # replaced in tests
        self._live_last: tuple[int, int, int] | None = None
        self._live_last_t = -1e9
        self._live_thread: threading.Thread | None = None

    def connect(self) -> bool:
        self.close()
        try:
            resp = requests.post(BASE_URL, json=_APP_INFO, timeout=TIMEOUT_S)
            resp.raise_for_status()
            data = resp.json()
            self._uri = data["uri"]
        except (requests.ConnectionError, requests.Timeout):
            self.status = f"not found: {_NOT_RUNNING}"
            return False
        except Exception as exc:
            self.status = f"error: Chroma init failed ({exc})"
            return False
        time.sleep(1)  # Chroma needs a moment before accepting effects
        self._stop.clear()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        self.supports_live = True
        self.status = "connected"
        return True

    def _heartbeat(self) -> None:
        """Keep the Chroma session alive until close()."""
        while not self._stop.wait(HEARTBEAT_S):
            uri = self._uri
            if not uri:
                return
            try:
                requests.put(f"{uri}/heartbeat", timeout=TIMEOUT_S)
            except requests.RequestException as exc:
                log.warning("Razer heartbeat failed: %s", exc)

    def list_devices(self) -> list[Device]:
        """Chroma REST cannot enumerate hardware, so report categories."""
        if not self._uri:
            return []
        if not self._applied:
            return [Device(self.name, "all", "Razer Chroma devices (all categories)",
                           "various")]
        return [Device(self.name, c, f"Razer {c}", c) for c in self._active]

    def set_color(self, r: int, g: int, b: int) -> None:
        uri = self._uri
        if not uri:
            return
        bgr = (b << 16) | (g << 8) | r
        body = {"effect": "CHROMA_STATIC", "param": {"color": bgr}}
        active: list[str] = []
        errors: list[str] = []
        for cat in CATEGORIES:
            try:
                resp = requests.put(f"{uri}/{cat}", json=body, timeout=TIMEOUT_S)
                result = resp.json().get("result") if resp.content else None
                if result == 0:
                    active.append(cat)
            except (requests.ConnectionError, requests.Timeout):
                errors.append(_NOT_RUNNING)
                break
            except Exception as exc:
                errors.append(f"{cat}: {exc}")
        self._active, self._applied = active, True
        if errors:
            self.status = f"error: {errors[0]}"
        else:
            self.status = "connected"

    def start_live(self) -> None:
        self._live_last = None

    def render(self, scene: Scene) -> None:
        """CHROMA_STATIC with ``scene.base``, only on change, max 10 per second.

        The REST calls run on a short-lived thread so a slow Synapse cannot stall
        the engine (and with it the other brands).
        """
        try:
            rgb = tuple(max(0, min(255, int(c))) for c in scene.base)
            now = self._clock()
            if (self._uri is None or rgb == self._live_last
                    or now - self._live_last_t < LIVE_MIN_INTERVAL_S):
                return
            busy = self._live_thread
            if busy is not None and busy.is_alive():
                return  # previous REST round is still running; retry next tick
            self._live_last, self._live_last_t = rgb, now
            # set_color makes one REST call per device category (slow, and up
            # to TIMEOUT_S each when Synapse hangs): never block the engine.
            self._live_thread = threading.Thread(
                target=self.set_color, args=rgb, daemon=True)
            self._live_thread.start()
        except Exception as exc:
            log.warning("Razer render failed: %s", exc)
            self.status = f"error: {exc}"

    def close(self) -> None:
        self.supports_live = False
        self._stop.set()
        uri, self._uri = self._uri, None
        self._active, self._applied = [], False
        if uri:
            try:
                requests.delete(uri, timeout=TIMEOUT_S)
            except requests.RequestException:
                log.debug("Razer DELETE failed", exc_info=True)
