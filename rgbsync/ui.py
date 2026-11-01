"""tkinter UI: pick one color and apply it to every detected device."""
from __future__ import annotations

import json
import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import colorchooser, messagebox, ttk

from .manager import Manager

log = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"
DEFAULT_COLOR = "#ff0000"
PRESETS = {
    "Red": "#ff0000", "Orange": "#ff8000", "Yellow": "#ffff00",
    "Green": "#00ff00", "Cyan": "#00ffff", "Blue": "#0000ff",
    "Purple": "#8000ff", "White": "#ffffff",
}


def load_color() -> str:
    """Last used color from settings.json, or the default."""
    try:
        color = json.loads(SETTINGS_PATH.read_text("utf-8")).get("color", "")
        if isinstance(color, str) and len(color) == 7 and color[0] == "#":
            int(color[1:], 16)
            return color
    except Exception:
        pass
    return DEFAULT_COLOR


def save_color(color: str) -> None:
    """Persist the chosen color (failures are ignored)."""
    try:
        SETTINGS_PATH.write_text(json.dumps({"color": color}), "utf-8")
    except Exception:
        log.debug("could not save settings", exc_info=True)


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """'#rrggbb' -> (r, g, b)."""
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


class App:
    """Main window. Slow SDK work runs on a worker thread."""

    def __init__(self, root: tk.Tk, manager: Manager) -> None:
        self.root = root
        self.manager = manager
        self.color = load_color()
        self._jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._busy = False
        self._closing = False
        self._build()
        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.rescan()

    # --- layout ---------------------------------------------------------
    def _build(self) -> None:
        root = self.root
        root.title("RGB Sync")
        root.minsize(520, 460)
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        cols = ("brand", "device", "type")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=9)
        for col, text, width in (("brand", "Brand", 90), ("device", "Device", 280),
                                 ("type", "Type", 130)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width)
        self.tree.pack(fill="both", expand=True)

        self.status_frame = ttk.LabelFrame(frm, text="Backends", padding=6)
        self.status_frame.pack(fill="x", pady=8)
        self.status_labels: dict[str, ttk.Label] = {}
        for backend in self.manager.backends:
            label = ttk.Label(self.status_frame, text=f"{backend.name}: ...")
            label.pack(anchor="w")
            self.status_labels[backend.name] = label

        color_row = ttk.Frame(frm)
        color_row.pack(fill="x", pady=4)
        ttk.Label(color_row, text="Color:").pack(side="left")
        self.preview = tk.Label(color_row, width=8, bg=self.color, relief="sunken")
        self.preview.pack(side="left", padx=6)
        ttk.Button(color_row, text="Pick color...", command=self.pick_color).pack(
            side="left")

        swatches = ttk.Frame(frm)
        swatches.pack(fill="x", pady=4)
        for hexcolor in PRESETS.values():
            tk.Button(swatches, bg=hexcolor, width=3, relief="raised",
                      command=lambda c=hexcolor: self.set_color(c)).pack(
                side="left", padx=2)

        actions = ttk.Frame(frm)
        actions.pack(fill="x", pady=8)
        self.apply_btn = ttk.Button(actions, text="Apply to all", command=self.apply)
        self.apply_btn.pack(side="left")
        self.rescan_btn = ttk.Button(actions, text="Rescan", command=self.rescan)
        self.rescan_btn.pack(side="left", padx=6)
        self.restore_btn = ttk.Button(actions, text="Restore original",
                                      command=self.restore)
        self.restore_btn.pack(side="left")
        self.message = ttk.Label(actions, text="")
        self.message.pack(side="left", padx=10)

    # --- color ----------------------------------------------------------
    def set_color(self, color: str) -> None:
        """Update the preview and remember the color."""
        self.color = color
        self.preview.configure(bg=color)
        save_color(color)

    def pick_color(self) -> None:
        """Open the system color chooser."""
        _, hexcolor = colorchooser.askcolor(color=self.color, parent=self.root)
        if hexcolor:
            self.set_color(hexcolor)

    # --- worker thread --------------------------------------------------
    def _work_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:
                log.exception("worker job failed")

    def _submit(self, work: Callable[[], None], message: str,
                on_done: Callable[[], None] | None = None) -> None:
        """Run `work` on the worker, then refresh the UI on the main thread
        (and call `on_done` there, if given)."""
        if self._busy or self._closing:
            return
        self._busy = True
        self._set_buttons("disabled")
        self.message.configure(text=message)

        def job() -> None:
            try:
                work()
                devices = self.manager.devices()
            except Exception:
                log.exception("job failed")
                devices = []
            statuses = self.manager.statuses()
            if not self._closing:
                self.root.after(0, lambda: self._done(devices, statuses, on_done))

        self._jobs.put(job)

    def _done(self, devices, statuses,
              on_done: Callable[[], None] | None = None) -> None:
        """Main-thread refresh after a worker job."""
        self._busy = False
        self._set_buttons("normal")
        self.message.configure(text="")
        self.tree.delete(*self.tree.get_children())
        for d in devices:
            self.tree.insert("", "end", values=(d.backend, d.name, d.kind))
        for name, label in self.status_labels.items():
            label.configure(text=f"{name}: {statuses.get(name, '?')}")
        if on_done:
            on_done()

    def _set_buttons(self, state: str) -> None:
        self.apply_btn.configure(state=state)
        self.rescan_btn.configure(state=state)
        self.restore_btn.configure(state=state)

    def rescan(self) -> None:
        """Reconnect all backends and refresh the device list."""
        self._submit(self.manager.connect_all, "Scanning...")

    def apply(self) -> None:
        """Apply the current color to every device."""
        r, g, b = hex_to_rgb(self.color)
        self._submit(lambda: self.manager.apply_color(r, g, b), "Applying...")

    def restore(self) -> None:
        """Ask, then put every device back to its original lighting."""
        if self._busy or self._closing:
            return
        if not messagebox.askyesno(
                "Restore original",
                "Put every device back to how it was before RGB Sync changed it?",
                parent=self.root):
            return
        results: dict[str, str] = {}

        def work() -> None:
            results.update(self.manager.restore_all())

        def show() -> None:  # runs on the main thread after the refresh
            text = "\n".join(f"{name}: {msg}" for name, msg in results.items())
            messagebox.showinfo("Restore original", text or "Nothing to restore.",
                                parent=self.root)

        self._submit(work, "Restoring...", show)

    # --- shutdown -------------------------------------------------------
    def on_close(self) -> None:
        """Release SDK control, then close the window."""
        self._closing = True
        self._jobs.put(self.manager.close_all)
        self._jobs.put(None)
        self._worker.join(timeout=5)
        self.root.destroy()


def run() -> None:
    """Create the window and run the Tk main loop."""
    root = tk.Tk()
    App(root, Manager())
    root.mainloop()
