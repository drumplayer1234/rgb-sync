"""Read-only NuPhy check: connect, print the decoded lighting state.

REQUIRES A REAL NuPhy KEYBOARD (USB cable) so it is not part of
tests/run_all.py or CI. Run it by hand: python tests/nuphy_read_state.py

Sends only the two read commands (05 83 b6 .. and 05 88 b8 ..) and never
writes lighting data or calls set_color. Close NuPhy Console first.
"""
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rgbsync.backends.nuphy import NuPhyBackend  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    backend = NuPhyBackend()
    try:
        ok = backend.connect()
        print(f"[{backend.name}] connected={ok} status={backend.status!r}")
        if not ok:
            return 1
        for d in backend.list_devices():
            print(f"  device: {d.name} | {d.kind} | id={d.id}")
        state = backend.read_state()
        if state is None:
            print(f"read_state failed: {backend.status}")
            return 1
        print(json.dumps(state, indent=2))
    finally:
        backend.close()
    print("closed cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
