"""
focus_bracket.py
Focus bracketing logic: captures a sequence of photos between two focus points.
Uses the camera's focalposition (0=nearest, 100=infinity) for closed-loop positioning.
Runs in a separate thread with cancellation support.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from camera_controller import CameraController

logger = logging.getLogger(__name__)


@dataclass
class FocusPoint:
    """A focus position stored as focalposition (0-100)."""
    position: int = 0  # 0=nearest, 100=infinity


class FocusBracket:
    """
    Manages focus bracketing between two user-defined points.

    The user sets Point A then moves focus to Point B.
    Both points are recorded as focalposition values (0-100).
    During bracketing, the system drives to A and steps evenly toward B,
    capturing a photo at each position using closed-loop control.
    """

    def __init__(self, controller: CameraController):
        self._controller = controller
        self._point_a: Optional[FocusPoint] = None
        self._point_b: Optional[FocusPoint] = None
        self._has_focal_position: Optional[bool] = None  # cached availability

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Callbacks for GUI updates
        self.on_start: Optional[Callable[[], None]] = None  # bracket starting
        self.on_progress: Optional[Callable[[int, int, str], None]] = None
        self.on_complete: Optional[Callable[[int], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    @property
    def point_a(self) -> Optional[FocusPoint]:
        return self._point_a

    @property
    def point_b(self) -> Optional[FocusPoint]:
        return self._point_b

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def total_distance(self) -> Optional[int]:
        """Total distance between A and B in focalposition units, or None."""
        if self._point_a is None or self._point_b is None:
            return None
        return abs(self._point_b.position - self._point_a.position)

    def check_focal_position(self) -> bool:
        """Check if the camera supports focalposition readback."""
        if self._has_focal_position is None:
            pos = self._controller.get_focal_position()
            self._has_focal_position = pos is not None
        return self._has_focal_position

    def get_current_position(self) -> Optional[int]:
        """Read current focalposition from camera."""
        return self._controller.get_focal_position()

    # ─── Point Management ─────────────────────────────────────────

    def set_point_a(self, user_pos: Optional[int] = None) -> int:
        """
        Mark current focus position as Point A. Returns the position.
        If focalposition is not readable, uses user_pos as fallback.
        """
        pos = self._controller.get_focal_position()
        if pos is None and user_pos is not None:
            pos = user_pos
            logger.info("Using user-supplied position %d for Point A (no readback)", pos)
        if pos is None:
            raise RuntimeError(
                "Cannot read focus position from camera.\n"
                "Enter a target position (0-100) in the Target spinbox,\n"
                "then use 'Set A at target' instead.\n\n"
                "Also check:\n"
                "1. Lens AF/MF switch is set to AF\n"
                "2. Camera body Focus Mode is Manual"
            )
        self._point_a = FocusPoint(position=pos)
        self._point_b = None  # reset B when A changes
        logger.info("Focus Point A set at focalposition %d", pos)
        return pos

    def set_point_b(self, user_pos: Optional[int] = None) -> int:
        """
        Mark current focus position as Point B. Returns the position.
        If focalposition is not readable, uses user_pos as fallback.
        """
        if self._point_a is None:
            raise RuntimeError("Set Point A first")
        pos = self._controller.get_focal_position()
        if pos is None and user_pos is not None:
            pos = user_pos
            logger.info("Using user-supplied position %d for Point B (no readback)", pos)
        if pos is None:
            raise RuntimeError(
                "Cannot read focus position from camera.\n"
                "Enter a target position (0-100) in the Target spinbox,\n"
                "then use 'Set B at target' instead.\n\n"
                "Also check:\n"
                "1. Lens AF/MF switch is set to AF\n"
                "2. Camera body Focus Mode is Manual"
            )
        self._point_b = FocusPoint(position=pos)
        logger.info("Focus Point B set at focalposition %d", pos)
        return pos

    def move_focus_near(self):
        """Nudge focus toward near (medium speed)."""
        self._controller.move_focus(-3.0)
        time.sleep(0.3)

    def move_focus_far(self):
        """Nudge focus toward far (medium speed)."""
        self._controller.move_focus(3.0)
        time.sleep(0.3)

    # ─── Bracket Execution ────────────────────────────────────────

    def start(self, num_photos: int, download_path: Optional[str] = None):
        """Start step-by-step focus bracketing in a background thread."""
        self._validate_bracket_ready()
        if num_photos < 2:
            raise ValueError("Need at least 2 photos")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_bracket,
            args=(num_photos, download_path),
            daemon=True,
        )
        self._thread.start()

    def start_sweep_single(self, step_size: float = 1.0,
                           download_path: Optional[str] = None):
        """Start sweep mode: rapid single captures while moving A→B."""
        self._validate_bracket_ready()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_sweep_single,
            args=(step_size, download_path),
            daemon=True,
        )
        self._thread.start()

    def start_sweep_burst(self, step_size: float = 1.0,
                          sweep_delay: float = 0.15,
                          download_path: Optional[str] = None):
        """Start sweep burst: hold shutter while moving A→B (10fps continuous)."""
        self._validate_bracket_ready()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_sweep_burst,
            args=(step_size, sweep_delay, download_path),
            daemon=True,
        )
        self._thread.start()

    def _validate_bracket_ready(self):
        if self._point_a is None or self._point_b is None:
            raise RuntimeError("Set both Point A and Point B first")
        if self.is_running:
            raise RuntimeError("Bracket already running")

    def stop(self):
        """Request cancellation of a running bracket."""
        if self.is_running:
            self._stop_event.set()
            logger.info("Bracket stop requested")

    # ─── Step-by-step bracket ─────────────────────────────────────

    def _run_bracket(self, num_photos: int, download_path: Optional[str]):
        """Execute step-by-step bracket: move, capture, move, capture..."""
        if self.on_start:
            self.on_start()

        photos_taken = 0
        try:
            pos_a = self._point_a.position
            pos_b = self._point_b.position

            if pos_a == pos_b:
                if self.on_error:
                    self.on_error("Points A and B are at the same position.")
                return

            total_distance = pos_b - pos_a  # signed
            # Steps between consecutive photos in open-loop units
            step_per_photo = total_distance / (num_photos - 1)

            # Drive to Point A
            self._notify_progress(0, num_photos, "Moving to Point A...")
            self._drive_to_endpoint(pos_a)
            if self._stop_event.is_set():
                return

            # Bracket loop — relative stepping between positions
            for i in range(num_photos):
                if self._stop_event.is_set():
                    self._notify_progress(photos_taken, num_photos, "Cancelled")
                    return

                # Move to next position (skip first — already at A)
                if i > 0:
                    self._notify_progress(
                        photos_taken, num_photos,
                        f"Stepping to photo {i + 1}/{num_photos}..."
                    )
                    self._relative_step(step_per_photo)
                    if self._stop_event.is_set():
                        return

                # Brief settle
                time.sleep(0.3)

                # Capture with retry
                self._notify_progress(
                    photos_taken, num_photos,
                    f"Capturing {i + 1}/{num_photos}..."
                )
                captured = False
                for attempt in range(3):
                    if self._stop_event.is_set():
                        break
                    try:
                        self._controller.capture_image(download_path)
                        captured = True
                        photos_taken += 1
                        break
                    except Exception as e:
                        logger.warning("Capture attempt %d failed at step %d: %s",
                                       attempt + 1, i, e)
                        if attempt < 2:
                            wait = 2.0 * (attempt + 1)
                            self._notify_progress(
                                photos_taken, num_photos,
                                f"Retry {attempt + 2}/3 ({wait:.0f}s)..."
                            )
                            time.sleep(wait)
                        else:
                            logger.error("Capture failed after 3 attempts at step %d", i)
                            if self.on_error:
                                self.on_error(
                                    f"Capture failed at photo {i + 1} after 3 attempts: {e}")
                            return

                if not captured:
                    return

            self._notify_progress(photos_taken, num_photos, "Complete!")
            if self.on_complete:
                self.on_complete(photos_taken)

        except Exception as e:
            logger.error("Bracket error: %s", e)
            if self.on_error:
                self.on_error(str(e))

    # ─── Sweep single (rapid single captures while moving) ────────

    def _run_sweep_single(self, step_size: float, download_path: Optional[str]):
        """Sweep A→B firing rapid single captures at each micro-step."""
        if self.on_start:
            self.on_start()

        photos_taken = 0
        try:
            pos_a = self._point_a.position
            pos_b = self._point_b.position
            if pos_a == pos_b:
                if self.on_error:
                    self.on_error("Points A and B are at the same position.")
                return

            total_distance = abs(pos_b - pos_a)
            direction = 1.0 if pos_b > pos_a else -1.0
            magnitude = min(abs(step_size), 7.0)

            # Drive to Point A
            self._notify_progress(0, 0, "Moving to Point A...")
            self._drive_to_endpoint(pos_a)
            if self._stop_event.is_set():
                return

            # Estimate total steps for progress feedback
            estimated_steps = max(1, int(total_distance / magnitude))

            # Sweep: step + capture, step + capture...
            for step_idx in range(estimated_steps + 20):  # margin for overshoot
                if self._stop_event.is_set():
                    self._notify_progress(photos_taken, photos_taken,
                                          f"Cancelled — {photos_taken} photos")
                    break

                self._notify_progress(
                    step_idx, estimated_steps,
                    f"Sweep: capturing photo {photos_taken + 1}..."
                )

                # Capture (no retry in sweep — speed is priority)
                try:
                    self._controller.capture_image(download_path)
                    photos_taken += 1
                except Exception as e:
                    logger.warning("Sweep capture failed at step %d: %s", step_idx, e)
                    # Try once more after brief recovery
                    time.sleep(0.5)
                    try:
                        self._controller.capture_image(download_path)
                        photos_taken += 1
                    except Exception:
                        logger.error("Sweep capture retry failed, stopping sweep")
                        break

                # Step focus toward B
                self._controller.move_focus(direction * magnitude)
                time.sleep(0.15)

            self._notify_progress(photos_taken, photos_taken,
                                  f"Sweep complete: {photos_taken} photos")
            if self.on_complete:
                self.on_complete(photos_taken)

        except Exception as e:
            logger.error("Sweep single error: %s", e)
            if self.on_error:
                self.on_error(str(e))

    # ─── Sweep burst (continuous shutter while moving) ────────────

    def _run_sweep_burst(self, step_size: float, sweep_delay: float,
                         download_path: Optional[str]):
        """
        Hold shutter down while stepping focus from A→B.
        Uses the camera's continuous drive mode (e.g. 10fps).
        Photos are NOT downloaded during the sweep — only counted.
        After the sweep, optionally downloads from camera if download_path set.
        """
        if self.on_start:
            self.on_start()

        try:
            pos_a = self._point_a.position
            pos_b = self._point_b.position
            if pos_a == pos_b:
                if self.on_error:
                    self.on_error("Points A and B are at the same position.")
                return

            total_distance = abs(pos_b - pos_a)
            direction = 1.0 if pos_b > pos_a else -1.0
            magnitude = min(abs(step_size), 7.0)
            estimated_steps = max(1, int(total_distance / magnitude))

            # Drive to Point A
            self._notify_progress(0, estimated_steps, "Moving to Point A...")
            self._drive_to_endpoint(pos_a)
            if self._stop_event.is_set():
                return

            time.sleep(0.3)

            # Press shutter (start continuous capture)
            self._notify_progress(0, estimated_steps, "Starting burst...")
            self._controller.press_shutter()
            logger.info("Burst started")
            time.sleep(0.2)

            # Sweep focus A→B while camera fires
            for step_idx in range(estimated_steps + 5):
                if self._stop_event.is_set():
                    break

                self._notify_progress(
                    step_idx, estimated_steps,
                    f"Burst sweep: step {step_idx + 1}/{estimated_steps}..."
                )

                self._controller.move_focus(direction * magnitude)
                time.sleep(max(0.05, sweep_delay))

            # Release shutter
            try:
                self._controller.release_shutter()
            except Exception:
                pass
            logger.info("Burst ended")
            time.sleep(1.0)

            # Count photos on card (we don't know exact count)
            self._notify_progress(
                estimated_steps, estimated_steps,
                f"Burst sweep complete (~{estimated_steps} steps). "
                "Check camera for photo count."
            )

            # Download all new photos if path specified
            if download_path:
                self._notify_progress(
                    estimated_steps, estimated_steps,
                    "Downloading photos from camera..."
                )
                downloaded = self._download_new_photos(download_path)
                self._notify_progress(
                    estimated_steps, estimated_steps,
                    f"Complete: {downloaded} photos downloaded"
                )
                if self.on_complete:
                    self.on_complete(downloaded)
            else:
                if self.on_complete:
                    self.on_complete(estimated_steps)

        except Exception as e:
            # Make sure shutter is released on error
            try:
                self._controller.release_shutter()
            except Exception:
                pass
            logger.error("Sweep burst error: %s", e)
            if self.on_error:
                self.on_error(str(e))

    def _download_new_photos(self, download_path: str) -> int:
        """Download all files from the camera's last storage folder."""
        import os
        downloaded = 0
        try:
            # List files on camera storage
            folders = self._controller._camera.folder_list_files(
                "/store_00010001/DCIM", self._controller._context
            )
            # Find the last subfolder
            subfolders = self._controller._camera.folder_list_folders(
                "/store_00010001/DCIM", self._controller._context
            )
            if not subfolders:
                return 0
            last_folder = f"/store_00010001/DCIM/{subfolders[-1][0]}"
            files = self._controller._camera.folder_list_files(
                last_folder, self._controller._context
            )
            for fname, _ in files:
                local_path = os.path.join(download_path, fname)
                if os.path.exists(local_path):
                    continue  # already downloaded
                try:
                    camera_file = self._controller._camera.file_get(
                        last_folder, fname,
                        14,  # GP_FILE_TYPE_NORMAL
                        self._controller._context
                    )
                    camera_file.save(local_path)
                    downloaded += 1
                except Exception as e:
                    logger.warning("Failed to download %s: %s", fname, e)
        except Exception as e:
            logger.warning("Download enumeration failed: %s", e)
        return downloaded

    # ─── Movement helpers ─────────────────────────────────────────

    def _drive_to_endpoint(self, target_pos: int):
        """
        Open-loop drive to a target position (0-100).
        Drives to nearest endpoint first, then steps to target.
        """
        # Try closed-loop first
        current = self._controller.get_focal_position()
        if current is not None:
            self._controller.move_to_position(target_pos, self._stop_event)
            return

        # Open-loop: drive to nearest endpoint, then step
        if target_pos <= 50:
            # Drive to near end (0)
            for _ in range(30):
                if self._stop_event.is_set():
                    return
                self._controller.move_focus(-7.0)
                time.sleep(0.15)
            # Step toward target
            for _ in range(target_pos):
                if self._stop_event.is_set():
                    return
                self._controller.move_focus(1.0)
                time.sleep(0.15)
        else:
            # Drive to far end (100)
            for _ in range(30):
                if self._stop_event.is_set():
                    return
                self._controller.move_focus(7.0)
                time.sleep(0.15)
            # Step back toward target
            for _ in range(100 - target_pos):
                if self._stop_event.is_set():
                    return
                self._controller.move_focus(-1.0)
                time.sleep(0.15)

    def _relative_step(self, distance: float):
        """
        Move focus by a relative amount in open-loop units.
        Handles fractional distances by accumulating residuals across calls.
        """
        if not hasattr(self, '_step_residual'):
            self._step_residual = 0.0

        total = distance + self._step_residual
        direction = 1.0 if total >= 0 else -1.0
        abs_total = abs(total)

        # Use larger magnitude for big steps, 1.0 for fine steps
        if abs_total >= 7:
            mag = 7.0
        elif abs_total >= 3:
            mag = 3.0
        else:
            mag = 1.0

        steps = int(abs_total / mag)
        self._step_residual = total - (steps * mag * direction)

        for _ in range(steps):
            if self._stop_event.is_set():
                return
            self._controller.move_focus(direction * mag)
            time.sleep(0.15)

    # ─── Helpers ──────────────────────────────────────────────────

    def _notify_progress(self, current: int, total: int, message: str):
        if self.on_progress:
            self.on_progress(current, total, message)

    def reset(self):
        """Reset all bracket state."""
        if self.is_running:
            self.stop()
            self._thread.join(timeout=5)
        self._point_a = None
        self._point_b = None
        self._has_focal_position = None  # re-check on next use
        logger.info("Focus bracket state reset")
