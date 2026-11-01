"""Restore the original NuPhy lighting saved before the first write.

Connects only the NuPhy backend and writes ``nuphy_backup_<model>.json`` back
to each connected board. Use USB and close NuPhy Console first.
"""
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
        if not backend.connect():
            print(f"[{backend.name}] {backend.status}")
            return 1
        result = backend.restore_backup()
        print(result)
        ok = all("restored from" in line for line in result.splitlines())
        return 0 if ok else 1
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
