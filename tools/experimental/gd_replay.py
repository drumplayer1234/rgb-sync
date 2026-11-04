"""Replay a recorded Geometry Dash capture through the lighting effects, offline.

No hardware, no game, no network: the capture's messages go through the real
``GDListener.handle`` parser (with an injected clock) into ``GDEffects`` (with
injected time), and one scene per frame is measured. Prints statistics of the
``Scene.base`` color: saturation and brightness distribution, distinct hues, the
share of near-grey frames (saturation < 0.25), and the beat level distribution.

Flicker numbers: "large jumps" is how many times per minute the base brightness
(largest channel, 0-1) moves by more than 25% (0.25) between two frames, and "hue
changes" is how many times per minute the base hue moves by more than 20 degrees
and then settles. A capture made before the mod sent ``accent`` cannot show accent
flicker, so a second, synthetic replay of the same capture is also run in which the
``accent`` alternates between two hues every 0.2 s (``--no-synthetic`` skips it).

With ``--old-ref REF`` (default ``HEAD``) the same capture is replayed through the
effects module as it is in git at REF (loaded into a throw-away module, nothing is
changed in the repo) and both sets of numbers are printed side by side; use
``--no-compare`` to skip it.

Capture format: JSONL, one ``{"at": seconds, "msg": {...}}`` per line, ``msg``
being a protocol message (docs/gd-protocol.md).

Usage:  python tools/experimental/gd_replay.py <capture.jsonl> [--fps 30]
"""
from __future__ import annotations

import argparse
import colorsys
import json
import pathlib
import subprocess
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import rgbsync.gd  # noqa: E402,F401  (the package the old module is loaded into)
from rgbsync.gd.effects import GDEffects  # noqa: E402
from rgbsync.gd.listener import GDListener  # noqa: E402

NEAR_GREY = 0.25
JUMP = 0.25                 # a frame-to-frame brightness move bigger than this is a large jump
HUE_MOVE_DEGREES = 20.0     # a hue this far from the settled one starts a hue change
HUE_SETTLE_DEGREES = 3.0    # ... which ends when the hue stops moving (over SETTLE_SECONDS)
SETTLE_SECONDS = 0.1
SYNTHETIC_ACCENTS = ((255, 90, 0), (0, 200, 255))      # orange / cyan, 0.2 s each
SYNTHETIC_PERIOD = 0.2
VISIBLE_VALUE = 0.15        # darker frames are too coarse (8-bit) to have a reliable hue
HUE_BIN_DEGREES = 15


