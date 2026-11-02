"""Hardware demo of the live lighting layer (RGB Sync LiveEngine).

For 6 seconds: base dark blue (0, 0, 60) on every live-capable device, the keys
Q W E R bright cyan, and a 1 Hz red sine pulse on SPACE (NuPhy Halo65 only;
Corsair/Razer just show the dark blue). ``edge`` is left None, so nothing is
written to flash. Then the engine stops (NuPhy gets one final solid frame in
the base color) and the backends are closed.

Needs real hardware. Without --yes it only connects and lists what is live.
Usage:  python tools/experimental/live_demo.py [--yes] [--seconds 6]
"""
from __future__ import annotations

import argparse
import logging
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from rgbsync.live import LiveEngine  # noqa: E402
from rgbsync.manager import Manager  # noqa: E402
from rgbsync.scene import Scene  # noqa: E402

BASE = (0, 0, 60)
CYAN = (0, 255, 255)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true",
                        help="actually send lighting to the hardware")
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    manager = Manager()
    try:
        manager.connect_all()
        for backend in manager.backends:
            print(f"[{backend.name}] status={backend.status!r} "
                  f"supports_live={backend.supports_live}")
        live = manager.live_backends()
        print("live backends:", [b.name for b in live] or "none")
        if not args.yes:
            print("Dry run (no --yes): nothing sent.")
            return 0
        if not live:
            print("Nothing to drive.")
            return 1

        t0 = time.monotonic()

        def source(now: float) -> Scene:
            pulse = (math.sin(2 * math.pi * (now - t0)) + 1) / 2   # 1 Hz, 0..1
            keys = {k: CYAN for k in "QWER"}
            keys["SPACE"] = (round(255 * pulse), 0, 0)
            return Scene(base=BASE, keys=keys, edge=None)

        engine = LiveEngine(manager, fps=30)
        engine.start(source)
        time.sleep(args.seconds)
        stats = engine.stats          # read while running
        engine.stop()
        print(f"engine ran {args.seconds:g} s")
        print(f"  achieved fps : {stats['fps']:.1f}")
        print(f"  frames       : {stats['frames']}")
        for name, ms in stats["render_ms"].items():
            print(f"  last render  : {name} {ms:.2f} ms")
        print(f"  source errors: {stats['source_errors']}")
        for backend in manager.backends:
            print(f"[{backend.name}] status after run: {backend.status!r}")
        print("stopped; NuPhy keys hold the final base-color frame "
              "(use the Restore button to hand back saved lighting).")
    finally:
        manager.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
