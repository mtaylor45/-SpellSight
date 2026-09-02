"""Gesture recognition by normalized template matching.

A $1 Recognizer variant. Each cast is resampled to a fixed number of evenly
spaced points, scaled uniformly, centered, flattened and unit-normalized, then
compared to stored samples by cosine similarity.

Rotation normalization is deliberately absent. For wand casting an up-stroke and
a down-stroke should be two different spells, and indicative rotation would
collapse them into one. Do not "fix" this.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

log = logging.getLogger(__name__)

Point = tuple[float, float]

TEMPLATES_VERSION = 1


@dataclass
class Sample:
    """One recorded training trace for a spell."""

    id: str
    points: list[Point]
    created: float
    vector: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "created": self.created,
            "points": [[round(float(x), 3), round(float(y), 3)] for x, y in self.points],
        }


@dataclass
class MatchResult:
    """The outcome of classifying one cast.

    `accepted` is the only field the engine acts on; the rest exist so the
    console can explain a rejection instead of just swallowing it.
    """

    spell_id: str | None
    confidence: float
    margin: float
    accepted: bool
    reason: str
    runner_up: str | None = None
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "spell": self.spell_id,
            "confidence": round(self.confidence, 4),
            "margin": round(self.margin, 4),
            "accepted": self.accepted,
            "reason": self.reason,
            "runner_up": self.runner_up,
            "scores": {k: round(v, 4) for k, v in sorted(
                self.scores.items(), key=lambda kv: kv[1], reverse=True)[:5]},
        }


def resample(points: Sequence[Point], n: int) -> list[Point]:
    """Resample a path to `n` points spaced evenly along its arc length.

    This is what makes recognition independent of how fast the wand moved: a
    slow cast and a fast one trace the same shape and produce the same vector.
    """
    if n < 2:
        raise ValueError("resample needs at least 2 points")
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 2:
        return [pts[0] if pts else (0.0, 0.0)] * n

    segments = [
        float(np.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))
        for i in range(len(pts) - 1)
    ]
    total = sum(segments)
    if total <= 0:
        # A path with no length at all (wand held perfectly still). Callers
        # reject this downstream via the degenerate-vector check.
        return [pts[0]] * n

    interval = total / (n - 1)
    out: list[Point] = [pts[0]]
    distance = 0.0
    i = 0
    current = pts[0]
    while i < len(pts) - 1 and len(out) < n:
        seg_len = float(np.hypot(pts[i + 1][0] - current[0], pts[i + 1][1] - current[1]))
        if seg_len <= 0:
            i += 1
            current = pts[i]
            continue
        if distance + seg_len >= interval:
            t = (interval - distance) / seg_len
            nxt = (
                current[0] + t * (pts[i + 1][0] - current[0]),
                current[1] + t * (pts[i + 1][1] - current[1]),
            )
            out.append(nxt)
            current = nxt
            distance = 0.0
        else:
            distance += seg_len
            i += 1
            current = pts[i]

    while len(out) < n:
        out.append(pts[-1])
    return out[:n]


def normalize(points: Sequence[Point], n: int = 64) -> np.ndarray:
    """Resample, scale uniformly, center, flatten, and unit-normalize.

    Scaling is uniform rather than per-axis so a flat horizontal swipe stays
    flat instead of being stretched into a square and colliding with every other
    gesture.
    """
    pts = np.asarray(resample(points, n), dtype=np.float64)
    span = pts.max(axis=0) - pts.min(axis=0)
    scale = float(span.max())
    if scale <= 1e-9:
        return np.zeros(n * 2, dtype=np.float64)
    pts = pts / scale
    pts = pts - pts.mean(axis=0)
    vector = pts.reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-9:
        return np.zeros(n * 2, dtype=np.float64)
    return vector / norm


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two already unit-normalized vectors, clamped to >= 0."""
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    return max(0.0, float(np.dot(a, b)))


