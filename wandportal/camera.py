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
        # Health, for /api/status and the console. Silent degradation is the
        # worst failure mode for a device nobody can see.
        self.opened = False
        self.reopens = 0
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> bool:
        """Try to open the device. Reports failure rather than raising.

        Raising here used to kill the process at startup, which on a device
        running under `Restart=always` or `restart: unless-stopped` turned an
        unplugged camera into a boot loop. A camera that is missing, still
        enumerating, or briefly claimed by something else is an ordinary
        condition for this device, so the capture thread retries instead.
        """
        source = self.cfg.source
        if isinstance(source, str) and source.isdigit():
            source = int(source)

        try:
            cap = cv2.VideoCapture(source)
        except Exception as exc:                      # a malformed source
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.opened = False
            return False

        if not cap.isOpened():
            cap.release()
            self.last_error = (
                f"could not open camera source {self.cfg.source!r} — check the device "
                "exists and, in Docker, that it is passed in "
                "(devices: - /dev/video0:/dev/video0)"
            )
            self.opened = False
            return False

        if self.cfg.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.cfg.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        self._cap = cap
        self.opened = True
        self.last_error = None
        log.info(
            "Camera open: %sx%s @ %s fps",
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            cap.get(cv2.CAP_PROP_FPS),
        )
        return True

    def start(self) -> "Camera":
        """Start capturing. Opening happens on the capture thread.

        Nothing is opened here on purpose: recognition must reach a serving
        state with no hardware attached, so a camera that is not there yet is
        the capture thread's problem, not a reason to refuse to start.
        """
        self._stop.clear()
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
        self.opened = False

    # -- capture -----------------------------------------------------------

    def _loop(self) -> None:
        failures = 0
        frames_since_open = 0
        delay = self.cfg.reopen_delay
        last = time.monotonic()
        smoothed = 0.0
        while not self._stop.is_set():
            if self._cap is None:
                if not self.open():
                    # Back off so a permanently absent camera stays quiet
                    # instead of spinning a core and filling the log. Waiting on
                    # the stop event keeps shutdown immediate.
                    log.warning("Camera unavailable (%s); retrying in %.0fs",
                                self.last_error, delay)
                    self._stop.wait(delay)
                    delay = min(delay * 2, self.cfg.reopen_max_delay)
                    continue
                failures = 0
                frames_since_open = 0
                last = time.monotonic()

            ok, frame = self._cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= self.cfg.reopen_after_failures:
                    log.error("Camera read failed %dx, reopening", failures)
                    self.last_error = f"read failed {failures}x"
                    self.reopens += 1
                    self._release()
                    failures = 0
                    if frames_since_open == 0:
                        # It enumerates but never delivers a frame — undervoltage
                        # on a Pi looks exactly like this. Reopening on a tight
                        # loop would never fix it, so back off as if it were
                        # absent rather than hammering the device.
                        self._stop.wait(delay)
                        delay = min(delay * 2, self.cfg.reopen_max_delay)
                    else:
                        delay = self.cfg.reopen_delay
                time.sleep(0.02)
                continue

            failures = 0
            frames_since_open += 1
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

    def _release(self) -> None:
        """Drop the current capture so the loop reopens it next time round."""
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:            # a dying UVC device can raise here
                pass
            self._cap = None
        self.opened = False
        with self._lock:
            self._fps = 0.0

    def health(self) -> dict:
        """Camera state for /api/status."""
        return {
            "opened": self.opened,
            "fps": self.fps,
            "reopens": self.reopens,
            "last_error": self.last_error,
            "source": self.cfg.source,
        }

    def read(self):
        """Return (sequence, frame). Frame is None until the first capture."""
        with self._lock:
            return self._seq, self._frame

    @property
    def fps(self) -> float:
        with self._lock:
            return round(self._fps, 1)
