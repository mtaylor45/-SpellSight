"""The runtime loop: capture -> track -> classify -> publish."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import cv2
import numpy as np

from .camera import Camera
from .config import Config
from .mqtt_bridge import MqttBridge
from .recognizer import Recognizer
from .spells import Spell, resolve
from .tracker import BlobTracker, TrackState, render_trace

log = logging.getLogger(__name__)

MODE_RUN = "run"
MODE_TRAIN = "train"
MODE_TUNE = "tune"

TRAIL_COLOR = (86, 186, 255)     # BGR amber
HIT_COLOR = (120, 255, 160)
MISS_COLOR = (90, 90, 235)


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.spells: list[Spell] = resolve(cfg.spells)
        self.spell_ids = [s.id for s in self.spells]
        self.by_id = {s.id: s for s in self.spells}

        self.camera = Camera(cfg.camera)
        self.tracker = BlobTracker(cfg.tracker)
        self.recognizer = Recognizer(cfg.recognizer)
        self.mqtt = MqttBridge(cfg.mqtt, self.spells)

        self.mode = MODE_RUN
        self.training_spell: str | None = None
        self.history: deque[dict] = deque(maxlen=25)
        self.last_trace: list[tuple[float, float]] = []
        self.last_result: dict | None = None
        self.error: str | None = None

        self._frame = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.camera.start()
        self.mqtt.start()
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self.mqtt.stop()
        self.camera.stop()

    # -- loop --------------------------------------------------------------

    def _loop(self) -> None:
        last_seq = -1
        while not self._stop.is_set():
            seq, frame = self.camera.read()
            if frame is None or seq == last_seq:
                time.sleep(0.004)
                continue
            last_seq = seq

            try:
                gesture = self.tracker.update(frame)
            except Exception as exc:  # pragma: no cover
                log.exception("Tracker error")
                self.error = str(exc)
                continue

            if gesture is not None:
                self._handle_gesture(gesture)

            annotated = self._annotate(frame)
            with self._lock:
                self._frame = annotated

    def _handle_gesture(self, gesture) -> None:
        self.last_trace = gesture.points

        if self.mode == MODE_TRAIN and self.training_spell:
            count = self.recognizer.add_sample(self.training_spell, gesture.points)
            spell = self.by_id.get(self.training_spell)
            entry = {
                "at": time.time(),
                "kind": "sample",
                "spell_id": self.training_spell,
                "name": spell.name if spell else self.training_spell,
                "count": count,
                "duration": round(gesture.duration, 2),
            }
            log.info("Recorded sample %d for %s", count, self.training_spell)
        else:
            match = self.recognizer.classify(gesture.points, enabled=self.spell_ids)
            spell = self.by_id.get(match.spell_id) if match.spell_id else None
            published = False
            if match.accepted and spell and self.mode == MODE_RUN:
                self.mqtt.cast(spell, match.confidence, gesture.duration)
                published = True
            entry = {
                "at": time.time(),
                "kind": "cast" if match.accepted else "rejected",
                "spell_id": match.spell_id,
                "name": spell.name if spell else "Unrecognized",
                "confidence": round(match.confidence, 3),
                "runner_up": match.runner_up,
                "runner_up_confidence": round(match.runner_up_confidence, 3),
                "reason": match.rejected_reason,
                "published": published,
                "duration": round(gesture.duration, 2),
            }
            log.info(
                "Gesture: %s conf=%.3f %s",
                match.spell_id, match.confidence, match.rejected_reason or "-> cast",
            )

        self.last_result = entry
        self.history.appendleft(entry)

    # -- rendering ---------------------------------------------------------

    def _annotate(self, frame):
        if self.mode == MODE_TUNE and self.tracker.mask is not None:
            out = cv2.cvtColor(self.tracker.mask, cv2.COLOR_GRAY2BGR)
        else:
            out = frame.copy() if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            out = (out * 0.55).astype(np.uint8)

        pts = self.tracker.points
        if len(pts) > 1:
            arr = np.array(pts, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(out, [arr], False, TRAIL_COLOR, 3, cv2.LINE_AA)
        elif self.tracker.state is TrackState.COOLDOWN and len(self.last_trace) > 1:
            arr = np.array(self.last_trace, dtype=np.int32).reshape(-1, 1, 2)
            color = HIT_COLOR if (self.last_result or {}).get("kind") in ("cast", "sample") else MISS_COLOR
            cv2.polylines(out, [arr], False, color, 3, cv2.LINE_AA)

        d = self.tracker.detection
        if d is not None:
            cv2.circle(out, (int(d[0]), int(d[1])), 9, (255, 255, 255), 2, cv2.LINE_AA)

        label = self.tracker.state.value
        if self.mode == MODE_TRAIN and self.training_spell:
            spell = self.by_id.get(self.training_spell)
            label = f"training {spell.name if spell else self.training_spell}"
        cv2.putText(out, label, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (235, 235, 235), 1, cv2.LINE_AA)
        return out

    def jpeg(self, quality: int = 70) -> bytes | None:
        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None

    def trace_png(self, spell_id: str, index: int) -> bytes | None:
        traces = self.recognizer.points_for(spell_id)
        if index < 0 or index >= len(traces):
            return None
        img = render_trace(traces[index])
        ok, buf = cv2.imencode(".png", img)
        return buf.tobytes() if ok else None

    # -- control -----------------------------------------------------------

    def set_mode(self, mode: str, spell_id: str | None = None) -> None:
        if mode not in (MODE_RUN, MODE_TRAIN, MODE_TUNE):
            raise ValueError(f"unknown mode {mode!r}")
        if mode == MODE_TRAIN:
            if spell_id not in self.by_id:
                raise ValueError(f"{spell_id!r} is not an enabled spell")
            self.training_spell = spell_id
        else:
            self.training_spell = None
        self.mode = mode
        self.tracker.reset()

    def status(self) -> dict:
        counts = self.recognizer.counts()
        return {
            "mode": self.mode,
            "training_spell": self.training_spell,
            "camera_fps": self.camera.fps,
            "tracker_state": self.tracker.state.value,
            "detecting": self.tracker.detection is not None,
            "mqtt_connected": self.mqtt.connected,
            "mqtt_enabled": self.cfg.mqtt.enabled,
            "error": self.error,
            "tracker": {
                "threshold": self.cfg.tracker.threshold,
                "min_area": self.cfg.tracker.min_area,
                "max_area": self.cfg.tracker.max_area,
                "lost_frames": self.cfg.tracker.lost_frames,
                "min_path_length": self.cfg.tracker.min_path_length,
                "cooldown": self.cfg.tracker.cooldown,
            },
            "recognizer": {
                "min_confidence": self.cfg.recognizer.min_confidence,
                "min_margin": self.cfg.recognizer.min_margin,
            },
            "spells": [
                {**s.as_dict(), "samples": counts.get(s.id, 0)} for s in self.spells
            ],
            "history": list(self.history)[:12],
        }
