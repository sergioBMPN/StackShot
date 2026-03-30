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

    # manualfocusdrive step values (Sony)
    FOCUS_STEPS = {
        "Near 1": "Near 1",  # finest near
        "Near 2": "Near 2",
        "Near 3": "Near 3",  # coarsest near
        "Far 1": "Far 1",    # finest far
        "Far 2": "Far 2",
        "Far 3": "Far 3",    # coarsest far
        "None": "None",
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
        """Kill macOS PTPCamera daemon that grabs USB cameras before gphoto2."""
        if platform.system() != "Darwin":
            return
        for proc_name in ("PTPCamera", "ptpd"):
            try:
                subprocess.run(
                    ["killall", proc_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                logger.info("Killed %s daemon", proc_name)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

    @staticmethod
    def list_cameras() -> list[tuple[str, str]]:
        """Return list of (model, port) for all detected cameras."""
        camera_list = gp.Camera.autodetect()
        result = [(name, port) for name, port in camera_list]
        return result

    def connect(self) -> str:
        """Detect and connect to the camera. Returns camera summary text."""
        with self._lock:
            if self._connected:
                return "Already connected"

            # On macOS, kill PTPCamera daemon that steals USB cameras
            self._kill_macos_ptp_daemon()

            # Log all detected cameras
            try:
                cameras = self.list_cameras()
                logger.info("Detected cameras: %s", cameras if cameras else "(none)")
            except gp.GPhoto2Error as e:
                logger.warning("Auto-detect failed: %s", e)

            self._context = gp.Context()
            self._camera = gp.Camera()
            self._camera.init(self._context)
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
                camera_file = self._camera.capture_preview(self._context)
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
                    local_file = f"{download_path}/{file_path.name}"
                    camera_file = self._camera.file_get(
                        file_path.folder, file_path.name,
                        gp.GP_FILE_TYPE_NORMAL, self._context
                    )
                    camera_file.save(local_file)
                    logger.info("Downloaded to: %s", local_file)
                    return local_file

                return camera_path
            except gp.GPhoto2Error as e:
                logger.error("Capture failed: %s", e)
                raise

    # ─── Manual Focus Drive ───────────────────────────────────────

    def move_focus(self, step: str):
        """
        Move focus by one step.
        step: one of 'Near 1','Near 2','Near 3','Far 1','Far 2','Far 3','None'
        The camera must be in MF or DMF mode.
        """
        with self._lock:
            if not self._connected:
                return
            try:
                config = self._camera.get_config(self._context)
                widget = config.get_child_by_name("manualfocusdrive")
                widget.set_value(step)
                self._camera.set_config(config, self._context)
            except gp.GPhoto2Error as e:
                logger.error("Focus move failed (%s): %s", step, e)
                raise

    def move_focus_steps(self, direction: str, count: int, step_size: int = 1):
        """
        Move focus multiple steps in a direction.
        direction: 'near' or 'far'
        count: number of steps to take
        step_size: 1, 2, or 3 (maps to Near/Far 1/2/3)
        """
        step_name = f"{'Near' if direction == 'near' else 'Far'} {step_size}"
        for _ in range(count):
            self.move_focus(step_name)

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
