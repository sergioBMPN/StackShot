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

    def set_point_a(self) -> int:
        """Mark current focus position as Point A. Returns the position."""
        pos = self._controller.get_focal_position()
        if pos is None:
            raise RuntimeError(
                "Cannot read focus position from camera.\n"
                "Make sure:\n"
                "1. Lens AF/MF switch is set to AF\n"
                "2. Camera body Focus Mode is Manual"
            )
        self._point_a = FocusPoint(position=pos)
        self._point_b = None  # reset B when A changes
        logger.info("Focus Point A set at focalposition %d", pos)
        return pos

    def set_point_b(self) -> int:
        """Mark current focus position as Point B. Returns the position."""
        if self._point_a is None:
            raise RuntimeError("Set Point A first")
        pos = self._controller.get_focal_position()
        if pos is None:
            raise RuntimeError(
                "Cannot read focus position from camera.\n"
                "Make sure:\n"
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
        """Start focus bracketing in a background thread."""
        if self._point_a is None or self._point_b is None:
            raise RuntimeError("Set both Point A and Point B first")
        if num_photos < 2:
            raise ValueError("Need at least 2 photos")
        if self.is_running:
            raise RuntimeError("Bracket already running")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_bracket,
            args=(num_photos, download_path),
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        """Request cancellation of a running bracket."""
        if self.is_running:
            self._stop_event.set()
            logger.info("Bracket stop requested")

    def _run_bracket(self, num_photos: int, download_path: Optional[str]):
        """Internal: execute the bracket sequence with closed-loop focus."""
        if self.on_start:
            self.on_start()

        photos_taken = 0
        try:
            pos_a = self._point_a.position
            pos_b = self._point_b.position

            if pos_a == pos_b:
                if self.on_error:
                    self.on_error("Points A and B are at the same position. "
                                  "Move focus between setting A and B.")
                return

            # Calculate target positions for each photo
            positions = []
            for i in range(num_photos):
                t = i / (num_photos - 1)
                pos = round(pos_a + (pos_b - pos_a) * t)
                positions.append(pos)

            # Step 1: Drive to Point A
            self._notify_progress(0, num_photos, "Moving to Point A...")
            reached = self._controller.move_to_position(
                pos_a, self._stop_event
            )
            logger.info("Drove to A: target=%d reached=%d", pos_a, reached)

            if self._stop_event.is_set():
                self._notify_progress(0, num_photos, "Cancelled")
                return

            # Step 2: Bracket loop
            for i, target_pos in enumerate(positions):
                if self._stop_event.is_set():
                    self._notify_progress(photos_taken, num_photos, "Cancelled")
                    return

                self._notify_progress(
                    photos_taken, num_photos,
                    f"Moving to position {target_pos} ({i + 1}/{num_photos})..."
                )
                reached = self._controller.move_to_position(
                    target_pos, self._stop_event
                )
                logger.info("Step %d: target=%d reached=%d", i, target_pos, reached)

                if self._stop_event.is_set():
                    self._notify_progress(photos_taken, num_photos, "Cancelled")
                    return

                # Settle delay
                time.sleep(1.0)

                # Capture with retry
                self._notify_progress(
                    photos_taken, num_photos,
                    f"Capturing {i + 1}/{num_photos} (pos {reached})..."
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
                                f"Retry {attempt + 2}/3 for photo {i + 1} (waiting {wait:.0f}s)..."
                            )
                            time.sleep(wait)
                        else:
                            logger.error("Capture failed after 3 attempts at step %d", i)
                            if self.on_error:
                                self.on_error(f"Capture failed at photo {i + 1} after 3 attempts: {e}")
                            return

                if not captured:
                    return

                # Wait for camera to write image
                time.sleep(2.0)

            self._notify_progress(photos_taken, num_photos, "Complete!")
            if self.on_complete:
                self.on_complete(photos_taken)

        except Exception as e:
            logger.error("Bracket error: %s", e)
            if self.on_error:
                self.on_error(str(e))

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
        logger.info("Focus bracket state reset")
