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

from .gd.effects import BEAT_STRENGTH, GDEffects, make_source
from .gd.listener import GDListener
from .gd.sim import FakeGDSender
from .live import LiveEngine
from .manager import Manager

log = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"
DEFAULT_COLOR = "#ff0000"
PRESETS = {
    "Red": "#ff0000", "Orange": "#ff8000", "Yellow": "#ffff00",
    "Green": "#00ff00", "Cyan": "#00ffff", "Blue": "#0000ff",
    "Purple": "#8000ff", "White": "#ffffff",
}


def _read_settings() -> dict:
    """The whole settings.json as a dict ({} when missing or unreadable)."""
    try:
        data = json.loads(SETTINGS_PATH.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _update_settings(**changes) -> None:
    """Merge ``changes`` into settings.json, keeping every other key (failures are
    ignored)."""
    try:
        data = _read_settings()
        data.update(changes)
        SETTINGS_PATH.write_text(json.dumps(data), "utf-8")
    except Exception:
        log.debug("could not save settings", exc_info=True)


def load_color() -> str:
    """Last used color from settings.json, or the default."""
    try:
        color = _read_settings().get("color", "")
        if isinstance(color, str) and len(color) == 7 and color[0] == "#":
            int(color[1:], 16)
            return color
    except Exception:
        pass
    return DEFAULT_COLOR


def save_color(color: str) -> None:
    """Persist the chosen color (other settings are kept; failures are ignored)."""
    _update_settings(color=color)


def load_beat_strength() -> float:
    """Last used Geometry Dash "Beat flash" strength (0.0-1.0), or the default."""
    try:
        value = float(_read_settings()["beat_strength"])
        if 0.0 <= value <= 1.0:          # also rejects NaN
            return value
    except Exception:
        pass
    return BEAT_STRENGTH


def save_beat_strength(value: float) -> None:
    """Persist the "Beat flash" strength (other settings are kept; failures are
    ignored)."""
    _update_settings(beat_strength=round(min(1.0, max(0.0, float(value))), 3))


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """'#rrggbb' -> (r, g, b)."""
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


class App:
    """Main window. Slow SDK work runs on a worker thread."""

    def __init__(self, root: tk.Tk, manager: Manager) -> None:
        self.root = root
        self.manager = manager
        self.color = load_color()
        self.beat_strength = load_beat_strength()
        self._jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._busy = False
        self._closing = False
        # Geometry Dash mode (all touched on the main thread only)
        self._gd_listener: GDListener | None = None
        self._gd_effects: GDEffects | None = None
        self._gd_engine = LiveEngine(manager)
        self._gd_error = ""
        self._gd_sender: FakeGDSender | None = None
        self._gd_test_thread: threading.Thread | None = None
        self._gd_after: str | None = None
        self._beat_save_after: str | None = None
        self._build()
        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.rescan()
        self._gd_after = root.after(500, self._gd_tick)

    # --- layout ---------------------------------------------------------
    def _build(self) -> None:
        root = self.root
        root.title("RGB Sync")
        root.minsize(520, 540)
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
        self._build_gd(frm)

    def _build_gd(self, frm: ttk.Frame) -> None:
        box = ttk.LabelFrame(frm, text="Geometry Dash", padding=6)
        box.pack(fill="x", pady=4)
        top = ttk.Frame(box)
        top.pack(fill="x")
        self.gd_var = tk.BooleanVar(value=False)
        self.gd_check = ttk.Checkbutton(top, text="Geometry Dash mode",
                                        variable=self.gd_var,
                                        command=self._on_gd_toggle)
        self.gd_check.pack(side="left")
        self.gd_test_btn = ttk.Button(top, text="Test effects", command=self.gd_test)
        self.gd_test_btn.pack(side="right")
        self.gd_status = ttk.Label(box, text="Off")
        self.gd_status.pack(anchor="w", pady=(4, 0))
        beat_row = ttk.Frame(box)
        beat_row.pack(fill="x", pady=(4, 0))
        ttk.Label(beat_row, text="Beat flash:").pack(side="left")
        self.beat_var = tk.DoubleVar(value=round(self.beat_strength * 100))
        self.beat_scale = ttk.Scale(beat_row, from_=0, to=100, orient="horizontal",
                                    variable=self.beat_var,
                                    command=self._on_beat_scale)
        self.beat_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.beat_label = ttk.Label(beat_row, width=5, anchor="e",
                                    text=f"{round(self.beat_strength * 100)}%")
        self.beat_label.pack(side="left")
        ttk.Label(box, text="How hard the lights pulse on the music (0 = steady).",
                  foreground="gray40").pack(anchor="w")

    # --- color ----------------------------------------------------------
    def set_color(self, color: str) -> None:
        """Update the preview and remember the color."""
        self.color = color
        self.preview.configure(bg=color)
        save_color(color)
        if self._gd_effects is not None:     # shown whenever the game is not running
            self._gd_effects.idle_color = hex_to_rgb(color)

    def pick_color(self) -> None:
        """Open the system color chooser."""
        _, hexcolor = colorchooser.askcolor(color=self.color, parent=self.root)
        if hexcolor:
            self.set_color(hexcolor)

    def _on_beat_scale(self, _value: str = "") -> None:
        """The "Beat flash" slider moved (main thread): apply live, save shortly after."""
        percent = round(min(100.0, max(0.0, float(self.beat_var.get()))))
        self.beat_strength = percent / 100.0
        self.beat_label.configure(text=f"{percent}%")
        if self._gd_effects is not None:
            self._gd_effects.beat_strength = self.beat_strength
        if self._beat_save_after is not None:
            self.root.after_cancel(self._beat_save_after)
        self._beat_save_after = self.root.after(400, self._save_beat)

    def _save_beat(self) -> None:
        """Write the slider value to settings.json (once it stops moving)."""
        self._beat_save_after = None
        save_beat_strength(self.beat_strength)

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
        self._gd_buttons(state)

    def rescan(self) -> None:
        """Reconnect all backends and refresh the device list."""
        if self._busy or self._closing:
            return
        self._stop_gd(apply=False)          # reconnecting would pull devices from under it
        self._submit(self.manager.connect_all, "Scanning...")

    def apply(self) -> None:
        """Apply the current color to every device."""
        if self._busy or self._closing:
            return
        self._stop_gd(apply=False)          # this write replaces the idle color anyway
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
        self._stop_gd(apply=False)          # the engine and a restore must not fight
        results: dict[str, str] = {}

        def work() -> None:
            results.update(self.manager.restore_all())

        def show() -> None:  # runs on the main thread after the refresh
            text = "\n".join(f"{name}: {msg}" for name, msg in results.items())
            messagebox.showinfo("Restore original", text or "Nothing to restore.",
                                parent=self.root)

        self._submit(work, "Restoring...", show)

    # --- Geometry Dash --------------------------------------------------
    def _on_gd_toggle(self) -> None:
        """The "Geometry Dash mode" checkbox was clicked."""
        if self.gd_var.get():
            self._start_gd()
        else:
            self._stop_gd(apply=True)

    def _start_gd(self) -> bool:
        """Listen for the game and run the live engine. False when it cannot start."""
        if self._gd_listener is not None:
            return True
        if self._closing:
            return False
        listener = GDListener()
        if not listener.start():
            self.gd_var.set(False)
            self._gd_error = (f"Port {listener.port} in use"
                              if listener.status == "port in use"
                              else f"Geometry Dash: {listener.status}")
            self._gd_refresh()
            return False
        self._gd_error = ""
        effects = GDEffects(idle_color=hex_to_rgb(self.color),
                            beat_strength=self.beat_strength)
        self._gd_listener, self._gd_effects = listener, effects
        self._gd_engine.start(make_source(listener, effects))
        self.gd_var.set(True)
        self._gd_refresh()
        return True

    def _stop_gd(self, apply: bool) -> None:
        """Stop the test sender, the engine and the listener. With ``apply``,
        also put the current color on every device (on the worker thread) so
        no game frame stays on the lights."""
        sender = self._gd_sender
        if sender is not None:
            sender.stop()
        listener = self._gd_listener
        if listener is None:
            return
        self._gd_listener = self._gd_effects = None
        self._gd_engine.stop()
        listener.stop()
        self.gd_var.set(False)
        self._gd_error = ""
        if apply and not self._closing:
            r, g, b = hex_to_rgb(self.color)
            self._jobs.put(lambda: self.manager.apply_color(r, g, b))
        self._gd_refresh()

    def gd_test(self) -> None:
        """Play the simulator's scripted game once into our own listener."""
        thread = self._gd_test_thread
        if self._closing or self._busy or (thread is not None and thread.is_alive()):
            return
        if not self._start_gd() or self._gd_listener is None:
            return
        sender = FakeGDSender(port=self._gd_listener.port)
        self._gd_sender = sender
        self._gd_test_thread = threading.Thread(target=sender.run, daemon=True,
                                                name="gd-test")
        self._gd_test_thread.start()
        self._gd_refresh()

    def _gd_buttons(self, state: str) -> None:
        """Enable/disable the Geometry Dash widgets (state: "normal"/"disabled")."""
        thread = self._gd_test_thread
        testing = thread is not None and thread.is_alive()
        self.gd_check.configure(state=state)
        self.gd_test_btn.configure(
            state="disabled" if testing else state,
            text="Testing..." if testing else "Test effects")

    def _gd_refresh(self) -> None:
        """Update the status line and the test button."""
        listener = self._gd_listener
        if listener is None:
            text = self._gd_error or "Off"
        elif listener.status != "listening":
            text = f"Geometry Dash: {listener.status}"
        else:
            st = listener.state()
            if st.connected:
                extra = f"{st.pct:.0f}%, {st.mode}"
                if st.practice:
                    extra += ", practice"
                if st.paused:
                    extra += ", paused"
                text = f"Playing: {st.level_name or 'unnamed level'} ({extra})"
            else:
                text = "Waiting for Geometry Dash..."
        self.gd_status.configure(text=text)
        self._gd_buttons("disabled" if self._busy else "normal")

    def _gd_tick(self) -> None:
        """Every 500 ms, on the main thread: refresh the status line."""
        self._gd_after = None
        if self._closing:
            return
        self._gd_refresh()
        self._gd_after = self.root.after(500, self._gd_tick)

    # --- shutdown -------------------------------------------------------
    def on_close(self) -> None:
        """Stop Geometry Dash mode, release SDK control, then close the window."""
        self._stop_gd(apply=False)
        if self._beat_save_after is not None:      # a slider change not saved yet
            self.root.after_cancel(self._beat_save_after)
            self._save_beat()
        if self._gd_after is not None:
            self.root.after_cancel(self._gd_after)
            self._gd_after = None
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
