# RGB Sync

[![tests](https://github.com/drumplayer1234/rgb-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/drumplayer1234/rgb-sync/actions/workflows/tests.yml)

Set one static color on all your RGB keyboards and peripherals at once, across Corsair, Razer and NuPhy, from one small Windows app.

## Features

- Pick a color once and apply it to every detected device ("Apply to all").
- Works with Corsair (iCUE SDK), Razer (Chroma REST API) and NuPhy Halo V1 keyboards (USB HID).
- Per-brand status display and a "Rescan" button.
- "Restore original" puts every device back the way it was before RGB Sync touched it.
- Remembers your last color (`settings.json`, next to the app).
- Optional [Geometry Dash mode](#geometry-dash-mode): live game-reactive lighting (needs a small Geode mod).
- Pure Python 3.12 + tkinter; otherwise static colors only (no per-device colors).

## Supported hardware

| Brand | How | What is controlled | Requirements |
|-------|-----|--------------------|--------------|
| Corsair | iCUE SDK via [`cuesdk`](https://pypi.org/project/cuesdk/) | All devices iCUE exposes (keyboards, mice, headsets, fans, ...) | iCUE running, with Settings -> "Enable SDK" turned on |
| Razer | Chroma REST API | Chroma devices, set per category (the REST API cannot list physical devices) | Razer Synapse with the Chroma module, running |
| NuPhy | Raw USB HID (V1 firmware) | Halo65, Halo75, Halo96: backlight with the exact color, side lights and halo (see below) | Keyboard on a **USB cable** (not wireless / Bluetooth), **NuPhy Console closed** |

Notes:

- **NuPhy edge lights use 7 presets.** The backlight takes the exact RGB color (one slot of its Static palette is rewritten). The side lights and halo only offer 7 fixed colors (red, green, blue, yellow, purple, cyan, white), so the nearest preset is used. To leave them alone, set `SYNC_EDGE_LIGHTS = False` at the top of `rgbsync/backends/nuphy.py`.
- NuPhy **V2 / QMK** boards (and Air, Field, ...) are not supported yet; only V1-firmware Halo65/75/96.

## Install and run

Windows only. Needs Python 3.12.

```
pip install -r requirements.txt
python main.py
```

## Geometry Dash mode

Turns the lights into part of the game. Tick **Geometry Dash mode** in the app and, while you play, the lights follow the level:

- The background color of the level, brightened so dark levels still show, pulsing with the music.
- On a keyboard with live lighting (NuPhy Halo65): a progress bar across the keys in your player color, a white marker at your best percent, and WASD / arrows / space colored by the current game mode (cube, ship, wave, ...). Gravity flips fill the bar from the right.
- Death bursts (gold on a new best), a rainbow sweep when you complete a level, green for practice mode (with a flash on every checkpoint), dimmed lights when paused.
- The NuPhy side lights and halo show the level's difficulty. They are a slow saved setting, so they change once per level.
- Other devices (fans and so on) only get the level color and the full-device effects. When the game is not running, everything shows the color you picked in the app. Unticking the box applies that color again.

It needs the **Geode mod** in [`geode-mod/`](geode-mod/), which sends the game state to the app over UDP on `127.0.0.1:47831` (the app only listens on this computer). Install Geode and the mod as described in that folder's README. Geometry Dash 2.2091 needs **Geode 6 alpha** (`v6.0.0-alpha.1`). The wire format is in [`docs/gd-protocol.md`](docs/gd-protocol.md). If the app says "Port 47831 in use", another program (or a second copy of RGB Sync) already has the port.

**Try it without the game:** tick Geometry Dash mode and press **Test effects**. The app plays a scripted 40-second session (a hard level with deaths, a new best, practice mode, then a full run to the end) into its own listener. Or run the simulator from a terminal:

```
python -m rgbsync.gd.sim --print --speed 4   # no hardware: prints the scenes as text
python -m rgbsync.gd.sim --send              # sends a pass to a running RGB Sync window
```

## Restore

- **Corsair / Razer:** nothing is stored on the devices. Restore gives control back, so iCUE / Synapse show their own lighting again.
- **NuPhy:** the color settings live on the keyboard itself. Before the first change, RGB Sync saves the original lighting settings to `nuphy_backup_<model>.json` next to `main.py` (never overwritten; if the backup cannot be saved, nothing is written to the keyboard). Restore from the app's **Restore original** button, or with the keyboard on USB and NuPhy Console closed:

  ```
  python tools/restore_nuphy.py
  ```

  A NuPhy with no backup file was never changed, so there is nothing to restore.

## Tests

The tests use fake keyboards and need no hardware:

```
python tests/run_all.py
```

`tests/smoke_test.py` and `tests/nuphy_read_state.py` need real devices and are run by hand. `nuphy_read_state.py` only reads; `smoke_test.py` connects and lists devices without changing colors.

## Adding a brand backend

1. Create `rgbsync/backends/<brand>.py` with a subclass of `LightingBackend` (`name`, `connect()`, `status`, `list_devices()`, `set_color()`, `close()`).
2. Add the class to `BACKEND_CLASSES` in `rgbsync/manager.py`.

## Disclaimer

RGB Sync is an unofficial project. It is not affiliated with, endorsed by or sponsored by Corsair, Razer or NuPhy. All product names and trademarks belong to their respective owners.

The NuPhy support writes to the keyboard's settings using a reverse-engineered protocol. Use it at your own risk; try it first with a color you can verify, and keep the automatic backup.

## Credits

The NuPhy HID protocol is a Python port of the reverse-engineering work in [mishamyrt/nuga-lib](https://github.com/mishamyrt/nuga-lib) (MIT, Copyright (c) 2024 Mikhael Khrustik). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

[MIT](LICENSE), Copyright (c) 2026 drumplayer1234.