class Recognizer:
    """Stores trained samples and classifies casts against them."""

    def __init__(
        self,
        templates_path: str | Path = "data/templates.json",
        resample_points: int = 64,
        min_confidence: float = 0.85,
        min_margin: float = 0.06,
        max_samples_per_spell: int = 12,
    ) -> None:
        self.templates_path = Path(templates_path)
        self.resample_points = resample_points
        self.min_confidence = min_confidence
        self.min_margin = min_margin
        self.max_samples_per_spell = max_samples_per_spell
        self.samples: dict[str, list[Sample]] = {}

    # ---- persistence -----------------------------------------------------

    def load(self) -> None:
        """Load templates from disk. A missing file is a fresh install, not an error."""
        if not self.templates_path.exists():
            log.info("No templates file at %s, starting empty", self.templates_path)
            return
        try:
            raw = json.loads(self.templates_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Could not read templates from %s: %s", self.templates_path, exc)
            return

        stored_n = int(raw.get("resample_points", self.resample_points))
        spells = raw.get("spells", {})
        loaded = 0
        for spell_id, entries in spells.items():
            samples: list[Sample] = []
            for entry in entries:
                points = [(float(p[0]), float(p[1])) for p in entry.get("points", [])]
                if len(points) < 2:
                    continue
                samples.append(
                    Sample(
                        id=str(entry.get("id") or uuid.uuid4().hex[:8]),
                        points=points,
                        created=float(entry.get("created", time.time())),
                    )
                )
            if samples:
                self.samples[spell_id] = samples
                loaded += len(samples)
        if stored_n != self.resample_points:
            log.info(
                "Templates stored at %d points, recomputing for %d",
                stored_n, self.resample_points,
            )
        self._rebuild_vectors()
        log.info("Loaded %d samples across %d spells", loaded, len(self.samples))

    def save(self) -> None:
        """Write templates atomically.

        Write-then-rename so a power cut mid-save leaves the previous file
        intact rather than a truncated one. This device gets unplugged.
        """
        self.templates_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": TEMPLATES_VERSION,
            "resample_points": self.resample_points,
            "saved_at": time.time(),
            "spells": {
                spell_id: [s.to_json() for s in samples]
                for spell_id, samples in sorted(self.samples.items())
                if samples
            },
        }
        tmp = self.templates_path.with_suffix(self.templates_path.suffix + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.templates_path)

    def _rebuild_vectors(self) -> None:
        for samples in self.samples.values():
            for sample in samples:
                sample.vector = normalize(sample.points, self.resample_points)

    # ---- training --------------------------------------------------------

    def add_sample(self, spell_id: str, points: Sequence[Point]) -> Sample:
        """Record one training trace, dropping the oldest if the spell is full."""
        sample = Sample(
            id=uuid.uuid4().hex[:8],
            points=[(float(x), float(y)) for x, y in points],
            created=time.time(),
        )
        sample.vector = normalize(sample.points, self.resample_points)
        bucket = self.samples.setdefault(spell_id, [])
        bucket.append(sample)
        if len(bucket) > self.max_samples_per_spell:
            del bucket[0 : len(bucket) - self.max_samples_per_spell]
        return sample

    def delete_sample(self, spell_id: str, sample_id: str) -> bool:
        bucket = self.samples.get(spell_id)
        if not bucket:
            return False
        for i, sample in enumerate(bucket):
            if sample.id == sample_id:
                del bucket[i]
                if not bucket:
                    del self.samples[spell_id]
                return True
        return False

    def clear_spell(self, spell_id: str) -> int:
        removed = len(self.samples.pop(spell_id, []))
        return removed

    def trained_spells(self) -> list[str]:
        return sorted(k for k, v in self.samples.items() if v)

    def sample_count(self, spell_id: str) -> int:
        return len(self.samples.get(spell_id, []))

    # ---- classification --------------------------------------------------

    def score_all(self, points: Sequence[Point], exclude: str | None = None) -> dict[str, float]:
        """Best cosine score per spell. `exclude` skips one sample id, for leave-one-out."""
        vector = normalize(points, self.resample_points)
        if not np.any(vector):
            return {}
        scores: dict[str, float] = {}
        for spell_id, samples in self.samples.items():
            best = 0.0
            for sample in samples:
                if exclude is not None and sample.id == exclude:
                    continue
                best = max(best, cosine(vector, sample.vector))
            if best > 0.0:
                scores[spell_id] = best
        return scores

    def classify(self, points: Sequence[Point], exclude: str | None = None) -> MatchResult:
        """Classify a cast.

        Two gates, not one: the best match must clear `min_confidence` *and* beat
        the runner-up by `min_margin`. The margin gate is what stops a sloppy
        wave from picking arbitrarily between two similar spells.
        """
        if not self.samples:
            return MatchResult(None, 0.0, 0.0, False, "no_templates")

        scores = self.score_all(points, exclude=exclude)
        if not scores:
            return MatchResult(None, 0.0, 0.0, False, "degenerate")

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_id, best_score = ranked[0]
        runner_up_id, runner_up_score = (ranked[1] if len(ranked) > 1 else (None, 0.0))
        margin = best_score - runner_up_score

        if best_score < self.min_confidence:
            reason = "low_confidence"
        elif len(ranked) > 1 and margin < self.min_margin:
            reason = "low_margin"
        else:
            reason = "ok"

        return MatchResult(
            spell_id=best_id,
            confidence=best_score,
            margin=margin,
            accepted=(reason == "ok"),
            reason=reason,
            runner_up=runner_up_id,
            scores=scores,
        )

    # ---- diagnostics -----------------------------------------------------

    def separation(self) -> dict:
        """Leave-one-out check over every stored sample.

        Names the pairs that read as each other, which is the honest answer to
        "can I add another spell?" — better than lowering min_confidence until
        things stop failing.
        """
        total = 0
        correct = 0
        confusions: dict[str, int] = {}
        per_spell: dict[str, dict] = {}

        # With a single sample in the whole store, leave-one-out has nothing to
        # compare against and every result would be a meaningless miss.
        if sum(len(v) for v in self.samples.values()) < 2:
            return {
                "total": 0, "correct": 0, "accuracy": 0.0,
                "confusions": [], "per_spell": {},
                "note": "need at least two samples to check separation",
            }

        for spell_id, samples in self.samples.items():
            spell_total = 0
            spell_correct = 0
            confidences: list[float] = []
            for sample in samples:
                result = self.classify(sample.points, exclude=sample.id)
                total += 1
                spell_total += 1
                confidences.append(result.confidence)
                if result.spell_id == spell_id and result.accepted:
                    correct += 1
                    spell_correct += 1
                elif result.spell_id and result.spell_id != spell_id:
                    key = " / ".join(sorted((spell_id, result.spell_id)))
                    confusions[key] = confusions.get(key, 0) + 1
            per_spell[spell_id] = {
                "samples": len(samples),
                "correct": spell_correct,
                "total": spell_total,
                "mean_confidence": round(
                    float(np.mean(confidences)) if confidences else 0.0, 4
                ),
            }

        return {
            "total": total,
            "correct": correct,
            "accuracy": round(correct / total, 4) if total else 0.0,
            "confusions": [
                {"pair": pair, "count": count}
                for pair, count in sorted(confusions.items(), key=lambda kv: -kv[1])
            ],
            "per_spell": per_spell,
        }
