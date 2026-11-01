# RGB Sync

Small Windows app (Python 3.12, tkinter) that sets one static color on all
detected RGB lighting devices across brands, using the official vendor SDKs.
Currently supported: Corsair (iCUE SDK), Razer (Chroma REST API) and NuPhy
Halo65/75/96 V1 keyboards (raw USB HID).

## Setup
```
pip install -r requirements.txt
```
- **Corsair:** iCUE must be running, with Settings -> "Enable SDK" (third-party control) turned on.
- **Razer:** install Razer Synapse with the Chroma module and keep it running.
- **NuPhy (Halo65/75/96, V1 firmware):** the keyboard must be connected by **USB cable**
  (wireless / Bluetooth mode cannot be controlled) and **NuPhy Console must be closed**.
  The main backlight is switched to its Static mode with the exact color (one slot of
  the Static color palette is rewritten); halo and side lights are left alone.
  This backend is a Python port of the HID protocol reverse-engineered in
  [mishamyrt/nuga-lib](https://github.com/mishamyrt/nuga-lib) (MIT License,
  Copyright (c) 2024 Mikhael Khrustik); see the header of `rgbsync/backends/nuphy.py`.
  It writes to the keyboard's lighting settings, so try it once with a color you can
  verify. Read-only check: `python tests/nuphy_read_state.py`.

  Before the first write, the keyboard's original lighting settings are saved to
  `nuphy_backup_<model>.json` next to `main.py` (never overwritten; if it can't be
  saved, nothing is written to the keyboard). To put them back, connect the keyboard
  by USB and run `python tools/restore_nuphy.py`.

## Run
```
python main.py
```
The last color is remembered in `settings.json` next to the app.
Headless check (changes no colors): `python tests/smoke_test.py`

## Adding a backend
1. Create `rgbsync/backends/<brand>.py` with a subclass of `LightingBackend`
   (`name`, `connect()`, `status`, `list_devices()`, `set_color()`, `close()`).
2. Add the class to `BACKEND_CLASSES` in `rgbsync/manager.py`.

## Known limits
- NuPhy: only V1-firmware Halo65/75/96 over USB (no Air/Field/V2/QMK boards, no wireless).
- Razer Chroma's REST API cannot enumerate physical devices, so Razer is listed per category.
- Static colors only; no effects or per-device colors.
