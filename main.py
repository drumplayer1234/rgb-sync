"""Entry point: `python main.py`."""
import logging

from rgbsync.ui import run

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run()
