"""Corsair backend using the iCUE SDK v4 via the `cuesdk` package."""
from __future__ import annotations

import logging
import threading

from .base import Device, LightingBackend

log = logging.getLogger(__name__)

try:
    from cuesdk import (CorsairDeviceFilter, CorsairDeviceType, CorsairError,
                        CorsairLedColor, CorsairSessionState, CueSdk)
    _IMPORT_ERROR: str | None = None
except Exception as exc:  # package missing or DLL cannot load
    _IMPORT_ERROR = str(exc)

CONNECT_TIMEOUT_S = 5.0
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

    def close(self) -> None:
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
