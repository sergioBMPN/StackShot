"""
gui.py
Tkinter GUI for Sony A7 III remote control.
Live view, parameter adjustment, and focus bracketing interface.
"""

import io
import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from PIL import Image, ImageTk

from camera_controller import CameraController
from focus_bracket import FocusBracket

logger = logging.getLogger(__name__)

# Refresh interval for live view (ms). ~15 fps = 66ms, ~10 fps = 100ms
LIVEVIEW_INTERVAL_MS = 80


class App(tk.Tk):
    """Main application window."""

    def __init__(self):
        super().__init__()
        logger.debug("=== App.__init__ START ===")
        logger.debug("Tk version: %s", self.tk.call("info", "patchlevel"))
        logger.debug("Python: %s", sys.version)
        logger.debug("Platform: %s", sys.platform)

        self.title("Sony A7 III — Remote Control")
        self.geometry("1200x750")
        self.minsize(900, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        logger.debug("Window geometry set to 1200x750, minsize 900x600")

        # Core objects
        self._controller = CameraController()
        self._bracket = FocusBracket(self._controller)
        self._bracket.on_progress = self._on_bracket_progress
        self._bracket.on_complete = self._on_bracket_complete
        self._bracket.on_error = self._on_bracket_error

        # Live view state
        self._liveview_running = False
        self._liveview_image: Optional[ImageTk.PhotoImage] = None

        # Download path
        self._download_path = os.path.expanduser("~/Pictures/SonyBracket")

        self._build_ui()
        self._set_ui_state_disconnected()

        # Force geometry computation — fixes blank window on macOS
        self.update_idletasks()
        logger.debug("After update_idletasks — window size: %sx%s",
                     self.winfo_width(), self.winfo_height())
        logger.debug("Window mapped: %s, viewable: %s",
                     self.winfo_ismapped(), self.winfo_viewable())
        # Schedule a delayed size check after the window is displayed
        self.after(500, self._debug_sizes)

    # ══════════════════════════════════════════════════════════════
    # UI CONSTRUCTION
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        logger.debug("=== _build_ui START ===")

        # Main layout: grid with 2 columns (live view | controls)
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        logger.debug("Grid configured: col0 weight=3, col1 weight=1, row0 weight=1")

        # ── Left: Live View ──
        lv_frame = ttk.LabelFrame(self, text="Live View")
        lv_frame.grid(row=0, column=0, sticky="nsew", padx=(5, 2), pady=5)
        logger.debug("lv_frame gridded at (0,0)")

        self._lv_label = ttk.Label(lv_frame, anchor=tk.CENTER)
        self._lv_label.pack(fill=tk.BOTH, expand=True)
        logger.debug("lv_label packed")

        # ── Right: Controls (scrollable) ──
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.grid(row=0, column=1, sticky="nsew", padx=(2, 5), pady=5)
        ctrl_frame.rowconfigure(0, weight=1)
        ctrl_frame.columnconfigure(0, weight=1)
        logger.debug("ctrl_frame gridded at (0,1)")

        self._ctrl_canvas = tk.Canvas(ctrl_frame, highlightthickness=0, bg="#f0f0f0")
        scrollbar = ttk.Scrollbar(ctrl_frame, orient=tk.VERTICAL, command=self._ctrl_canvas.yview)
        self._scroll_frame = ttk.Frame(self._ctrl_canvas)

        self._scroll_frame.bind(
            "<Configure>",
            lambda e: (
                self._ctrl_canvas.configure(scrollregion=self._ctrl_canvas.bbox("all")),
                logger.debug("scroll_frame <Configure>: w=%s h=%s, bbox=%s",
                             e.width, e.height, self._ctrl_canvas.bbox("all"))
            )
        )
        # Propagate canvas width to inner frame so widgets fill horizontally
        self._ctrl_canvas.bind(
            "<Configure>",
            lambda e: (
                self._ctrl_canvas.itemconfigure(self._canvas_window_id, width=e.width),
                logger.debug("ctrl_canvas <Configure>: w=%s h=%s", e.width, e.height)
            )
        )
        self._canvas_window_id = self._ctrl_canvas.create_window(
            (0, 0), window=self._scroll_frame, anchor=tk.NW
        )
        self._ctrl_canvas.configure(yscrollcommand=scrollbar.set)
        self._ctrl_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        logger.debug("Canvas and scrollbar gridded inside ctrl_frame")

        parent = self._scroll_frame

        # ── Connection ──
        conn_frame = ttk.LabelFrame(parent, text="Connection")
        conn_frame.pack(fill=tk.X, padx=5, pady=5)
        logger.debug("conn_frame packed")

        self._btn_connect = ttk.Button(conn_frame, text="Connect", command=self._on_connect)
        self._btn_connect.pack(side=tk.LEFT, padx=5, pady=5)
        self._btn_disconnect = ttk.Button(conn_frame, text="Disconnect", command=self._on_disconnect)
        self._btn_disconnect.pack(side=tk.LEFT, padx=5, pady=5)
        self._lbl_status = ttk.Label(conn_frame, text="Disconnected", foreground="red")
        self._lbl_status.pack(side=tk.LEFT, padx=10)
        logger.debug("Connection widgets created")

        # ── Camera Parameters ──
        params_frame = ttk.LabelFrame(parent, text="Camera Settings")
        params_frame.pack(fill=tk.X, padx=5, pady=5)

        self._param_combos: dict[str, ttk.Combobox] = {}
        param_labels = {
            "iso": "ISO",
            "fnumber": "Aperture (f/)",
            "shutterspeed": "Shutter Speed",
            "whitebalance": "White Balance",
        }
        for i, (key, label) in enumerate(param_labels.items()):
            ttk.Label(params_frame, text=label).grid(row=i, column=0, sticky=tk.W, padx=5, pady=2)
            combo = ttk.Combobox(params_frame, state="readonly", width=18)
            combo.grid(row=i, column=1, padx=5, pady=2)
            combo.bind("<<ComboboxSelected>>", lambda e, k=key: self._on_param_change(k))
            self._param_combos[key] = combo

        ttk.Button(params_frame, text="Refresh", command=self._refresh_params).grid(
            row=len(param_labels), column=0, columnspan=2, pady=5
        )

        # ── Capture ──
        cap_frame = ttk.LabelFrame(parent, text="Capture")
        cap_frame.pack(fill=tk.X, padx=5, pady=5)

        self._btn_capture = ttk.Button(cap_frame, text="📷  Take Photo", command=self._on_capture)
        self._btn_capture.pack(padx=5, pady=5, fill=tk.X)

        # ── Focus Control ──
        focus_frame = ttk.LabelFrame(parent, text="Focus Control")
        focus_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(focus_frame, text="Step size:").grid(row=0, column=0, padx=5, pady=2)
        self._step_size_var = tk.IntVar(value=1)
        step_combo = ttk.Combobox(
            focus_frame, textvariable=self._step_size_var,
            values=[1, 2, 3], state="readonly", width=5
        )
        step_combo.grid(row=0, column=1, padx=5, pady=2)
        step_combo.set("1")
        ttk.Label(focus_frame, text="(1=fine, 3=coarse)").grid(row=0, column=2, padx=2)

        btn_row = ttk.Frame(focus_frame)
        btn_row.grid(row=1, column=0, columnspan=3, pady=5)
        self._btn_near = ttk.Button(btn_row, text="◀ Near", command=self._on_focus_near)
        self._btn_near.pack(side=tk.LEFT, padx=5)
        self._btn_far = ttk.Button(btn_row, text="Far ▶", command=self._on_focus_far)
        self._btn_far.pack(side=tk.LEFT, padx=5)

        self._lbl_focus_pos = ttk.Label(focus_frame, text="Position: 0")
        self._lbl_focus_pos.grid(row=2, column=0, columnspan=3, pady=2)

        # ── Focus Bracket ──
        bracket_frame = ttk.LabelFrame(parent, text="Focus Bracket")
        bracket_frame.pack(fill=tk.X, padx=5, pady=5)

        points_row = ttk.Frame(bracket_frame)
        points_row.pack(fill=tk.X, padx=5, pady=5)
        self._btn_set_a = ttk.Button(points_row, text="Set Point A", command=self._on_set_point_a)
        self._btn_set_a.pack(side=tk.LEFT, padx=5)
        self._btn_set_b = ttk.Button(points_row, text="Set Point B", command=self._on_set_point_b)
        self._btn_set_b.pack(side=tk.LEFT, padx=5)

        self._lbl_points = ttk.Label(bracket_frame, text="A: — | B: — | Distance: —")
        self._lbl_points.pack(padx=5)

        photos_row = ttk.Frame(bracket_frame)
        photos_row.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(photos_row, text="Num photos:").pack(side=tk.LEFT, padx=5)
        self._num_photos_var = tk.IntVar(value=10)
        self._spin_photos = ttk.Spinbox(
            photos_row, from_=2, to=200, textvariable=self._num_photos_var, width=6
        )
        self._spin_photos.pack(side=tk.LEFT, padx=5)

        # Download options
        dl_frame = ttk.Frame(bracket_frame)
        dl_frame.pack(fill=tk.X, padx=5, pady=2)
        self._download_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            dl_frame, text="Download photos to:", variable=self._download_var
        ).pack(side=tk.LEFT)
        self._lbl_dl_path = ttk.Label(
            dl_frame, text=self._download_path, foreground="blue", cursor="hand2"
        )
        self._lbl_dl_path.pack(side=tk.LEFT, padx=5)
        self._lbl_dl_path.bind("<Button-1>", self._on_choose_folder)

        # Start / Stop
        action_row = ttk.Frame(bracket_frame)
        action_row.pack(fill=tk.X, padx=5, pady=5)
        self._btn_start_bracket = ttk.Button(
            action_row, text="▶  Start Bracket", command=self._on_start_bracket
        )
        self._btn_start_bracket.pack(side=tk.LEFT, padx=5)
        self._btn_stop_bracket = ttk.Button(
            action_row, text="■  Stop", command=self._on_stop_bracket, state=tk.DISABLED
        )
        self._btn_stop_bracket.pack(side=tk.LEFT, padx=5)
        self._btn_reset_bracket = ttk.Button(
            action_row, text="Reset", command=self._on_reset_bracket
        )
        self._btn_reset_bracket.pack(side=tk.LEFT, padx=5)

        # Progress
        self._bracket_progress = ttk.Progressbar(bracket_frame, mode="determinate")
        self._bracket_progress.pack(fill=tk.X, padx=5, pady=2)
        self._lbl_bracket_status = ttk.Label(bracket_frame, text="")
        self._lbl_bracket_status.pack(padx=5, pady=2)

        logger.debug("=== _build_ui END — all widgets created ===")

    # ══════════════════════════════════════════════════════════════
    # DEBUG HELPERS
    # ══════════════════════════════════════════════════════════════

    def _debug_sizes(self):
        """Log all widget sizes 500ms after startup to diagnose blank UI."""
        logger.debug("=== DELAYED SIZE CHECK (500ms after init) ===")
        logger.debug("Root window: %sx%s (requested: %sx%s)",
                     self.winfo_width(), self.winfo_height(),
                     self.winfo_reqwidth(), self.winfo_reqheight())
        logger.debug("Root mapped: %s, viewable: %s",
                     self.winfo_ismapped(), self.winfo_viewable())
        logger.debug("Root geometry: %s", self.winfo_geometry())
        # Children of root
        for child in self.winfo_children():
            cname = child.winfo_class()
            logger.debug("  Child %-20s  actual=%sx%s  req=%sx%s  mapped=%s",
                         cname,
                         child.winfo_width(), child.winfo_height(),
                         child.winfo_reqwidth(), child.winfo_reqheight(),
                         child.winfo_ismapped())
            # One more level deep
            for sub in child.winfo_children():
                sname = sub.winfo_class()
                logger.debug("    Sub %-18s  actual=%sx%s  req=%sx%s  mapped=%s",
                             sname,
                             sub.winfo_width(), sub.winfo_height(),
                             sub.winfo_reqwidth(), sub.winfo_reqheight(),
                             sub.winfo_ismapped())
        # Canvas and scroll_frame specifically
        logger.debug("ctrl_canvas: actual=%sx%s  req=%sx%s",
                     self._ctrl_canvas.winfo_width(), self._ctrl_canvas.winfo_height(),
                     self._ctrl_canvas.winfo_reqwidth(), self._ctrl_canvas.winfo_reqheight())
        logger.debug("scroll_frame: actual=%sx%s  req=%sx%s  children=%d",
                     self._scroll_frame.winfo_width(), self._ctrl_canvas.winfo_height(),
                     self._scroll_frame.winfo_reqwidth(), self._scroll_frame.winfo_reqheight(),
                     len(self._scroll_frame.winfo_children()))
        logger.debug("lv_label: actual=%sx%s",
                     self._lv_label.winfo_width(), self._lv_label.winfo_height())
        logger.debug("scrollregion: %s", self._ctrl_canvas.cget("scrollregion"))
        logger.debug("=== END SIZE CHECK ===")

    # ══════════════════════════════════════════════════════════════
    # UI STATE
    # ══════════════════════════════════════════════════════════════

    def _set_ui_state_disconnected(self):
        self._btn_connect.config(state=tk.NORMAL)
        self._btn_disconnect.config(state=tk.DISABLED)
        self._btn_capture.config(state=tk.DISABLED)
        self._btn_near.config(state=tk.DISABLED)
        self._btn_far.config(state=tk.DISABLED)
        self._btn_set_a.config(state=tk.DISABLED)
        self._btn_set_b.config(state=tk.DISABLED)
        self._btn_start_bracket.config(state=tk.DISABLED)
        for combo in self._param_combos.values():
            combo.config(state=tk.DISABLED)

    def _set_ui_state_connected(self):
        self._btn_connect.config(state=tk.DISABLED)
        self._btn_disconnect.config(state=tk.NORMAL)
        self._btn_capture.config(state=tk.NORMAL)
        self._btn_near.config(state=tk.NORMAL)
        self._btn_far.config(state=tk.NORMAL)
        self._btn_set_a.config(state=tk.NORMAL)
        self._btn_set_b.config(state=tk.NORMAL)
        self._btn_start_bracket.config(state=tk.NORMAL)
        for combo in self._param_combos.values():
            combo.config(state="readonly")

    # ══════════════════════════════════════════════════════════════
    # CONNECTION
    # ══════════════════════════════════════════════════════════════

    def _on_connect(self):
        self._btn_connect.config(state=tk.DISABLED)
        self._lbl_status.config(text="Connecting...", foreground="orange")
        self.update_idletasks()

        def do_connect():
            try:
                summary = self._controller.connect()
                self.after(0, self._connect_success, summary)
            except Exception as e:
                self.after(0, self._connect_fail, str(e))

        threading.Thread(target=do_connect, daemon=True).start()

    def _connect_success(self, summary: str):
        self._lbl_status.config(text="Connected ✓", foreground="green")
        self._set_ui_state_connected()
        self._refresh_params()
        self._start_liveview()

    def _connect_fail(self, error: str):
        self._lbl_status.config(text="Connection failed", foreground="red")
        self._btn_connect.config(state=tk.NORMAL)
        messagebox.showerror("Connection Error", f"Could not connect to camera:\n{error}")

    def _on_disconnect(self):
        self._stop_liveview()
        self._bracket.reset()
        self._controller.disconnect()
        self._lbl_status.config(text="Disconnected", foreground="red")
        self._set_ui_state_disconnected()
        self._lv_label.config(image="")

    # ══════════════════════════════════════════════════════════════
    # LIVE VIEW
    # ══════════════════════════════════════════════════════════════

    def _start_liveview(self):
        self._liveview_running = True
        self._poll_liveview()

    def _stop_liveview(self):
        self._liveview_running = False

    def _poll_liveview(self):
        if not self._liveview_running:
            return

        def fetch():
            data = self._controller.capture_preview_bytes()
            if data and self._liveview_running:
                self.after(0, self._display_frame, data)
            if self._liveview_running:
                self.after(LIVEVIEW_INTERVAL_MS, self._poll_liveview)

        threading.Thread(target=fetch, daemon=True).start()

    def _display_frame(self, jpeg_data: bytes):
        try:
            image = Image.open(io.BytesIO(jpeg_data))
            # Scale to fit the label
            lw = self._lv_label.winfo_width()
            lh = self._lv_label.winfo_height()
            if lw > 1 and lh > 1:
                image.thumbnail((lw, lh), Image.LANCZOS)
            self._liveview_image = ImageTk.PhotoImage(image)
            self._lv_label.config(image=self._liveview_image)
        except Exception as e:
            logger.debug("Frame decode error: %s", e)

    # ══════════════════════════════════════════════════════════════
    # CAMERA PARAMETERS
    # ══════════════════════════════════════════════════════════════

    def _refresh_params(self):
        if not self._controller.connected:
            return

        def do_refresh():
            params = self._controller.get_all_params()
            self.after(0, self._update_param_combos, params)

        threading.Thread(target=do_refresh, daemon=True).start()

    def _update_param_combos(self, params: dict):
        for key, combo in self._param_combos.items():
            if key in params:
                choices = params[key]["choices"]
                current = params[key]["current"]
                combo["values"] = choices
                if current in choices:
                    combo.set(current)
                elif choices:
                    combo.set(choices[0])

    def _on_param_change(self, key: str):
        combo = self._param_combos[key]
        value = combo.get()
        if not value:
            return

        config_map = {
            "iso": CameraController.CONFIG_ISO,
            "fnumber": CameraController.CONFIG_FNUMBER,
            "shutterspeed": CameraController.CONFIG_SHUTTERSPEED,
            "whitebalance": CameraController.CONFIG_WHITEBALANCE,
        }
        path = config_map[key]

        def do_set():
            try:
                self._controller.set_config_value(path, value)
            except Exception as e:
                self.after(0, messagebox.showerror, "Setting Error", str(e))

        threading.Thread(target=do_set, daemon=True).start()

    # ══════════════════════════════════════════════════════════════
    # CAPTURE
    # ══════════════════════════════════════════════════════════════

    def _on_capture(self):
        dl_path = self._download_path if self._download_var.get() else None
        if dl_path:
            os.makedirs(dl_path, exist_ok=True)

        def do_capture():
            try:
                result = self._controller.capture_image(dl_path)
                self.after(0, lambda: self._lbl_bracket_status.config(
                    text=f"Captured: {os.path.basename(result) if result else '?'}"
                ))
            except Exception as e:
                self.after(0, messagebox.showerror, "Capture Error", str(e))

        threading.Thread(target=do_capture, daemon=True).start()

    # ══════════════════════════════════════════════════════════════
    # FOCUS CONTROL
    # ══════════════════════════════════════════════════════════════

    def _on_focus_near(self):
        self._bracket.step_size = self._step_size_var.get()

        def do_move():
            try:
                self._bracket.move_focus_near()
                self.after(0, self._update_focus_display)
            except Exception as e:
                self.after(0, messagebox.showerror, "Focus Error", str(e))

        threading.Thread(target=do_move, daemon=True).start()

    def _on_focus_far(self):
        self._bracket.step_size = self._step_size_var.get()

        def do_move():
            try:
                self._bracket.move_focus_far()
                self.after(0, self._update_focus_display)
            except Exception as e:
                self.after(0, messagebox.showerror, "Focus Error", str(e))

        threading.Thread(target=do_move, daemon=True).start()

    def _update_focus_display(self):
        self._lbl_focus_pos.config(text=f"Position: {self._bracket._current_position}")
        self._update_points_label()

    # ══════════════════════════════════════════════════════════════
    # FOCUS BRACKET
    # ══════════════════════════════════════════════════════════════

    def _on_set_point_a(self):
        self._bracket.step_size = self._step_size_var.get()
        self._bracket.set_point_a()
        self._update_points_label()
        self._update_focus_display()

    def _on_set_point_b(self):
        try:
            self._bracket.set_point_b()
            self._update_points_label()
        except RuntimeError as e:
            messagebox.showwarning("Focus Bracket", str(e))

    def _update_points_label(self):
        a_text = "0" if self._bracket.point_a is not None else "—"
        b_text = str(self._bracket.point_b.step_count) if self._bracket.point_b else "—"
        dist = self._bracket.total_steps
        dist_text = str(dist) if dist is not None else "—"
        self._lbl_points.config(text=f"A: {a_text} | B: {b_text} | Distance: {dist_text} steps")

    def _on_choose_folder(self, event=None):
        path = filedialog.askdirectory(initialdir=self._download_path, title="Select download folder")
        if path:
            self._download_path = path
            self._lbl_dl_path.config(text=path)

    def _on_start_bracket(self):
        if self._bracket.point_a is None or self._bracket.point_b is None:
            messagebox.showwarning("Focus Bracket", "Set both Point A and Point B first.")
            return

        num_photos = self._num_photos_var.get()
        dl_path = None
        if self._download_var.get():
            dl_path = self._download_path
            os.makedirs(dl_path, exist_ok=True)

        self._bracket.step_size = self._step_size_var.get()
        self._bracket_progress["value"] = 0
        self._bracket_progress["maximum"] = num_photos
        self._btn_start_bracket.config(state=tk.DISABLED)
        self._btn_stop_bracket.config(state=tk.NORMAL)

        try:
            self._bracket.start(num_photos, dl_path)
        except Exception as e:
            messagebox.showerror("Focus Bracket", str(e))
            self._btn_start_bracket.config(state=tk.NORMAL)
            self._btn_stop_bracket.config(state=tk.DISABLED)

    def _on_stop_bracket(self):
        self._bracket.stop()
        self._btn_stop_bracket.config(state=tk.DISABLED)

    def _on_reset_bracket(self):
        self._bracket.reset()
        self._update_points_label()
        self._update_focus_display()
        self._bracket_progress["value"] = 0
        self._lbl_bracket_status.config(text="")
        self._btn_start_bracket.config(state=tk.NORMAL if self._controller.connected else tk.DISABLED)
        self._btn_stop_bracket.config(state=tk.DISABLED)

    # ── Bracket callbacks (called from bracket thread) ──

    def _on_bracket_progress(self, current: int, total: int, message: str):
        self.after(0, self._update_bracket_ui, current, total, message)

    def _on_bracket_complete(self, photos_taken: int):
        self.after(0, self._bracket_finished, f"Bracket complete: {photos_taken} photos taken.")

    def _on_bracket_error(self, error_msg: str):
        self.after(0, self._bracket_finished, f"Error: {error_msg}")
        self.after(0, messagebox.showerror, "Bracket Error", error_msg)

    def _update_bracket_ui(self, current: int, total: int, message: str):
        self._bracket_progress["value"] = current
        self._bracket_progress["maximum"] = total
        self._lbl_bracket_status.config(text=message)

    def _bracket_finished(self, message: str):
        self._lbl_bracket_status.config(text=message)
        self._btn_start_bracket.config(state=tk.NORMAL)
        self._btn_stop_bracket.config(state=tk.DISABLED)

    # ══════════════════════════════════════════════════════════════
    # CLEANUP
    # ══════════════════════════════════════════════════════════════

    def _on_close(self):
        self._stop_liveview()
        if self._bracket.is_running:
            self._bracket.stop()
        if self._controller.connected:
            self._controller.disconnect()
        self.destroy()
