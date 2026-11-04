# Geometry Dash mod <-> RGB Sync protocol

The Geode mod (`geode-mod/`) sends JSON datagrams over UDP to `127.0.0.1:47831`.
One message per datagram, UTF-8, under 512 bytes. The mod only sends and the app
only listens, on loopback only. Fire and forget: a lost packet must never matter,
which is why `state` repeats everything the lights need.

Every message has `"v": 1` (protocol version) and `"t"` (type).

## `state`: sent about 30 times a second while a level is playing
| Field | Type | Meaning |
|---|---|---|
| `pct` | float 0-100 | progress through the level |
| `best` | float 0-100 | best percent in this mode (normal or practice) |
| `mode` | string | `cube` `ship` `ball` `ufo` `wave` `robot` `spider` `swing` |
| `flip` | bool | gravity is upside down |
| `speed` | float | player speed multiplier, 1.0 when unknown |
| `bg` | [r,g,b] | current background color channel |
| `p1` | [r,g,b] | player primary color |
| `pulse` | float 0-16 | raw music level from the game's meter (not normalized; the app auto-scales it), 0 when unavailable |
| `pulse2` | float 0-16 | second meter, raw, 0 when unavailable |
| `pulse3` | float 0-16 | third meter, raw, 0 when unavailable |
| `accent` | [r,g,b] or null | the level's most active vivid color right now, chosen by the mod from all its color channels; null when nothing on screen is vivid |
| `practice` | bool | practice mode |
| `paused` | bool | game is paused |

## Events: sent once when they happen
| `t` | Extra fields | Meaning |
|---|---|---|
| `level_start` | `name`, `stars`, `difficulty`, `platformer` | Entered a level. `difficulty` is one of `auto` `easy` `normal` `hard` `harder` `insane` `demon` `unrated` |
| `death` | `pct`, `new_best` (bool) | The player died (never for the invisible anti-cheat spike at level start) |
| `restart` | | A new attempt started |
| `complete` | `new_best` (bool) | Level finished |
| `checkpoint` | | Practice checkpoint placed |
| `level_exit` | | Left the level |

No `state` for 3 seconds means the game is closed or in a menu.
