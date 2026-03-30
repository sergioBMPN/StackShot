"""
camera_controller.py
Wrapper around python-gphoto2 for Sony Alpha 7 III control via USB.
Provides: connection, config read/write, live view, capture, manual focus drive.
"""

import logging
import os
import platform
import subprocess
import threading
import time
from typing import Optional

import gphoto2 as gp

logger = logging.getLogger(__name__)


class CameraController:
    """Controls a Sony A7 III camera via USB using gphoto2."""

    # gphoto2 config paths for Sony A7 III in PC Remote mode
    CONFIG_ISO = "/main/imgsettings/iso"
    CONFIG_FNUMBER = "/main/capturesettings/f-number"
    CONFIG_SHUTTERSPEED = "/main/capturesettings/shutterspeed"
    CONFIG_WHITEBALANCE = "/main/imgsettings/whitebalance"

    # manualfocus range widget (Sony A7 III via PTP)
    # Range: -7.0 to 7.0, step 1.0
    # Negative = near, positive = far
    # IMPORTANT: lens AF/MF switch must be set to AF for electronic
    # focus control to work. Camera body focusmode stays "Manual".
    FOCUS_WIDGET = "manualfocus"
    FOCUS_MIN = -7.0
    FOCUS_MAX = 7.0

    # focalposition: read-only, 0 (nearest) to 100 (infinity)
    FOCAL_POSITION_WIDGET = "focalposition"

    def __init__(self):
        self._camera: Optional[gp.Camera] = None
        self._context: Optional[gp.Context] = None
        self._lock = threading.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ─── Connection ───────────────────────────────────────────────

    @staticmethod
    def _kill_macos_ptp_daemon():
        """
        Aggressively kill macOS PTPCamera / ptpd daemons.
        macOS auto-relaunches PTPCamera whenever a PTP camera connects,
        so we kill it in a loop until it stays dead.
        """
        if platform.system() != "Darwin":
            return
        import time
        proc_names = ("PTPCamera", "ptpd")
        for attempt in range(5):
            any_killed = False
            for proc_name in proc_names:
                try:
                    result = subprocess.run(
                        ["killall", "-9", proc_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        logger.info("Killed %s (attempt %d)", proc_name, attempt + 1)
                        any_killed = True
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass
            if not any_killed:
                logger.debug("No PTP daemons running (attempt %d)", attempt + 1)
                break
            time.sleep(0.5)
        # Final pause to let the OS fully release the USB device
        time.sleep(1)

    @staticmethod
    def list_cameras() -> list[tuple[str, str]]:
        """Return list of (model, port) for all detected cameras."""
        camera_list = gp.Camera.autodetect()
        result = [(name, port) for name, port in camera_list]
        return result

    def connect(self) -> str:
        """Detect and connect to the camera. Returns camera summary text."""
        import time
        with self._lock:
            if self._connected:
                return "Already connected"

            # On macOS, kill PTPCamera daemon that steals USB cameras
            self._kill_macos_ptp_daemon()

            # Log all detected cameras
            cameras = []
            try:
                cameras = self.list_cameras()
                logger.info("Detected cameras: %s", cameras if cameras else "(none)")
            except gp.GPhoto2Error as e:
                logger.warning("Auto-detect failed: %s", e)

            if not cameras:
                raise RuntimeError(
                    "No cameras detected.\n\n"
                    "Please check:\n"
                    "1. Camera is ON and connected via USB\n"
                    "2. Menu > Setup > USB Connection = 'PC Remote'\n"
                    "3. Wait a few seconds after plugging USB, then retry"
                )

            # Kill PTPCamera again right before init (macOS relaunches it)
            self._kill_macos_ptp_daemon()

            self._context = gp.Context()
            self._camera = gp.Camera()

            # Retry camera.init() — macOS may need multiple attempts
            last_error = None
            for attempt in range(3):
                try:
                    logger.info("camera.init() attempt %d...", attempt + 1)
                    self._camera.init(self._context)
                    last_error = None
                    break
                except gp.GPhoto2Error as e:
                    last_error = e
                    error_code = e.code if hasattr(e, 'code') else 0
                    logger.warning("camera.init() attempt %d failed: %s (code %s)",
                                   attempt + 1, e, error_code)
                    # Kill PTPCamera again and retry
                    self._kill_macos_ptp_daemon()
                    time.sleep(1)

            if last_error is not None:
                self._camera = None
                self._context = None
                error_msg = str(last_error)
                raise RuntimeError(
                    f"Could not connect to camera after 3 attempts.\n"
                    f"Last error: {error_msg}\n\n"
                    f"Try this in Terminal before running the app:\n"
                    f"  killall -9 PTPCamera 2>/dev/null; sleep 2; python3.13 main.py\n\n"
                    f"If it keeps failing, disable auto-launch:\n"
                    f"  defaults write com.apple.ImageCapture disableHotPlug -bool true\n"
                    f"  (reboot to apply, undo with: defaults delete com.apple.ImageCapture disableHotPlug)"
                ) from last_error

            self._connected = True

            # Set capture target to Memory card to avoid SDRAM buffer
            # overflow that causes [-7] I/O errors on burst/bracket
            try:
                config = self._camera.get_config(self._context)
                target_widget = config.get_child_by_name("capturetarget")
                current = target_widget.get_value()
                if current != "Memory card":
                    target_widget.set_value("Memory card")
                    self._camera.set_config(config, self._context)
                    logger.info("Set capturetarget: %s → Memory card", current)
            except gp.GPhoto2Error as e:
                logger.warning("Could not set capturetarget: %s", e)

            summary = self._camera.get_summary(self._context)
            model = str(summary)
            logger.info("Connected to camera: %s", model[:120])
            return model

    def disconnect(self):
        """Release the camera."""
        with self._lock:
            if self._camera and self._connected:
                try:
                    self._camera.exit(self._context)
                except gp.GPhoto2Error:
                    pass
                self._camera = None
                self._connected = False
                logger.info("Disconnected from camera")

    # ─── Configuration ────────────────────────────────────────────

    def _get_widget(self, config_path: str):
        """Get a config widget by path. Must hold self._lock."""
        config = self._camera.get_config(self._context)
        widget = config.get_child_by_name(config_path.split("/")[-1])
        return config, widget

    def get_config_choices(self, config_path: str) -> list[str]:
        """Return list of available choices for a config widget."""
        with self._lock:
            if not self._connected:
                return []
            try:
                _config, widget = self._get_widget(config_path)
                return [widget.get_choice(i) for i in range(widget.count_choices())]
            except gp.GPhoto2Error as e:
                logger.warning("Cannot read choices for %s: %s", config_path, e)
                return []

    def get_config_value(self, config_path: str) -> str:
        """Return current value of a config widget."""
        with self._lock:
            if not self._connected:
                return ""
            try:
                _config, widget = self._get_widget(config_path)
                return widget.get_value()
            except gp.GPhoto2Error as e:
                logger.warning("Cannot read %s: %s", config_path, e)
                return ""

    def set_config_value(self, config_path: str, value: str):
        """Set a config widget to a new value."""
        with self._lock:
            if not self._connected:
                return
            try:
                config = self._camera.get_config(self._context)
                widget = config.get_child_by_name(config_path.split("/")[-1])
                widget.set_value(value)
                self._camera.set_config(config, self._context)
                logger.info("Set %s = %s", config_path, value)
            except gp.GPhoto2Error as e:
                logger.error("Cannot set %s to %s: %s", config_path, value, e)
                raise

    # ─── Live View ────────────────────────────────────────────────

    def capture_preview_bytes(self) -> Optional[bytes]:
        """Capture a live view frame. Returns JPEG bytes or None."""
        with self._lock:
            if not self._connected:
                return None
            try:
                camera_file = gp.CameraFile()
                self._camera.capture_preview(camera_file, self._context)
                file_data = camera_file.get_data_and_size()
                return bytes(file_data)
            except gp.GPhoto2Error as e:
                logger.debug("Preview capture error: %s", e)
                return None

    # ─── Image Capture ────────────────────────────────────────────

    def _try_recover_io(self):
        """Attempt to recover from a USB I/O error without full reconnect."""
        logger.info("Attempting I/O recovery...")
        try:
            # Re-read config to reset the PTP session
            self._camera.get_config(self._context)
            logger.info("I/O recovery: config read OK")
            return True
        except gp.GPhoto2Error:
            pass
        # Heavier recovery: re-init the camera
        try:
            self._camera.exit(self._context)
            time.sleep(1)
            self._camera.init(self._context)
            # Re-set capturetarget after re-init
            try:
                config = self._camera.get_config(self._context)
                tw = config.get_child_by_name("capturetarget")
                tw.set_value("Memory card")
                self._camera.set_config(config, self._context)
            except gp.GPhoto2Error:
                pass
            logger.info("I/O recovery: re-init OK")
            return True
        except gp.GPhoto2Error as e:
            logger.error("I/O recovery failed: %s", e)
            return False

    def capture_image(self, download_path: Optional[str] = None) -> Optional[str]:
        """
        Trigger a still capture.
        If download_path is given, download the file to that local path.
        Returns the camera-side file path or the local path if downloaded.
        On I/O error, attempts recovery and one retry.
        """
        with self._lock:
            if not self._connected:
                return None
            for attempt in range(2):
                try:
                    file_path = self._camera.capture(
                        gp.GP_CAPTURE_IMAGE, self._context
                    )
                    camera_path = f"{file_path.folder}/{file_path.name}"
                    logger.info("Captured: %s", camera_path)

                    if download_path:
                        local_file = os.path.join(download_path, file_path.name)
                        camera_file = gp.CameraFile()
                        self._camera.file_get(
                            file_path.folder, file_path.name,
                            gp.GP_FILE_TYPE_NORMAL, camera_file, self._context
                        )
                        camera_file.save(local_file)
                        logger.info("Downloaded to: %s", local_file)
                        return local_file

                    return camera_path
                except gp.GPhoto2Error as e:
                    error_code = e.code if hasattr(e, 'code') else 0
                    logger.error("Capture failed (attempt %d): %s", attempt + 1, e)
                    # I/O error (-7): try recovery before giving up
                    if error_code == -7 and attempt == 0:
                        time.sleep(1)
                        if self._try_recover_io():
                            continue
                    raise

    # ─── Manual Focus Drive ───────────────────────────────────────

    def press_shutter(self):
        """Press the shutter button (start continuous capture in burst mode)."""
        with self._lock:
            if not self._connected:
                return
            try:
                widget = self._camera.get_single_config("capture", self._context)
                widget.set_value(2)
                self._camera.set_single_config("capture", widget, self._context)
                logger.info("Shutter pressed")
            except gp.GPhoto2Error as e:
                logger.error("Shutter press failed: %s", e)
                raise

    def release_shutter(self):
        """Release the shutter button (stop continuous capture)."""
        with self._lock:
            if not self._connected:
                return
            try:
                widget = self._camera.get_single_config("capture", self._context)
                widget.set_value(1)
                self._camera.set_single_config("capture", widget, self._context)
                logger.info("Shutter released")
            except gp.GPhoto2Error as e:
                logger.warning("Shutter release failed: %s", e)

    def move_focus(self, value: float):
        """
        Move focus by a relative amount using set_single_config.
        value: float in [-7.0, 7.0]  (negative=near, positive=far)
        The camera body must be in MF mode AND the lens AF/MF switch must be AF.
        Uses set_single_config which maps directly to
        ptp_sony_setdevicecontrolvalueb(ManualFocusAdjust, INT16).
        """
        value = max(self.FOCUS_MIN, min(self.FOCUS_MAX, value))
        with self._lock:
            if not self._connected:
                return
            try:
                widget = self._camera.get_single_config(
                    self.FOCUS_WIDGET, self._context
                )
                widget.set_value(value)
                self._camera.set_single_config(
                    self.FOCUS_WIDGET, widget, self._context
                )
                logger.debug("Focus moved: %s", value)
            except gp.GPhoto2Error as e:
                logger.error("Focus move failed (value=%s): %s", value, e)
                raise

    # ─── Focal Position (0-100 closed-loop) ───────────────────────

    def get_focal_position(self) -> Optional[int]:
        """
        Read the camera's focalposition widget (0=nearest, 100=infinity).
        Tries get_single_config first, falls back to full config tree.
        Returns None if the widget is not available.
        """
        with self._lock:
            if not self._connected:
                return None
            # Method 1: get_single_config (fast)
            try:
                widget = self._camera.get_single_config(
                    self.FOCAL_POSITION_WIDGET, self._context
                )
                return int(float(widget.get_value()))
            except (gp.GPhoto2Error, ValueError) as e:
                logger.debug("get_single_config focalposition failed: %s", e)
            # Method 2: full config tree (slower but more compatible)
            try:
                config = self._camera.get_config(self._context)
                widget = config.get_child_by_name(self.FOCAL_POSITION_WIDGET)
                return int(float(widget.get_value()))
            except (gp.GPhoto2Error, ValueError) as e:
                logger.debug("get_config focalposition failed: %s", e)
                return None

    def move_to_position(self, target: int, stop_event=None) -> int:
        """
        Move to a target focal position (0-100).
        Uses closed-loop with focalposition readback if available,
        otherwise falls back to open-loop (dead-reckoning) stepping.
        Returns the final focalposition reached, or -1 if unknown.
        """
        target = max(0, min(100, target))
        # Try closed-loop first
        current = self.get_focal_position()
        if current is not None:
            return self._move_to_position_closed(target, current, stop_event)
        # Fallback: open-loop dead-reckoning
        logger.warning("focalposition not available, using open-loop fallback")
        return self._move_to_position_open(target, stop_event)

    def _move_to_position_closed(self, target: int, current: int, stop_event=None) -> int:
        """Closed-loop move using focalposition readback."""
        for _ in range(80):
            if stop_event and stop_event.is_set():
                break
            delta = target - current
            if abs(delta) <= 1:
                logger.debug("Reached target %d (current %d)", target, current)
                return current
            # Choose magnitude based on distance
            if abs(delta) > 30:
                magnitude = 7.0
            elif abs(delta) > 10:
                magnitude = 3.0
            else:
                magnitude = 1.0
            value = magnitude if delta > 0 else -magnitude
            self.move_focus(value)
            time.sleep(0.3)
            new_pos = self.get_focal_position()
            if new_pos is None:
                # Lost readback mid-move, finish with open-loop
                logger.warning("Lost focalposition readback at %d", current)
                return current
            if new_pos == current:
                # No movement — try once more with smaller step
                if magnitude > 1.0:
                    value = 1.0 if delta > 0 else -1.0
                    self.move_focus(value)
                    time.sleep(0.3)
                    new_pos = self.get_focal_position()
                if new_pos is not None and new_pos == current:
                    logger.warning("Focus stuck at %d, target was %d", current, target)
                    return current
            if new_pos is not None:
                current = new_pos
        current = self.get_focal_position()
        return current if current is not None else -1

    def _move_to_position_open(self, target: int, stop_event=None) -> int:
        """
        Open-loop (dead-reckoning) move when focalposition isn't available.
        Estimates ~1 focalposition unit per magnitude-1 step with 0.3s delay.
        Returns -1 (position unknown without readback).
        """
        # We don't know current position; estimate steps from 0-100 range.
        # Drive to a known endpoint first, then step toward target.
        #
        # Strategy: drive to nearest end (0 or 100) by sending max-magnitude
        # steps, then step toward target.
        if target <= 50:
            # Drive to 0 first (near end)
            logger.info("Open-loop: driving to near end first")
            for _ in range(30):
                if stop_event and stop_event.is_set():
                    return -1
                self.move_focus(-7.0)
                time.sleep(0.25)
            # Now step toward target (positive direction)
            steps_needed = target  # ~1 unit per step at magnitude 1
            logger.info("Open-loop: stepping %d toward target %d", steps_needed, target)
            for _ in range(steps_needed):
                if stop_event and stop_event.is_set():
                    return -1
                self.move_focus(1.0)
                time.sleep(0.25)
        else:
            # Drive to 100 first (far end)
            logger.info("Open-loop: driving to far end first")
            for _ in range(30):
                if stop_event and stop_event.is_set():
                    return -1
                self.move_focus(7.0)
                time.sleep(0.25)
            # Now step toward target (negative direction)
            steps_needed = 100 - target
            logger.info("Open-loop: stepping %d toward target %d", steps_needed, target)
            for _ in range(steps_needed):
                if stop_event and stop_event.is_set():
                    return -1
                self.move_focus(-1.0)
                time.sleep(0.25)
        return -1

    # ─── Utility ──────────────────────────────────────────────────

    def get_focus_value(self) -> Optional[float]:
        """
        Read the current manualfocus widget value.
        Note: On Sony cameras this is the last command sent, not an absolute position.
        Returns None if unavailable.
        """
        with self._lock:
            if not self._connected:
                return None
            try:
                widget = self._camera.get_single_config(
                    self.FOCUS_WIDGET, self._context
                )
                return float(widget.get_value())
            except (gp.GPhoto2Error, ValueError) as e:
                logger.debug("Cannot read focus value: %s", e)
                return None

    def list_config_widgets(self) -> list[str]:
        """
        Return a list of all config widget names the camera exposes.
        Useful for diagnostics when a widget can't be found.
        """
        with self._lock:
            if not self._connected:
                return []
            try:
                config = self._camera.get_config(self._context)
                names = []
                self._walk_config(config, names)
                return names
            except gp.GPhoto2Error as e:
                logger.warning("Cannot list config widgets: %s", e)
                return []

    @staticmethod
    def _walk_config(widget, result: list, prefix: str = ""):
        """Recursively collect all widget names from the config tree."""
        name = widget.get_name()
        wtype = widget.get_type()
        path = f"{prefix}/{name}" if prefix else name
        # Only collect leaf widgets (non-section/window)
        if wtype not in (gp.GP_WIDGET_SECTION, gp.GP_WIDGET_WINDOW):
            try:
                val = widget.get_value()
            except gp.GPhoto2Error:
                val = "?"
            result.append(f"{path} = {val}")
        for i in range(widget.count_children()):
            child = widget.get_child(i)
            CameraController._walk_config(child, result, path)

    def get_all_params(self) -> dict:
        """
        Read current values and available choices for all main parameters.
        Returns dict with keys: iso, fnumber, shutterspeed, whitebalance.
        Each value is {'current': str, 'choices': list[str]}.
        """
        params = {}
        mapping = {
            "iso": self.CONFIG_ISO,
            "fnumber": self.CONFIG_FNUMBER,
            "shutterspeed": self.CONFIG_SHUTTERSPEED,
            "whitebalance": self.CONFIG_WHITEBALANCE,
        }
        for key, path in mapping.items():
            params[key] = {
                "current": self.get_config_value(path),
                "choices": self.get_config_choices(path),
            }
        return params
