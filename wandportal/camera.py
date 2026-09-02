"""Threaded frame capture.

The capture thread always overwrites a single slot rather than filling a queue.
A queue would hand the engine stale frames after any hiccup, and a gesture
recognized from three-second-old frames is worse than one missed.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)


class FrameSource(Protocol):
    """What the engine needs from anything that produces frames."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read(self) -> np.ndarray | None: ...
    def stats(self) -> dict: ...


class Camera:
    """A USB/UVC camera read on its own thread."""

    def __init__(
        self,
        index: int = 0,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        flip_horizontal: bool = False,
        flip_vertical: bool = False,
        reopen_delay: float = 2.0,
    ) -> None:
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.flip_horizontal = flip_horizontal
        self.flip_vertical = flip_vertical
        self.reopen_delay = reopen_delay

        self._capture: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self.frames = 0
        self.reopens = 0
        self.measured_fps = 0.0
        self.last_error: str | None = None
        self.opened = False

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._release()

    def _release(self) -> None:
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:  # a dying UVC device can raise here
                pass
            self._capture = None
        self.opened = False

    def _open(self) -> bool:
        self._release()
        capture = cv2.VideoCapture(self.index)
        if not capture.isOpened():
            self.last_error = f"could not open camera {self.index}"
            capture.release()
            return False
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.fps)
        # A one-frame buffer keeps the driver from handing us a backlog after a
        # stall. Not every UVC driver honours it, which is why the capture
        # thread also drops rather than queues.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._capture = capture
        self.opened = True
        self.last_error = None
        log.info(
            "Camera %s opened at %sx%s",
            self.index,
            int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        return True

    # ---- capture ---------------------------------------------------------

    def _loop(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            if self._capture is None:
                if not self._open():
                    # Don't spin on a missing camera: the device may be
                    # re-enumerating, which takes seconds.
                    self.reopens += 1
                    if self._stop.wait(self.reopen_delay):
                        break
                    continue

            ok, frame = self._capture.read()
            if not ok or frame is None:
                log.warning("Camera read failed, reopening")
                self.last_error = "read failed"
                self._release()
                self.reopens += 1
                if self._stop.wait(self.reopen_delay):
                    break
                continue

            if self.flip_horizontal and self.flip_vertical:
                frame = cv2.flip(frame, -1)
            elif self.flip_horizontal:
                frame = cv2.flip(frame, 1)
            elif self.flip_vertical:
                frame = cv2.flip(frame, 0)

            with self._lock:
                self._frame = frame
            self.frames += 1

            now = time.monotonic()
            delta = now - last
            last = now
            if delta > 0:
                instant = 1.0 / delta
                self.measured_fps = (
                    instant if self.measured_fps == 0.0
                    else 0.9 * self.measured_fps + 0.1 * instant
                )

        self._release()

    def read(self) -> np.ndarray | None:
        """The newest frame, or None if nothing has been captured yet."""
        with self._lock:
            return self._frame

    def stats(self) -> dict:
        return {
            "opened": self.opened,
            "frames": self.frames,
            "reopens": self.reopens,
            "fps": round(self.measured_fps, 2),
            "width": self.width,
            "height": self.height,
            "last_error": self.last_error,
        }


class SyntheticCamera:
    """A frame source with no hardware behind it.

    Exists so the test suites and a dev laptop can exercise the whole pipeline.
    Frames are black unless a caller injects a blob position, which is how the
    API tests drive a cast end to end.
    """

    def __init__(
        self, width: int = 640, height: int = 480, dot_radius: int = 4, fps: float = 60.0
    ) -> None:
        self.width = width
        self.height = height
        self.dot_radius = dot_radius
        self.fps = fps
        self.position: tuple[float, float] | None = None
        self._frame: np.ndarray | None = None
        self._next_frame_at = 0.0
        self.frames = 0
        self.reopens = 0
        self.opened = False
        self.measured_fps = 0.0
        self.last_error = None

    def start(self) -> None:
        self.opened = True

    def stop(self) -> None:
        self.opened = False

    def set_position(self, position: tuple[float, float] | None) -> None:
        self.position = position

    def read(self) -> np.ndarray | None:
        """Return the newest frame, regenerated at most `fps` times a second.

        The rate limit matters: without it the engine loop spins as fast as the
        CPU allows and a half-second gesture yields thousands of points, which
        is nothing like the hardware this has to run against.
        """
        now = time.monotonic()
        if self._frame is not None and now < self._next_frame_at:
            return self._frame
        self._next_frame_at = now + (1.0 / self.fps if self.fps > 0 else 0.0)
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        if self.position is not None:
            x, y = int(self.position[0]), int(self.position[1])
            cv2.circle(frame, (x, y), self.dot_radius, (255, 255, 255), -1)
        self._frame = frame
        self.frames += 1
        return frame

    def stats(self) -> dict:
        return {
            "opened": self.opened,
            "frames": self.frames,
            "reopens": 0,
            "fps": 0.0,
            "width": self.width,
            "height": self.height,
            "synthetic": True,
            "last_error": None,
        }
