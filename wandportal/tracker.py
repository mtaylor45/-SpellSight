"""IR blob detection and gesture segmentation.

The retroreflector on the wand tip is by far the brightest thing in an
IR-lit frame, so a hard grayscale threshold beats a full blob detector
and costs a fraction of the CPU on a Pi.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum

import cv2
import numpy as np

from .config import TrackerConfig


class TrackState(str, Enum):
    IDLE = "idle"
    TRACKING = "tracking"
    COOLDOWN = "cooldown"


@dataclass
class Gesture:
    points: list[tuple[float, float]]
    duration: float
    path_length: float
    started_at: float = field(default_factory=time.time)


class AmbientEstimator:
    """How bright the room is, ignoring the wand.

    A living room is full of near-IR — sunlight, halogen, a fire, some TVs — and
    a fixed cutoff cannot survive a day/night cycle. Measuring ambient is the
    first half of that fix: spec R2.3 publishes it so the hardware session has a
    number to read, and R2.2 later drives the cutoff from it.

    Deliberately an object rather than a method on the tracker. A pulsed
    differencing source (spec R2.4) has to satisfy the same interface — give it
    a frame, get an ambient level — without the tracker knowing which produced it.
    """

    def __init__(self, percentile: float = 99.0, interval: int = 15, width: int = 160):
        self.percentile = percentile
        self.interval = max(1, interval)
        self.width = max(16, width)
        self.value: float | None = None
        self._countdown = 0

    def update(self, gray, exclude: tuple[float, float] | None = None,
               exclude_radius: float = 0.06) -> float | None:
        """Refresh the estimate every `interval` frames. Returns the current value.

        `exclude` is the tracked blob, masked out before measuring: a wand held
        still is the brightest thing in frame, and letting it into a 99th
        percentile would raise ambient until the wand thresholded itself out of
        existence.
        """
        self._countdown -= 1
        if self._countdown > 0:
            return self.value
        self._countdown = self.interval

        h, w = gray.shape[:2]
        if w <= 0 or h <= 0:
            return self.value
        # Downscale first — a full-res percentile every frame is not free on a Pi.
        scale = min(1.0, self.width / float(w))
        small = gray if scale >= 1.0 else cv2.resize(
            gray, (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )

        if exclude is not None:
            sh, sw = small.shape[:2]
            ex, ey = int(exclude[0] * scale), int(exclude[1] * scale)
            r = max(2, int(round(exclude_radius * max(sw, sh))))
            keep = np.ones(small.shape, dtype=bool)
            keep[max(0, ey - r):ey + r + 1, max(0, ex - r):ex + r + 1] = False
            values = small[keep]
            if values.size < 16:            # blob covers the frame; use it all
                values = small.reshape(-1)
        else:
            values = small.reshape(-1)

        self.value = float(np.percentile(values, self.percentile))
        return self.value


class BlobTracker:
    def __init__(self, cfg: TrackerConfig):
        self.cfg = cfg
        self.state = TrackState.IDLE
        self.points: list[tuple[float, float]] = []
        self.last_point: tuple[float, float] | None = None
        self.detection: tuple[float, float] | None = None
        self._missing = 0
        self._started = 0.0
        self._cooldown_until = 0.0
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.mask = None
        self.ambient = AmbientEstimator(
            percentile=cfg.ambient_percentile,
            interval=cfg.ambient_interval,
            width=cfg.ambient_width,
        )

    # -- detection ---------------------------------------------------------

    @property
    def working_threshold(self) -> int:
        """The cutoff actually in force this frame.

        In "fixed" mode this is the configured threshold, unchanged — that is
        the whole point, and the tests assert against it. Day 8 (spec R2.2)
        gives "adaptive" a different answer here, and nothing else in the
        pipeline has to know.
        """
        return int(self.cfg.threshold)

    @property
    def headroom(self) -> float | None:
        """How far the cutoff sits above the room. Small means trouble coming."""
        if self.ambient.value is None:
            return None
        return round(self.working_threshold - self.ambient.value, 1)

    def detect(self, frame) -> tuple[float, float] | None:
        """Find the wand tip in a BGR or grayscale frame."""
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        k = self.cfg.blur
        if k and k >= 3:
            k = k if k % 2 == 1 else k + 1
            gray = cv2.GaussianBlur(gray, (k, k), 0)

        # Measured every frame call but recomputed only every ambient_interval,
        # and never counting the wand itself.
        self.ambient.update(gray, exclude=self.last_point)

        _, mask = cv2.threshold(gray, self.working_threshold, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        self.mask = mask

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_score = -1.0
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.cfg.min_area or area > self.cfg.max_area:
                continue
            m = cv2.moments(c)
            if m["m00"] == 0:
                x, y = c[0][0]
                cx, cy = float(x), float(y)
            else:
                cx = m["m10"] / m["m00"]
                cy = m["m01"] / m["m00"]

            # Prefer the blob nearest the last known position; otherwise the
            # largest. This keeps a stray reflection from stealing the track.
            if self.last_point is not None:
                d = math.dist((cx, cy), self.last_point)
                if d > self.cfg.max_jump:
                    continue
                score = 1000.0 - d
            else:
                score = area

            if score > best_score:
                best_score = score
                best = (cx, cy)

        return best

    # -- segmentation ------------------------------------------------------

    def update(self, frame) -> Gesture | None:
        """Feed one frame. Returns a Gesture on the frame a cast completes."""
        now = time.monotonic()

        if self.state is TrackState.COOLDOWN:
            if now < self._cooldown_until:
                self.detection = None
                return None
            self.state = TrackState.IDLE
            self.last_point = None

        point = self.detect(frame)
        self.detection = point

        if point is not None:
            a = self.cfg.smoothing
            if self.last_point is not None and 0.0 < a < 1.0:
                point = (
                    a * self.last_point[0] + (1 - a) * point[0],
                    a * self.last_point[1] + (1 - a) * point[1],
                )
            self.last_point = point
            self._missing = 0

            if self.state is TrackState.IDLE:
                self.state = TrackState.TRACKING
                self.points = [point]
                self._started = now
            else:
                self.points.append(point)

            if now - self._started > self.cfg.max_duration:
                return self._finish(now)
            return None

        # No blob this frame.
        if self.state is TrackState.TRACKING:
            self._missing += 1
            if self._missing >= self.cfg.lost_frames:
                return self._finish(now)
        else:
            self.last_point = None
        return None

    def _finish(self, now: float) -> Gesture | None:
        points = self.points
        self.points = []
        self.last_point = None
        self._missing = 0
        self.state = TrackState.COOLDOWN
        self._cooldown_until = now + self.cfg.cooldown

        if len(points) < self.cfg.min_points:
            return None
        length = path_length(points)
        if length < self.cfg.min_path_length:
            return None
        return Gesture(points=points, duration=now - self._started, path_length=length)

    def reset(self) -> None:
        self.points = []
        self.last_point = None
        self._missing = 0
        self.state = TrackState.IDLE
        self._cooldown_until = 0.0

    def skip_cooldown(self) -> None:
        self._cooldown_until = 0.0


def path_length(points: list[tuple[float, float]]) -> float:
    return float(sum(math.dist(a, b) for a, b in zip(points, points[1:])))


def render_trace(points: list[tuple[float, float]], size: int = 200, pad: int = 12):
    """Render a gesture as a small square image (for the training console)."""
    img = np.zeros((size, size), dtype=np.uint8)
    if len(points) < 2:
        return img
    arr = np.array(points, dtype=np.float32)
    lo = arr.min(axis=0)
    span = float(max((arr.max(axis=0) - lo).max(), 1e-6))
    scale = (size - 2 * pad) / span
    arr = (arr - lo) * scale + pad
    pts = arr.astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(img, [pts], False, 255, 2, cv2.LINE_AA)
    cv2.circle(img, tuple(pts[0][0]), 4, 160, -1)
    return img
