"""Corsair backend using the iCUE SDK v4 via the `cuesdk` package."""
from __future__ import annotations

import logging
import threading
import time

from .base import Device, LightingBackend
from ..scene import Scene

log = logging.getLogger(__name__)

try:
    from cuesdk import (CorsairDeviceFilter, CorsairDeviceType, CorsairError,
                        CorsairLedColor, CorsairSessionState, CueSdk)
    _IMPORT_ERROR: str | None = None
except Exception as exc:  # package missing or DLL cannot load
    _IMPORT_ERROR = str(exc)

CONNECT_TIMEOUT_S = 5.0
#: Live mode: resend an unchanged color after this long (keepalive).
LIVE_KEEPALIVE_S = 1.0
_KEEP_ALIVE: list = []  # ctypes callbacks that must outlive disconnect()

_KIND_NAMES = {
    "CDT_Keyboard": "keyboard",
    "CDT_Mouse": "mouse",
    "CDT_Mousemat": "mousepad",
    "CDT_Headset": "headset",
    "CDT_HeadsetStand": "headset stand",
    "CDT_FanLedController": "fan controller",
    "CDT_LedController": "LED controller",
    "CDT_MemoryModule": "memory",
    "CDT_Cooler": "cooler",
    "CDT_Motherboard": "motherboard",
    "CDT_GraphicsCard": "graphics card",
    "CDT_Touchbar": "touchbar",
    "CDT_GameController": "game controller",
}


def _kind_name(device_type: int) -> str:
    """Map a CorsairDeviceType value to a friendly name."""
    for attr, label in _KIND_NAMES.items():
        if getattr(CorsairDeviceType, attr) == int(device_type):
            return label
    return "other"


def _err_text(err: object) -> str:
    return str(err).replace("CorsairError.CE_", "")