def load_capture(path: str) -> list[tuple[float, dict]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rec = json.loads(line)
                rows.append((float(rec["at"]), rec["msg"]))
    rows.sort(key=lambda r: r[0])
    return rows


def load_effects_from_git(ref: str):
    """The ``GDEffects`` class of ``rgbsync/gd/effects.py`` as of git ``ref``."""
    src = subprocess.run(["git", "show", f"{ref}:rgbsync/gd/effects.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    mod = types.ModuleType("rgbsync.gd._effects_old")
    mod.__package__ = "rgbsync.gd"          # lets the relative imports resolve
    exec(compile(src, f"<git {ref}:rgbsync/gd/effects.py>", "exec"), mod.__dict__)
    return mod.GDEffects


def replay(effects_cls, rows: list[tuple[float, dict]], fps: float = 30.0) -> list[dict]:
    """One dict per frame: base color, and the beat level / raw pulse when known."""
    clock = [0.0]
    listener = GDListener(port=0, clock=lambda: clock[0])     # never started: no socket
    effects = effects_cls(idle_color=(255, 0, 0))
    frames = []
    if not rows:
        return frames
    start, end = rows[0][0], rows[-1][0]
    i, n = 0, len(rows)
    step = 1.0 / fps
    t = start
    while t <= end + 1e-9:
        while i < n and rows[i][0] <= t:
            clock[0] = rows[i][0]
            listener.handle(json.dumps(rows[i][1]).encode("utf-8"))
            i += 1
        clock[0] = t
        state = listener.state()
        effects.update(state, listener.drain_events(), t)
        scene = effects.scene(t)
        beat = effects.beat_level(t) if hasattr(effects, "beat_level") else None
        frames.append({"t": t, "base": scene.base, "beat": beat, "pulse": state.pulse,
                       "connected": state.connected})
        t += step
    return frames


def with_alternating_accent(rows: list[tuple[float, dict]], period: float = SYNTHETIC_PERIOD,
                            colors=SYNTHETIC_ACCENTS) -> list[tuple[float, dict]]:
    """The same capture with an ``accent`` on every state message, switching between
    ``colors`` every ``period`` seconds (a worst-case flickering accent)."""
    out = []
    t0 = rows[0][0]
    for at, msg in rows:
        if msg.get("t") == "state":
            msg = dict(msg, accent=list(colors[int((at - t0) / period) % len(colors)]))
        out.append((at, msg))
    return out


def _hue_dist(a: float, b: float) -> float:
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d) * 360.0


def flicker(frames: list[dict], fps: float) -> dict:
    """Large brightness jumps and hue changes per minute of the base color."""
    minutes = max(1e-9, (frames[-1]["t"] - frames[0]["t"]) / 60.0) if len(frames) > 1 else 1e-9
    vals = [max(f["base"]) / 255.0 for f in frames]
    jumps = sum(1 for a, b in zip(vals, vals[1:]) if abs(b - a) > JUMP)
    hues = []                                   # hue of the visible frames (else the last one)
    last = None
    for f in frames:
        h, s_, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in f["base"]))
        if s_ >= NEAR_GREY and v >= VISIBLE_VALUE:
            last = h
        hues.append(last)
    lag = max(1, int(round(SETTLE_SECONDS * fps)))
    anchor, moving, changes = None, False, 0
    for i, h in enumerate(hues):
        if h is None:
            continue
        if anchor is None:
            anchor = h
        elif not moving:
            if _hue_dist(h, anchor) > HUE_MOVE_DEGREES:
                moving, changes = True, changes + 1
        else:
            prev = hues[max(0, i - lag)]
            if prev is not None and _hue_dist(h, prev) < HUE_SETTLE_DEGREES:
                moving, anchor = False, h
    return {"jumps_per_min": jumps / minutes, "jumps": jumps,
            "hue_changes_per_min": changes / minutes, "hue_changes": changes,
            "minutes": minutes}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def summarize(frames: list[dict], fps: float = 30.0) -> dict:
    hsv = [colorsys.rgb_to_hsv(*(c / 255.0 for c in f["base"])) for f in frames]
    sats = [h[1] for h in hsv]
    vals = [h[2] for h in hsv]
    colored = [h for h in hsv if h[1] >= NEAR_GREY and h[2] >= VISIBLE_VALUE]
    bins = {int(h[0] * 360 // HUE_BIN_DEGREES) for h in colored}
    beats = [f["beat"] for f in frames if f["beat"] is not None]
    return {
        "frames": len(frames),
        "sat": [percentile(sats, p) for p in (0.05, 0.25, 0.5, 0.75, 0.95)],
        "val": [percentile(vals, p) for p in (0.05, 0.25, 0.5, 0.75, 0.95)],
        "mean_sat": sum(sats) / len(sats), "mean_val": sum(vals) / len(vals),
        "hue_bins": len(bins), "distinct_colors": len({f["base"] for f in frames}),
        "near_grey_pct": 100.0 * sum(1 for s in sats if s < NEAR_GREY) / len(sats),
        "beat": [percentile(beats, p) for p in (0.05, 0.25, 0.5, 0.75, 0.95)] if beats else None,
        "beat_mean": sum(beats) / len(beats) if beats else None,
        "beat_high_pct": 100.0 * sum(1 for b in beats if b > 0.5) / len(beats) if beats else None,
        "beat_zero_pct": 100.0 * sum(1 for b in beats if b == 0.0) / len(beats) if beats else None,
        "pulse": [percentile([f["pulse"] for f in frames], p) for p in (0.05, 0.25, 0.5, 0.75, 0.95)],
        **flicker(frames, fps),
    }


def _row(label: str, values: list[float]) -> str:
    return f"  {label:<26}" + "  ".join(f"{v:5.2f}" for v in values) + "   (p5 p25 p50 p75 p95)"


def print_flicker(s: dict) -> None:
    print(f"  large jumps (base brightness > {JUMP:.0%} between frames): "
          f"{s['jumps_per_min']:.1f}/min ({s['jumps']} in {s['minutes'] * 60:.0f} s)")
    print(f"  base hue changes (> {HUE_MOVE_DEGREES:.0f} degrees): "
          f"{s['hue_changes_per_min']:.1f}/min ({s['hue_changes']})")


def print_summary(name: str, s: dict) -> None:
    print(f"== {name}: {s['frames']} frames")
    print(_row("base saturation", s["sat"]))
    print(_row("base brightness (value)", s["val"]))
    print(f"  mean saturation {s['mean_sat']:.2f}, mean brightness {s['mean_val']:.2f}")
    print(f"  near-grey frames (sat < {NEAR_GREY}): {s['near_grey_pct']:.1f}%")
    print(f"  distinct hues: {s['hue_bins']} ({HUE_BIN_DEGREES} degree bins, frames with sat >= "
          f"{NEAR_GREY} and value >= {VISIBLE_VALUE}); distinct base colors: {s['distinct_colors']}")
    if s["beat"] is not None:
        print(_row("beat level", s["beat"]))
        print(f"  beat > 0.5 in {s['beat_high_pct']:.1f}% of frames, exactly 0 in "
              f"{s['beat_zero_pct']:.1f}%, mean {s['beat_mean']:.2f}")
    else:
        print(_row("(no beat; raw pulse used)", s["pulse"]))
    print_flicker(s)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture", help="gd_capture.jsonl")
    ap.add_argument("--fps", type=float, default=30.0, help="frames per second to sample")
    ap.add_argument("--old-ref", default="HEAD", help="git ref of the effects to compare with")
    ap.add_argument("--no-compare", action="store_true")
    ap.add_argument("--no-synthetic", action="store_true",
                    help="skip the replay with an alternating accent")
    args = ap.parse_args()

    rows = load_capture(args.capture)
    if not rows:
        sys.exit("empty capture")
    print(f"capture: {len(rows)} messages over {rows[-1][0] - rows[0][0]:.1f} s")
    old_cls = None
    if not args.no_compare:
        try:
            old_cls = load_effects_from_git(args.old_ref)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"(cannot load effects from git {args.old_ref}: {exc})")
    runs = [("capture", rows)]
    if not args.no_synthetic:
        runs.append((f"synthetic: accent alternating {SYNTHETIC_ACCENTS[0]} / "
                     f"{SYNTHETIC_ACCENTS[1]} every {SYNTHETIC_PERIOD} s",
                     with_alternating_accent(rows)))
    for label, run_rows in runs:
        print(f"\n##### {label}")
        new = summarize(replay(GDEffects, run_rows, args.fps), args.fps)
        old = (summarize(replay(old_cls, run_rows, args.fps), args.fps)
               if old_cls is not None else None)
        if old is not None:
            print_summary(f"OLD ({args.old_ref})", old)
        print_summary("NEW (working tree)", new)
        if old is not None:
            print(f"near-grey frames: {old['near_grey_pct']:.1f}% -> {new['near_grey_pct']:.1f}%")
            print(f"large jumps/min: {old['jumps_per_min']:.1f} -> {new['jumps_per_min']:.1f}; "
                  f"hue changes/min: {old['hue_changes_per_min']:.1f} -> "
                  f"{new['hue_changes_per_min']:.1f}")


if __name__ == "__main__":
    main()
