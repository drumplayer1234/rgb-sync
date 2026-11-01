"""Headless smoke test: connect, print status and devices. Changes no colors."""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rgbsync.manager import Manager  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    manager = Manager()
    try:
        results = manager.connect_all()
        for backend in manager.backends:
            print(f"[{backend.name}] connected={results[backend.name]} "
                  f"status={backend.status!r}")
        devices = manager.devices()
        print(f"{len(devices)} device(s):")
        for d in devices:
            print(f"  {d.backend:8} | {d.name} | {d.kind} | id={d.id}")
    finally:
        manager.close_all()
    print("closed cleanly")


if __name__ == "__main__":
    main()
