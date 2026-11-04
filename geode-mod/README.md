# RGB Sync (Geode mod)

A Geometry Dash mod that reports game state to the RGB Sync desktop app over UDP.

- Target: Geometry Dash **2.2091**, Windows (Steam), Geode **v6.0.0-alpha.1**.
- Mod id: `drumplayer1234.rgb-sync`

## What it sends

JSON datagrams to `127.0.0.1:47831`, one message each, about 30 `state` messages a second
while a level is playing, plus one-off events (`level_start`, `death`, `restart`,
`complete`, `checkpoint`, `level_exit`). The exact format is in
[`docs/gd-protocol.md`](../docs/gd-protocol.md). The mod only sends; it never reads from
the network, touches files, or changes gameplay.

## Install

1. Install Geode for Geometry Dash 2.2091 (Geode `v6.0.0-alpha.1`).
2. Download `rgb-sync-geode` from the latest run of the **geode-mod** workflow in the
   repository's Actions tab, unzip it, and put `drumplayer1234.rgb-sync.geode` in
   `Geometry Dash/geode/mods`.
3. Start the RGB Sync desktop app, then start the game.

## Build

CI builds it (`.github/workflows/geode-mod.yml`). To build locally you need the Geode SDK
`v6.0.0-alpha.1`, Geode CLI 4.0.0 or newer, and a C++ toolchain set up as described in the
Geode docs:

```
cd geode-mod
geode build
```

## Notes

- `pulse` follows GD's own music metering, which GD only switches on for levels that use
  it (music-driven effects such as audio-scaled objects or pulses). Otherwise `pulse` stays 0.
- Platformer levels have no percent: `pct` and `best` are 0.
- The level name is cut to 64 characters; non-ASCII characters are replaced with `?`.
