# RGB Sync

Small Windows app (Python 3.12, tkinter) that sets one static color on all
detected RGB lighting devices across brands, using the official vendor SDKs.
Currently supported: Corsair (iCUE SDK) and Razer (Chroma REST API).

## Setup
```
pip install -r requirements.txt
```
- **Corsair:** iCUE must be running, with Settings -> "Enable SDK" (third-party control) turned on.
- **Razer:** install Razer Synapse with the Chroma module and keep it running.

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
- NuPhy is not supported in v1: it has no vendor SDK. It would need a QMK/VIA raw-HID backend.
- Razer Chroma's REST API cannot enumerate physical devices, so Razer is listed per category.
- Static colors only; no effects or per-device colors.
