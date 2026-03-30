"""
camera_controller.py
Wrapper around python-gphoto2 for Sony Alpha 7 III control via USB.
Provides: connection, config read/write, live view, capture, manual focus drive.
"""

import io
import logging
import os
import platform
import subprocess
import threading
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
    FOCUS_WIDGET = "manualfocus"
    FOCUS_MIN = -7.0
    FOCUS_MAX = 7.0

    # Step size mapping: UI level -> manualfocus value magnitude
    FOCUS_STEP_MAP = {
        1: 1.0,   # fine
        2: 3.0,   # medium
        3: 7.0,   # coarse
    }

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

    def capture_image(self, download_path: Optional[str] = None) -> Optional[str]:
        """
        Trigger a still capture.
        If download_path is given, download the file to that local path.
        Returns the camera-side file path or the local path if downloaded.
        """
        with self._lock:
            if not self._connected:
                return None
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
                logger.error("Capture failed: %s", e)
                raise

    # ─── Manual Focus Drive ───────────────────────────────────────

    def move_focus(self, value: float):
        """
        Move focus by a relative amount.
        value: float in [-7.0, 7.0]  (negative=near, positive=far)
        The camera must be in MF or DMF mode.
        """
        value = max(self.FOCUS_MIN, min(self.FOCUS_MAX, value))
        with self._lock:
            if not self._connected:
                return
            try:
                config = self._camera.get_config(self._context)
                widget = config.get_child_by_name(self.FOCUS_WIDGET)
                widget.set_value(value)
                self._camera.set_config(config, self._context)
                logger.debug("Focus moved: %s", value)
            except gp.GPhoto2Error as e:
                logger.error("Focus move failed (value=%s): %s", value, e)
                raise

    def move_focus_steps(self, direction: str, count: int, step_size: int = 1):
        """
        Move focus multiple steps in a direction.
        direction: 'near' or 'far'
        count: number of steps to take
        step_size: 1 (fine), 2 (medium), or 3 (coarse)
        """
        magnitude = self.FOCUS_STEP_MAP.get(step_size, 1.0)
        value = -magnitude if direction == "near" else magnitude
        for _ in range(count):
            self.move_focus(value)
            import time
            time.sleep(0.05)  # let the lens settle between steps

    # ─── Utility ──────────────────────────────────────────────────

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
