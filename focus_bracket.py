"""
focus_bracket.py
Focus bracketing logic: captures a sequence of photos between two focus points.
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
    """A focus position stored as a relative step count from a reference origin."""
    step_count: int = 0  # cumulative Near 1 steps from origin (positive = far, negative = near)


class FocusBracket:
    """
    Manages focus bracketing between two user-defined points.

    The user sets Point A (origin, step_count=0) then moves focus to Point B.
    The total distance is tracked as a relative step count.
    During bracketing, the system returns to A and steps evenly toward B,
    capturing a photo at each position.
    """

    def __init__(self, controller: CameraController):
        self._controller = controller
        self._point_a: Optional[FocusPoint] = None
        self._point_b: Optional[FocusPoint] = None
        self._current_position: int = 0  # relative to point A
        self._step_size: int = 1  # 1=finest, 2=medium, 3=coarsest

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Callbacks for GUI updates
        self.on_progress: Optional[Callable[[int, int, str], None]] = None  # (current, total, message)
        self.on_complete: Optional[Callable[[int], None]] = None  # (photos_taken)
        self.on_error: Optional[Callable[[str], None]] = None  # (error_message)

    @property
    def step_size(self) -> int:
        return self._step_size

    @step_size.setter
    def step_size(self, value: int):
        if value not in (1, 2, 3):
            raise ValueError("step_size must be 1, 2, or 3")
        self._step_size = value

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
    def total_steps(self) -> Optional[int]:
        """Total steps between A and B, or None if not both set."""
        if self._point_a is None or self._point_b is None:
            return None
        return abs(self._point_b.step_count - self._point_a.step_count)

    # ─── Point Management ─────────────────────────────────────────

    def set_point_a(self):
        """Mark current focus position as Point A (origin)."""
        self._point_a = FocusPoint(step_count=0)
        self._point_b = None  # reset B when A changes
        self._current_position = 0
        logger.info("Focus Point A set (origin)")

    def set_point_b(self):
        """Mark current focus position as Point B."""
        if self._point_a is None:
            raise RuntimeError("Set Point A first")
        self._point_b = FocusPoint(step_count=self._current_position)
        logger.info("Focus Point B set at step %d from A", self._current_position)

    def move_focus_near(self, count: int = 1):
        """Move focus toward near, track position."""
        self._controller.move_focus_steps("near", count, self._step_size)
        self._current_position -= count
        logger.debug("Focus moved near %d, position now %d", count, self._current_position)

    def move_focus_far(self, count: int = 1):
        """Move focus toward far, track position."""
        self._controller.move_focus_steps("far", count, self._step_size)
        self._current_position += count
        logger.debug("Focus moved far %d, position now %d", count, self._current_position)

    # ─── Navigation ───────────────────────────────────────────────

    def _go_to_position(self, target: int):
        """Move from current_position to target position (in step units)."""
        delta = target - self._current_position
        if delta == 0:
            return
        direction = "far" if delta > 0 else "near"
        steps = abs(delta)
        for i in range(steps):
            if self._stop_event.is_set():
                return
            self._controller.move_focus_steps(direction, 1, self._step_size)
            self._current_position += 1 if delta > 0 else -1
            # Small delay to let the lens settle
            time.sleep(0.05)

    # ─── Bracket Execution ────────────────────────────────────────

    def start(self, num_photos: int, download_path: Optional[str] = None):
        """
        Start focus bracketing in a background thread.
        num_photos: total number of photos to take (including at A and B).
        download_path: local folder to save photos (None = don't download).
        """
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
        """Internal: execute the bracket sequence."""
        photos_taken = 0
        try:
            pos_a = self._point_a.step_count
            pos_b = self._point_b.step_count
            total_distance = pos_b - pos_a  # can be negative

            # Calculate step positions for each photo
            positions = []
            for i in range(num_photos):
                if num_photos == 1:
                    pos = pos_a
                else:
                    pos = pos_a + round(total_distance * i / (num_photos - 1))
                positions.append(pos)

            # Step 1: Return to Point A
            self._notify_progress(0, num_photos, "Returning to Point A...")
            self._go_to_position(pos_a)

            if self._stop_event.is_set():
                self._notify_progress(0, num_photos, "Cancelled")
                return

            # Step 2: Bracket loop
            for i, target_pos in enumerate(positions):
                if self._stop_event.is_set():
                    self._notify_progress(photos_taken, num_photos, "Cancelled")
                    return

                # Move to target position
                self._notify_progress(
                    photos_taken, num_photos,
                    f"Moving to position {i + 1}/{num_photos}..."
                )
                self._go_to_position(target_pos)

                if self._stop_event.is_set():
                    self._notify_progress(photos_taken, num_photos, "Cancelled")
                    return

                # Small settle delay after focus move
                time.sleep(0.2)

                # Capture
                self._notify_progress(
                    photos_taken, num_photos,
                    f"Capturing {i + 1}/{num_photos}..."
                )
                try:
                    self._controller.capture_image(download_path)
                    photos_taken += 1
                except Exception as e:
                    logger.error("Capture failed at step %d: %s", i, e)
                    if self.on_error:
                        self.on_error(f"Capture failed at photo {i + 1}: {e}")
                    return

                # Wait for camera to be ready
                time.sleep(0.3)

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
        self._current_position = 0
        logger.info("Focus bracket state reset")
