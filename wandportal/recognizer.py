"""Gesture recognition by normalized template matching.

A $1-Recognizer variant: resample each path to a fixed point count,
normalize scale and position, flatten to a unit vector, and compare with
cosine similarity. Five clean samples per spell is usually enough, which
matters when you're training forty spells rather than two.

Rotation normalization is OFF by default — for wand casting, "up-left"
and "down-right" are different spells, and rotation invariance would
merge them.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import RecognizerConfig


@dataclass
class Match:
    spell_id: str | None
    confidence: float
    runner_up: str | None = None
    runner_up_confidence: float = 0.0
    rejected_reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.spell_id is not None and not self.rejected_reason


# -- geometry --------------------------------------------------------------


def resample(points: list[tuple[float, float]], n: int) -> np.ndarray:
    arr = np.asarray(points, dtype=np.float64)
    # Drop consecutive duplicates, which break interval walking.
    keep = [0]
    for i in range(1, len(arr)):
        if math.dist(arr[i], arr[keep[-1]]) > 1e-9:
            keep.append(i)
    arr = arr[keep]
    if len(arr) < 2:
        return np.repeat(arr, n, axis=0)[:n]

    seg = np.linalg.norm(np.diff(arr, axis=0), axis=1)
    total = float(seg.sum())
    if total <= 0:
        return np.repeat(arr[:1], n, axis=0)

    cumulative = np.concatenate([[0.0], np.cumsum(seg)])
    targets = np.linspace(0.0, total, n)
    out = np.empty((n, 2), dtype=np.float64)
    out[:, 0] = np.interp(targets, cumulative, arr[:, 0])
    out[:, 1] = np.interp(targets, cumulative, arr[:, 1])
    return out


def _rotate_to_zero(pts: np.ndarray) -> np.ndarray:
    centroid = pts.mean(axis=0)
    v = pts[0] - centroid
    theta = -math.atan2(v[1], v[0])
    c, s = math.cos(theta), math.sin(theta)
    rot = np.array([[c, -s], [s, c]])
    return (pts - centroid) @ rot.T + centroid


def normalize(
    points: list[tuple[float, float]],
    n: int = 64,
    rotation_invariant: bool = False,
) -> np.ndarray:
    """Path -> unit-length feature vector of length 2n."""
    pts = resample(points, n)
    if rotation_invariant:
        pts = _rotate_to_zero(pts)

    # Uniform scale (preserves aspect ratio, so a flat swipe stays flat).
    span = float((pts.max(axis=0) - pts.min(axis=0)).max())
    if span > 1e-9:
        pts = pts / span
    pts = pts - pts.mean(axis=0)

    vec = pts.reshape(-1)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 1e-9 else vec


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two unit vectors, clamped to 0..1."""
    return float(max(0.0, min(1.0, np.dot(a, b))))


# -- store -----------------------------------------------------------------


class Recognizer:
    """Holds training samples and classifies new gestures against them."""

    def __init__(self, cfg: RecognizerConfig):
        self.cfg = cfg
        self.path = Path(cfg.templates_path)
        self._lock = threading.Lock()
        # spell_id -> list of {"points": [[x,y],...], "added": ts}
        self.samples: dict[str, list[dict]] = {}
        self._vectors: dict[str, list[np.ndarray]] = {}
        self.load()

    # -- persistence

    def load(self) -> None:
        with self._lock:
            if self.path.is_file():
                data = json.loads(self.path.read_text() or "{}")
                self.samples = data.get("samples", {})
            self._rebuild()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "resample_points": self.cfg.resample_points,
                "rotation_invariant": self.cfg.rotation_invariant,
                "samples": self.samples,
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self.path)

    def _rebuild(self) -> None:
        self._vectors = {
            sid: [
                normalize(s["points"], self.cfg.resample_points, self.cfg.rotation_invariant)
                for s in entries
                if len(s.get("points", [])) >= 2
            ]
            for sid, entries in self.samples.items()
        }

    # -- training

    def add_sample(self, spell_id: str, points: list[tuple[float, float]]) -> int:
        with self._lock:
            entry = {
                "points": [[round(float(x), 2), round(float(y), 2)] for x, y in points],
                "added": time.time(),
            }
            self.samples.setdefault(spell_id, []).append(entry)
            self._vectors.setdefault(spell_id, []).append(
                normalize(points, self.cfg.resample_points, self.cfg.rotation_invariant)
            )
            count = len(self.samples[spell_id])
        self.save()
        return count

    def delete_sample(self, spell_id: str, index: int) -> bool:
        with self._lock:
            entries = self.samples.get(spell_id)
            if not entries or index < 0 or index >= len(entries):
                return False
            entries.pop(index)
            if not entries:
                self.samples.pop(spell_id, None)
            self._rebuild()
        self.save()
        return True

    def clear_spell(self, spell_id: str) -> None:
        with self._lock:
            self.samples.pop(spell_id, None)
            self._rebuild()
        self.save()

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {sid: len(v) for sid, v in self.samples.items()}

    def points_for(self, spell_id: str) -> list[list[list[float]]]:
        with self._lock:
            return [s["points"] for s in self.samples.get(spell_id, [])]

    # -- classification

    def classify(self, points: list[tuple[float, float]], enabled: list[str] | None = None) -> Match:
        vec = normalize(points, self.cfg.resample_points, self.cfg.rotation_invariant)

        with self._lock:
            scores: dict[str, float] = {}
            for sid, templates in self._vectors.items():
                if enabled is not None and sid not in enabled:
                    continue
                if not templates:
                    continue
                scores[sid] = max(similarity(vec, t) for t in templates)

        if not scores:
            return Match(None, 0.0, rejected_reason="no templates trained")

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_id, best = ranked[0]
        second_id, second = ranked[1] if len(ranked) > 1 else (None, 0.0)

        if best < self.cfg.min_confidence:
            return Match(best_id, best, second_id, second,
                         rejected_reason=f"below confidence ({best:.2f} < {self.cfg.min_confidence:.2f})")
        if second_id and (best - second) < self.cfg.min_margin:
            return Match(best_id, best, second_id, second,
                         rejected_reason=f"too close to {second_id} ({best - second:.3f} margin)")
        return Match(best_id, best, second_id, second)

    def self_test(self, enabled: list[str] | None = None) -> dict:
        """Leave-one-out check across stored samples — how separable are the spells?"""
        with self._lock:
            vectors = {
                sid: list(v) for sid, v in self._vectors.items()
                if (enabled is None or sid in enabled) and v
            }

        total = correct = 0
        confusions: dict[str, dict[str, int]] = {}
        for sid, templates in vectors.items():
            for i, vec in enumerate(templates):
                scores = {}
                for other, others in vectors.items():
                    pool = [t for j, t in enumerate(others) if not (other == sid and j == i)]
                    if pool:
                        scores[other] = max(similarity(vec, t) for t in pool)
                if not scores:
                    continue
                pred = max(scores, key=scores.get)
                total += 1
                if pred == sid:
                    correct += 1
                else:
                    confusions.setdefault(sid, {}).setdefault(pred, 0)
                    confusions[sid][pred] += 1

        return {
            "samples": total,
            "correct": correct,
            "accuracy": round(correct / total, 3) if total else 0.0,
            "confusions": confusions,
        }
