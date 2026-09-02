"""The recognition loop: capture, track, classify, publish.

Nothing in `_loop` may block. MQTT publishing is fire-and-forget, the pulse-off
timer runs on its own thread, and template writes are handed to a save worker —
an SD card stalling mid-write must not cost a frame. Saves requested from HTTP
handlers stay synchronous, because there the caller is a request thread and a
200 should mean the change is on disk.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Sequence

import cv2
import numpy as np

from .camera import FrameSource
from .config import Config
from .mqtt_bridge import MqttBridge
from .recognizer import Point, Recognizer
from .spells import SPELLS_BY_ID, spell_name
from .tracker import Tracker

log = logging.getLogger(__name__)

TRAIL_COLOR = (72, 182, 255)
BLOB_COLOR = (255, 255, 255)


def render_trace(points: Sequence[Point], width: int = 160, height: int = 120, pad: int = 12) -> np.ndarray:
    """Draw a gesture path onto a small canvas, fitted to it.

    Used for the training thumbnails, which are how you spot a bad sample
    without replaying video.
    """
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    if len(points) < 2:
        return canvas
    pts = np.asarray(points, dtype=np.float64)
    lo = pts.min(axis=0)
    span = pts.max(axis=0) - lo
    scale = max(float(span.max()), 1e-6)
    usable = min(width, height) - 2 * pad
    pts = (pts - lo) / scale * usable
    pts[:, 0] += (width - usable) / 2.0
    pts[:, 1] += (height - usable) / 2.0
    poly = pts.astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(canvas, [poly], False, TRAIL_COLOR, 2, cv2.LINE_AA)
    cv2.circle(canvas, tuple(poly[0][0]), 3, (120, 255, 140), -1)
    cv2.circle(canvas, tuple(poly[-1][0]), 3, (120, 160, 255), -1)
    return canvas


class Engine:
    """Owns the camera, tracker, recognizer and MQTT bridge, and the loop between them."""

    def __init__(
        self,
        config: Config,
        camera: FrameSource,
        tracker: Tracker,
        recognizer: Recognizer,
        mqtt: MqttBridge,
    ) -> None:
        self.config = config
        self.camera = camera
        self.tracker = tracker
        self.recognizer = recognizer
        self.mqtt = mqtt

        self.cooldown = config.engine.cooldown
        self.events: deque[dict] = deque(maxlen=config.engine.event_history)
        self.lock = threading.RLock()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._save_thread: threading.Thread | None = None
        self._save_requested = threading.Event()
        self._last_frame: np.ndarray | None = None
        self._last_frame_id = 0
        self._last_update = None
        self._last_publish = 0.0
        self._started_at = time.time()
        self._loop_frames = 0
        self._loop_fps = 0.0

        self.training_spell: str | None = None
        self.casts = 0
        self.rejections = 0

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.recognizer.load()
        self.camera.start()
        self.mqtt.start()
        self._stop.clear()
        self._save_thread = threading.Thread(target=self._save_loop, name="save", daemon=True)
        self._save_thread.start()
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()
        log.info("Watching for spells")

    def stop(self) -> None:
        self._stop.set()
        self._save_requested.set()          # wake the save worker so it can exit
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._save_thread:
            self._save_thread.join(timeout=3.0)
        self.camera.stop()
        self.mqtt.stop()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- the loop --------------------------------------------------------

    def _loop(self) -> None:
        last_tick = time.monotonic()
        while not self._stop.is_set():
            frame = self.camera.read()
            if frame is None or frame is self._last_frame:
                # No new frame yet. A short sleep here is the difference between
                # an idle Pi and a pegged core.
                time.sleep(0.002)
                continue
            self._last_frame = frame
            self._last_frame_id += 1

            now = time.monotonic()
            try:
                update = self.tracker.process(frame, now=now)
            except Exception:
                log.exception("Tracker failed on a frame; skipping it")
                continue
            self._last_update = update

            if update.gesture is not None:
                try:
                    self._handle_gesture(update.gesture)
                except Exception:
                    log.exception("Failed to handle a completed gesture")
            elif update.rejected:
                self._record({"kind": "discarded", "reason": update.rejected})

            self._loop_frames += 1
            delta = now - last_tick
            last_tick = now
            if delta > 0:
                instant = 1.0 / delta
                self._loop_fps = (
                    instant if self._loop_fps == 0.0 else 0.9 * self._loop_fps + 0.1 * instant
                )

    def _save_loop(self) -> None:
        """Persist templates off the capture loop, coalescing bursts of requests."""
        while not self._stop.is_set():
            if not self._save_requested.wait(timeout=0.5):
                continue
            self._save_requested.clear()
            if self._stop.is_set():
                break
            try:
                self.save_templates()
            except OSError as exc:
                log.error("Could not save templates: %s", exc)

    def save_templates(self) -> None:
        """Write templates to disk. Safe to call from any thread."""
        with self.lock:
            self.recognizer.save()

    def request_save(self) -> None:
        """Ask the save worker to persist templates soon. Never blocks."""
        self._save_requested.set()

    def _handle_gesture(self, gesture) -> None:
        if self.training_spell:
            self._record_sample(gesture)
            return

        with self.lock:
            result = self.recognizer.classify(gesture.points)

        if not result.accepted:
            self.rejections += 1
            self._record(
                {
                    "kind": "rejected",
                    "spell": result.spell_id,
                    "name": spell_name(result.spell_id) if result.spell_id else None,
                    "reason": result.reason,
                    "confidence": round(result.confidence, 4),
                    "margin": round(result.margin, 4),
                    "duration": round(gesture.duration, 3),
                    "points": len(gesture.points),
                }
            )
            return

        elapsed = time.monotonic() - self._last_publish
        if elapsed < self.cooldown:
            self._record(
                {
                    "kind": "cooldown",
                    "spell": result.spell_id,
                    "name": spell_name(result.spell_id or ""),
                    "reason": "cooldown",
                    "confidence": round(result.confidence, 4),
                }
            )
            return

        self._last_publish = time.monotonic()
        self.casts += 1
        published = self.mqtt.publish_spell(
            result.spell_id or "", result.confidence, gesture.duration
        )
        self._record(
            {
                "kind": "cast",
                "spell": result.spell_id,
                "name": spell_name(result.spell_id or ""),
                "confidence": round(result.confidence, 4),
                "margin": round(result.margin, 4),
                "duration": round(gesture.duration, 3),
                "points": len(gesture.points),
                "published": published,
            }
        )
        log.info(
            "Cast %s at %.3f (margin %.3f)%s",
            result.spell_id, result.confidence, result.margin,
            "" if published else " [not published: no broker]",
        )

    def _record_sample(self, gesture) -> None:
        spell_id = self.training_spell
        if not spell_id:
            return
        with self.lock:
            sample = self.recognizer.add_sample(spell_id, gesture.points)
        self.request_save()
        self._record(
            {
                "kind": "sample",
                "spell": spell_id,
                "name": spell_name(spell_id),
                "sample_id": sample.id,
                "points": len(gesture.points),
                "duration": round(gesture.duration, 3),
            }
        )
        log.info("Recorded sample %s for %s", sample.id, spell_id)

    def _record(self, event: dict) -> None:
        event["at"] = time.time()
        with self.lock:
            self.events.appendleft(event)

    # ---- training --------------------------------------------------------

    def start_training(self, spell_id: str) -> None:
        if spell_id not in SPELLS_BY_ID:
            raise KeyError(spell_id)
        self.training_spell = spell_id
        self.tracker.reset()
        log.info("Training %s", spell_id)

    def stop_training(self) -> None:
        if self.training_spell:
            log.info("Stopped training %s", self.training_spell)
        self.training_spell = None
        self.tracker.reset()

    # ---- views for the console ------------------------------------------

    def frame(self, mode: str = "cast") -> np.ndarray | None:
        """The current frame rendered for the stream.

        `tune` shows the raw threshold mask, which is what you actually need to
        see when deciding whether the wand tip is the only white dot in frame.
        """
        frame = self._last_frame
        if frame is None:
            return None
        if mode == "tune":
            mask = self.tracker.mask(frame)
            view = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        else:
            view = frame.copy()

        update = self._last_update
        points = self.tracker.current_points if update and update.active else self.tracker.last_trace
        if len(points) >= 2:
            poly = np.asarray(points, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(view, [poly], False, TRAIL_COLOR, 2, cv2.LINE_AA)
        if update and update.blob is not None:
            cv2.circle(view, (int(update.blob.x), int(update.blob.y)), 10, BLOB_COLOR, 1)
        return view

    def status(self) -> dict[str, Any]:
        update = self._last_update
        blob = None
        if update and update.blob is not None:
            blob = {
                "x": round(update.blob.x, 1),
                "y": round(update.blob.y, 1),
                "area": round(update.blob.area, 1),
            }
        with self.lock:
            trained = self.recognizer.trained_spells()
            samples = {s: self.recognizer.sample_count(s) for s in trained}
            last_event = self.events[0] if self.events else None
        return {
            "running": self.running,
            "mode": "train" if self.training_spell else "cast",
            "training_spell": self.training_spell,
            "uptime_s": round(time.time() - self._started_at, 1),
            "loop_fps": round(self._loop_fps, 1),
            "casts": self.casts,
            "rejections": self.rejections,
            "camera": self.camera.stats(),
            "mqtt": self.mqtt.stats(),
            "tracker": {
                "threshold_mode": self.tracker.threshold_mode,
                "threshold": self.tracker.threshold,
                # The threshold actually in force. Identical to `threshold` in
                # fixed mode; SPEC.md R2.2 makes these diverge.
                "working_threshold": update.threshold if update else self.tracker.threshold,
                "active": bool(update.active) if update else False,
                "candidates": update.candidates if update else 0,
                "blob": blob,
                "trace_points": len(self.tracker.last_trace),
            },
            "recognizer": {
                "trained_spells": trained,
                "samples": samples,
                "total_samples": sum(samples.values()),
                "min_confidence": self.recognizer.min_confidence,
                "min_margin": self.recognizer.min_margin,
                "resample_points": self.recognizer.resample_points,
            },
            "last_event": last_event,
        }

    def tuning(self) -> dict[str, Any]:
        """The values the console's Tune panel edits."""
        t = self.tracker
        return {
            "threshold": t.threshold,
            "min_area": t.min_area,
            "max_area": t.max_area,
            "max_jump": t.max_jump,
            "smoothing": t.smoothing,
            "start_frames": t.start_frames,
            "lost_frames": t.lost_frames,
            "min_points": t.min_points,
            "min_path_length": t.min_path_length,
            "max_gesture_seconds": t.max_gesture_seconds,
            "min_confidence": self.recognizer.min_confidence,
            "min_margin": self.recognizer.min_margin,
            "cooldown": self.cooldown,
        }

    def apply_tuning(self, values: dict) -> list[str]:
        """Apply tuning values from the console.

        In-memory only — SPEC.md R3.4 adds writing these back to the YAML. Say so
        in the console rather than letting an hour of tuning quietly evaporate on
        the next restart.
        """
        changed: list[str] = []
        with self.lock:
            tracker_values = {k: v for k, v in values.items() if hasattr(self.tracker, k)}
            changed.extend(self.tracker.apply(tracker_values))
            for key in ("min_confidence", "min_margin"):
                if key in values:
                    new = float(values[key])
                    if new != getattr(self.recognizer, key):
                        setattr(self.recognizer, key, new)
                        changed.append(key)
            if "cooldown" in values:
                new = float(values["cooldown"])
                if new != self.cooldown:
                    self.cooldown = new
                    changed.append("cooldown")
        return changed

    def test_cast(self, spell_id: str) -> bool:
        """Publish a spell as though it had been cast. Used to wire up automations
        before any training exists."""
        if spell_id not in SPELLS_BY_ID:
            raise KeyError(spell_id)
        published = self.mqtt.publish_spell(spell_id, 1.0, 0.0)
        self._record(
            {
                "kind": "test",
                "spell": spell_id,
                "name": spell_name(spell_id),
                "published": published,
            }
        )
        return published
