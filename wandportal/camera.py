"""Threaded USB / CSI camera capture.

Always hands back the most recent frame rather than a queued one, so
gesture tracking never falls behind the wand.
"""

from __future__ import annotations

import logging
import threading
import time

import cv2

from .config import CameraConfig

log = logging.getLogger(__name__)


class Camera:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self._cap: cv2.VideoCapture | None = None
        self._frame = None
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fps = 0.0

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        source = self.cfg.source
        if isinstance(source, str) and source.isdigit():
            source = int(source)

        cap = cv2.VideoCapture(source)
        if self.cfg.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.cfg.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        if not cap.isOpened():
            raise RuntimeError(
                f"Could not open camera source {self.cfg.source!r}. "
                "Check that the device is passed into the container "
                "(devices: - /dev/video0:/dev/video0)."
            )
        self._cap = cap
        log.info(
            "Camera open: %sx%s @ %s fps",
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            cap.get(cv2.CAP_PROP_FPS),
        )

    def start(self) -> "Camera":
        if self._cap is None:
            self.open()
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._cap:
            self._cap.release()
            self._cap = None

    # -- capture -----------------------------------------------------------

    def _loop(self) -> None:
        failures = 0
        last = time.monotonic()
        smoothed = 0.0
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok or frame is None:
                failures += 1
                if failures > 60:
                    log.error("Camera read failed 60x, reopening")
                    try:
                        self._cap.release()
                        self.open()
                        failures = 0
                    except Exception as exc:  # pragma: no cover
                        log.error("Reopen failed: %s", exc)
                        time.sleep(2.0)
                time.sleep(0.02)
                continue

            failures = 0
            frame = self._orient(frame)

            now = time.monotonic()
            dt = now - last
            last = now
            if dt > 0:
                smoothed = smoothed * 0.9 + (1.0 / dt) * 0.1

            with self._lock:
                self._frame = frame
                self._seq += 1
                self._fps = smoothed

    def _orient(self, frame):
        if self.cfg.rotate == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif self.cfg.rotate == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif self.cfg.rotate == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        if self.cfg.flip_horizontal:
            frame = cv2.flip(frame, 1)
        if self.cfg.flip_vertical:
            frame = cv2.flip(frame, 0)
        return frame

    def read(self):
        """Return (sequence, frame). Frame is None until the first capture."""
        with self._lock:
            return self._seq, self._frame

    @property
    def fps(self) -> float:
        with self._lock:
            return round(self._fps, 1)
