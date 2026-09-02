"""Blob tracking and gesture segmentation.

Threshold the frame, find the brightest small blob, follow it between frames,
and decide where one cast starts and ends. Everything here operates on a single
frame at a time and holds no locks — it runs inside the capture loop, which must
never block.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Sequence

import cv2
import numpy as np

log = logging.getLogger(__name__)

Point = tuple[float, float]


@dataclass
class Blob:
    """A tracked bright spot, in pixel coordinates."""

    x: float
    y: float
    area: float


@dataclass
class Gesture:
    """One completed cast: the path traced between blob acquisition and loss."""

    points: list[Point]
    started_at: float
    ended_at: float
    frames: int

    @property
    def duration(self) -> float:
        return self.ended_at - self.started_at

    @property
    def path_length(self) -> float:
        return path_length(self.points)


@dataclass
class TrackerUpdate:
    """What one frame produced. `gesture` is set only on the frame a cast ends."""

    blob: Blob | None = None
    gesture: Gesture | None = None
    rejected: str | None = None
    active: bool = False
    threshold: int = 0
    candidates: int = 0


def path_length(points: Sequence[Point]) -> float:
    """Total arc length of a path in pixels."""
    if len(points) < 2:
        return 0.0
    pts = np.asarray(points, dtype=np.float64)
    deltas = np.diff(pts, axis=0)
    return float(np.hypot(deltas[:, 0], deltas[:, 1]).sum())


class Tracker:
    """Finds the wand tip in each frame and segments the path into gestures."""

    def __init__(
        self,
        threshold: int = 230,
        min_area: float = 2.0,
        max_area: float = 400.0,
        max_jump: float = 160.0,
        smoothing: float = 0.35,
        start_frames: int = 2,
        lost_frames: int = 6,
        min_points: int = 12,
        min_path_length: float = 90.0,
        max_gesture_seconds: float = 4.0,
        threshold_mode: str = "fixed",
    ) -> None:
        self.threshold = int(threshold)
        self.min_area = float(min_area)
        self.max_area = float(max_area)
        self.max_jump = float(max_jump)
        self.smoothing = float(smoothing)
        self.start_frames = int(start_frames)
        self.lost_frames = int(lost_frames)
        self.min_points = int(min_points)
        self.min_path_length = float(min_path_length)
        self.max_gesture_seconds = float(max_gesture_seconds)
        self.threshold_mode = threshold_mode

        self._points: list[Point] = []
        self._pending: list[Point] = []
        self._smoothed: Point | None = None
        self._last: Point | None = None
        self._seen = 0
        self._lost = 0
        self._started_at = 0.0
        self._frames = 0
        self._active = False
        self.last_trace: list[Point] = []

    # `threshold_mode: fixed` is all Phase 1 implements. SPEC.md R2.2 adds an
    # ambient-adaptive mode and R2.4 a differencing frame source; both plug in
    # here rather than anywhere else in the pipeline.
    def working_threshold(self, gray: np.ndarray) -> int:
        """The brightness cutoff to use for this frame."""
        return self.threshold

    def to_gray(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 3:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return frame

    def mask(self, frame: np.ndarray) -> np.ndarray:
        """The binary threshold mask, as shown in the console's Tune view."""
        gray = self.to_gray(frame)
        _, binary = cv2.threshold(gray, self.working_threshold(gray), 255, cv2.THRESH_BINARY)
        return binary

    def _find_blobs(self, gray: np.ndarray, threshold: int) -> list[Blob]:
        _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blobs: list[Blob] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            x, y, w, h = cv2.boundingRect(contour)
            if area <= 0.0:
                # contourArea is 0 for contours a couple of pixels across, which
                # is exactly the size a distant wand tip is. Fall back to the
                # bounding box so small blobs aren't silently discarded.
                area = float(max(1, w * h))
            if area < self.min_area or area > self.max_area:
                continue
            moments = cv2.moments(contour)
            if moments["m00"] > 0:
                cx = moments["m10"] / moments["m00"]
                cy = moments["m01"] / moments["m00"]
            else:
                cx, cy = x + w / 2.0, y + h / 2.0
            blobs.append(Blob(float(cx), float(cy), area))
        return blobs

    def _choose(self, blobs: list[Blob]) -> Blob | None:
        """Pick the blob to follow.

        While tracking, prefer continuity: the nearest candidate within
        `max_jump`, so a static reflection elsewhere in frame can't steal the
        trail mid-cast. Otherwise take the largest, which at these areas is the
        strongest retroreflective return.
        """
        if not blobs:
            return None
        if self._last is not None:
            near = [
                (np.hypot(b.x - self._last[0], b.y - self._last[1]), b)
                for b in blobs
            ]
            near = [(d, b) for d, b in near if d <= self.max_jump]
            if near:
                return min(near, key=lambda db: db[0])[1]
            if self._active:
                # Everything visible is too far to be the same wand tip.
                return None
        return max(blobs, key=lambda b: b.area)

    def _smooth(self, blob: Blob) -> Point:
        point = (blob.x, blob.y)
        if self._smoothed is None:
            self._smoothed = point
        else:
            a = self.smoothing
            self._smoothed = (
                a * self._smoothed[0] + (1.0 - a) * point[0],
                a * self._smoothed[1] + (1.0 - a) * point[1],
            )
        return self._smoothed

    def process(self, frame: np.ndarray, now: float | None = None) -> TrackerUpdate:
        """Advance the tracker by one frame."""
        now = time.monotonic() if now is None else now
        gray = self.to_gray(frame)
        threshold = self.working_threshold(gray)
        blobs = self._find_blobs(gray, threshold)
        blob = self._choose(blobs)
        update = TrackerUpdate(
            blob=blob, threshold=threshold, candidates=len(blobs), active=self._active
        )

        if blob is not None:
            self._lost = 0
            self._last = (blob.x, blob.y)
            point = self._smooth(blob)
            if self._active:
                self._points.append(point)
                self._frames += 1
                if now - self._started_at > self.max_gesture_seconds:
                    # A permanently visible reflector would otherwise trace
                    # forever and never produce a cast.
                    update.gesture, update.rejected = self._finish(now)
            else:
                self._seen += 1
                self._pending.append(point)
                if self._seen >= self.start_frames:
                    self._active = True
                    self._started_at = now
                    self._points = list(self._pending)
                    self._frames = len(self._pending)
                    self._pending.clear()
        else:
            self._seen = 0
            self._pending.clear()
            if self._active:
                self._lost += 1
                if self._lost >= self.lost_frames:
                    update.gesture, update.rejected = self._finish(now)
            else:
                self._smoothed = None
                self._last = None

        update.active = self._active
        return update

    def _finish(self, now: float) -> tuple[Gesture | None, str | None]:
        """End the active gesture, returning it only if it passes the size gates."""
        points = self._points
        frames = self._frames
        started = self._started_at
        self._reset_path()

        if len(points) < self.min_points:
            return None, "too_few_points"
        length = path_length(points)
        if length < self.min_path_length:
            return None, "too_short"

        self.last_trace = list(points)
        return Gesture(points=points, started_at=started, ended_at=now, frames=frames), None

    def _reset_path(self) -> None:
        self._points = []
        self._pending = []
        self._smoothed = None
        self._last = None
        self._seen = 0
        self._lost = 0
        self._frames = 0
        self._active = False

    @property
    def current_points(self) -> list[Point]:
        """The in-flight gesture path, for drawing a live trail."""
        return self._points

    def reset(self) -> None:
        """Drop any in-flight gesture. Used when tuning values change mid-cast."""
        self._reset_path()

    def apply(self, values: dict) -> list[str]:
        """Apply tuning values, returning the names actually changed."""
        changed = []
        for key, value in values.items():
            if not hasattr(self, key) or key.startswith("_"):
                continue
            current = getattr(self, key)
            if isinstance(current, bool) or not isinstance(current, (int, float, str)):
                continue
            cast = type(current)(value)
            if cast != current:
                setattr(self, key, cast)
                changed.append(key)
        if changed:
            self.reset()
        return changed
