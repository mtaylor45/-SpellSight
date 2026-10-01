"""Threaded USB / CSI camera capture.

Always hands back the most recent frame rather than a queued one, so
gesture tracking never falls behind the wand.
"""

from __future__ import annotations

import logging
import subprocess
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
        self.v4l2_applied: dict = {}
        self.v4l2_failed: dict = {}
        self._v4l2_warned = False

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
        # Applied on every open, not just the first: a USB re-enumeration resets
        # every control to its default, which looks exactly like slow drift.
        self.apply_v4l2_controls()
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

    # -- exposure lock (spec R2.1) ----------------------------------------

    def device_path(self) -> str | None:
        """The /dev node for this source, or None if it is not a local device."""
        src = self.cfg.source
        if isinstance(src, int) or (isinstance(src, str) and src.isdigit()):
            return f"/dev/video{int(src)}"
        if isinstance(src, str) and src.startswith("/dev/"):
            return src
        return None

    def _v4l2(self, args: list[str]) -> tuple[bool, str]:
        try:
            done = subprocess.run(["v4l2-ctl", *args], capture_output=True,
                                  text=True, timeout=5)
        except FileNotFoundError:
            return False, "v4l2-ctl not installed"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if done.returncode != 0:
            return False, (done.stderr or done.stdout).strip().splitlines()[:1] and \
                          (done.stderr or done.stdout).strip().splitlines()[0] or "failed"
        return True, done.stdout.strip()

    def _resolve_names(self, device: str) -> dict[str, str]:
        """Map our control names onto whatever this driver actually calls them.

        UVC renamed exposure_auto to auto_exposure and exposure_absolute to
        exposure_time_absolute in newer kernels. Guessing wrong means silently
        not locking exposure, which is the failure this whole control exists to
        prevent, so ask the driver instead.
        """
        ok, listing = self._v4l2(["-d", device, "--list-ctrls"])
        available = set()
        if ok:
            for line in listing.splitlines():
                name = line.strip().split(" ", 1)[0]
                if name:
                    available.add(name)
        def pick(*names):
            for name in names:
                if name in available:
                    return name
            return names[0] if not available else ""
        return {
            "auto_exposure": pick("exposure_auto", "auto_exposure"),
            "exposure": pick("exposure_absolute", "exposure_time_absolute"),
            "gain": pick("gain"),
            "awb": pick("white_balance_temperature_auto", "white_balance_automatic"),
        }

    def apply_v4l2_controls(self) -> dict:
        """Lock exposure, gain and white balance, and read the values back.

        Never raises: a control this driver does not have is a warning naming it,
        and no v4l2-ctl at all (a dev laptop, macOS, this test environment) is one
        INFO line. Neither is a reason to stop recognizing spells.
        """
        self.v4l2_applied, self.v4l2_failed = {}, {}
        if not self.cfg.lock_exposure:
            return self.v4l2_applied
        device = self.device_path()
        if device is None:
            log.info("Camera source %r is not a local device; skipping v4l2 controls",
                     self.cfg.source)
            return self.v4l2_applied
        if not self._v4l2(["--version"])[0]:
            if not self._v4l2_warned:
                log.info("v4l2-ctl not available; leaving camera controls at driver defaults")
                self._v4l2_warned = True
            return self.v4l2_applied

        names = self._resolve_names(device)
        wanted: list[tuple[str, object]] = []
        if names["auto_exposure"]:
            # 1 = manual on the classic exposure_auto enum, and on auto_exposure.
            wanted.append((names["auto_exposure"], 1))
        if names["exposure"]:
            wanted.append((names["exposure"], self.cfg.exposure_absolute))
        if names["gain"]:
            wanted.append((names["gain"], self.cfg.gain))
        if names["awb"]:
            wanted.append((names["awb"], 0 if not self.cfg.auto_white_balance else 1))
        wanted.extend(self.cfg.v4l2_extra.items())

        for control, value in wanted:
            ok, err = self._v4l2(["-d", device, f"--set-ctrl={control}={value}"])
            if not ok:
                log.warning("Camera control %s=%s did not apply: %s", control, value, err)
                self.v4l2_failed[control] = err
                continue
            # Read it back: setting a control is not the same as it having stuck.
            ok, readback = self._v4l2(["-d", device, f"--get-ctrl={control}"])
            actual = readback.split(":", 1)[1].strip() if ok and ":" in readback else "?"
            self.v4l2_applied[control] = actual
            if ok and actual not in ("?", str(value)):
                log.warning("Camera control %s set to %s but reads back %s",
                            control, value, actual)
        if self.v4l2_applied:
            log.info("Camera controls locked: %s",
                     ", ".join(f"{k}={v}" for k, v in sorted(self.v4l2_applied.items())))
        return self.v4l2_applied

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
            "controls": dict(self.v4l2_applied),
            "controls_failed": dict(self.v4l2_failed),
        }

    def read(self):
        """Return (sequence, frame). Frame is None until the first capture."""
        with self._lock:
            return self._seq, self._frame

    @property
    def fps(self) -> float:
        with self._lock:
            return round(self._fps, 1)