class CorsairBackend(LightingBackend):
    """Controls Corsair devices through the running iCUE application."""

    name = "Corsair"
    restored_text = "back to iCUE lighting"

    def __init__(self) -> None:
        self.status = "not connected"
        self._sdk: CueSdk | None = None
        self._connected = False
        self._lock = threading.Lock()
        self._clock = time.monotonic  # replaced in tests
        self._live_leds: list[tuple[str, str, list[int]]] = []  # id, model, led ids
        self._live_last: tuple[int, int, int] | None = None
        self._live_last_t = 0.0
        self._live_error = False

    def connect(self) -> bool:
        self.close()
        if _IMPORT_ERROR:
            self.status = f"error: cuesdk unavailable ({_IMPORT_ERROR})"
            return False
        try:
            sdk = CueSdk()
            ready = threading.Event()
            state: dict[str, object] = {"value": None}

            def on_state(evt) -> None:  # called from an SDK thread
                state["value"] = evt.state
                if evt.state in (CorsairSessionState.CSS_Connected,
                                 CorsairSessionState.CSS_ConnectionRefused,
                                 CorsairSessionState.CSS_Timeout):
                    ready.set()

            err = sdk.connect(on_state)
            if err != CorsairError.CE_Success:
                self.status = f"error: iCUE SDK connect failed ({_err_text(err)})"
                return False
            self._sdk = sdk  # keep reference so close() can disconnect
            ready.wait(CONNECT_TIMEOUT_S)
            value = state["value"]
            if value is not None and value == CorsairSessionState.CSS_Connected:
                self._connected = True
                self.supports_live = True
                self.status = "connected"
                return True
            if value is None:
                self.status = ("not found: iCUE not running or SDK disabled "
                               "in iCUE settings (no response)")
            else:
                self.status = ("not found: iCUE not running or SDK disabled "
                               f"in iCUE settings ({value})")
            self.close()
            return False
        except Exception as exc:
            log.exception("Corsair connect failed")
            self.status = f"error: {exc}"
            self.close()
            return False

    def _corsair_devices(self) -> list:
        """Raw device infos from the SDK ([] on any failure)."""
        if not (self._connected and self._sdk):
            return []
        devices, err = self._sdk.get_devices(
            CorsairDeviceFilter(device_type_mask=CorsairDeviceType.CDT_All))
        if err != CorsairError.CE_Success or devices is None:
            log.warning("Corsair get_devices failed: %s", _err_text(err))
            self.status = f"error: could not list devices ({_err_text(err)})"
            return []
        return devices

    def list_devices(self) -> list[Device]:
        with self._lock:
            return [Device(self.name, d.device_id, d.model or d.device_id,
                           _kind_name(d.type))
                    for d in self._corsair_devices()]

    def set_color(self, r: int, g: int, b: int) -> None:
        with self._lock:
            if not (self._connected and self._sdk):
                return
            failures = []
            for dev in self._corsair_devices():
                leds, err = self._sdk.get_led_positions(dev.device_id)
                if err != CorsairError.CE_Success or not leds:
                    failures.append(f"{dev.model}: positions {_err_text(err)}")
                    continue
                colors = [CorsairLedColor(id=led.id, r=r, g=g, b=b, a=255)
                          for led in leds]
                err = self._sdk.set_led_colors(dev.device_id, colors)
                if err != CorsairError.CE_Success:
                    failures.append(f"{dev.model}: set colors {_err_text(err)}")
            if failures:
                msg = "; ".join(failures)
                log.warning("Corsair set_color problems: %s", msg)
                self.status = f"connected, but some devices failed: {msg}"
            else:
                self.status = "connected"

    # -- live lighting -------------------------------------------------------
    # Every LED of every device shows ``scene.base``. Per-key colors
    # (``scene.keys``) are deliberately ignored: the user owns no Corsair
    # keyboard, and mapping key names to iCUE LED ids would be untestable.
    def start_live(self) -> None:
        """Cache each device's LED ids once so ``render`` never queries them."""
        with self._lock:
            self._live_last = None
            self._live_error = False
            leds_by_device: list[tuple[str, str, list[int]]] = []
            if not (self._connected and self._sdk):
                self._live_leds = []
                return
            try:
                for dev in self._corsair_devices():
                    leds, err = self._sdk.get_led_positions(dev.device_id)
                    if err != CorsairError.CE_Success or not leds:
                        log.warning("Corsair live: %s positions %s",
                                    dev.model, _err_text(err))
                        continue
                    leds_by_device.append(
                        (dev.device_id, dev.model, [led.id for led in leds]))
            except Exception as exc:
                log.exception("Corsair start_live failed")
                self.status = f"error: {exc}"
            self._live_leds = leds_by_device

    def render(self, scene: Scene) -> None:
        """Set every LED to ``scene.base``; unchanged colors are resent at 1 Hz."""
        try:
            rgb = tuple(max(0, min(255, int(c))) for c in scene.base)
            now = self._clock()
            with self._lock:
                if not (self._connected and self._sdk and self._live_leds):
                    return
                if (rgb == self._live_last
                        and now - self._live_last_t < LIVE_KEEPALIVE_S):
                    return
                r, g, b = rgb
                failures = []
                for device_id, model, led_ids in self._live_leds:
                    colors = [CorsairLedColor(id=i, r=r, g=g, b=b, a=255)
                              for i in led_ids]
                    err = self._sdk.set_led_colors(device_id, colors)
                    if err != CorsairError.CE_Success:
                        failures.append(f"{model}: set colors {_err_text(err)}")
                self._live_last, self._live_last_t = rgb, now
                if failures:
                    self._live_error = True
                    self.status = ("connected, but some devices failed: "
                                   + "; ".join(failures))
                elif self._live_error:
                    self._live_error = False
                    self.status = "connected"
        except Exception as exc:
            log.warning("Corsair render failed: %s", exc)
            self.status = f"error: {exc}"

    def stop_live(self) -> None:
        """Nothing to undo: iCUE keeps the last colors (use Restore to hand back)."""
        self._live_leds = []
        self._live_last = None

    def close(self) -> None:
        self.supports_live = False
        with self._lock:
            sdk, self._sdk, self._connected = self._sdk, None, False
        if sdk is None:
            return
        try:
            sdk.release_control(None)
        except Exception:
            log.debug("release_control failed", exc_info=True)
        # cuesdk's disconnect() drops its ctypes callback before the native
        # call; the SDK then fires a final "closed" event into freed memory and
        # the process dies (0xc000001d). Keep the callback alive for good.
        _KEEP_ALIVE.append(getattr(sdk, "session_state_changed_event_handler", None))
        try:
            sdk.disconnect()
        except Exception:
            log.debug("disconnect failed", exc_info=True)
