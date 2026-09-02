#!/usr/bin/env python3
"""Tracker and recognizer regression tests. No camera, no broker.

Run after any change to tracker.py or recognizer.py:

    python smoke_test.py
"""

from __future__ import annotations

import math
import random
import shutil
import sys
import tempfile
from pathlib import Path

import json
import logging
import time

import numpy as np

from wandportal.mqtt_bridge import MqttBridge
from wandportal.recognizer import Recognizer, cosine, normalize, resample
from wandportal.tracker import Tracker

# The corrupt-templates test deliberately triggers an error log. Silence it so
# expected noise doesn't read as a failure.
logging.disable(logging.CRITICAL)

PASSED: list[str] = []
FAILED: list[str] = []

print("smoke_test: tracker + recognizer, no hardware\n")


def check(name: str):
    def decorator(fn):
        try:
            fn()
        except AssertionError as exc:
            FAILED.append(f"{name}: {exc}")
            print(f"  FAIL  {name}\n        {exc}")
        except Exception as exc:  # a crash is a failure too
            FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
        else:
            PASSED.append(name)
            print(f"  ok    {name}")
        return fn
    return decorator


# ---- gesture shapes ------------------------------------------------------

def line(x0, y0, x1, y1, n=40):
    return [(x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * i / (n - 1)) for i in range(n)]


def circle(cx, cy, r, n=48, clockwise=True):
    sign = 1.0 if clockwise else -1.0
    return [
        (cx + r * math.cos(sign * 2 * math.pi * i / (n - 1)),
         cy + r * math.sin(sign * 2 * math.pi * i / (n - 1)))
        for i in range(n)
    ]


def zigzag(x0, y0, n=45):
    pts = []
    for i in range(n):
        t = i / (n - 1)
        pts.append((x0 + 200 * t, y0 + (60 if (i // (n // 3)) % 2 else -60) * t))
    return pts


SHAPES = {
    "swipe_right": line(120, 240, 380, 240),
    "swipe_up": line(250, 360, 250, 120),
    "circle_cw": circle(260, 240, 90),
    "zigzag": zigzag(140, 230),
}


def jitter(points, noise=3.0, scale=1.0, dx=0.0, dy=0.0, rng=None):
    """Perturb a path the way a real cast differs from the one before it."""
    rng = rng or random
    return [
        (x * scale + dx + rng.gauss(0, noise), y * scale + dy + rng.gauss(0, noise))
        for x, y in points
    ]


def frame_with_dot(point, width=640, height=480, radius=3):
    frame = np.zeros((height, width), dtype=np.uint8)
    if point is not None:
        x, y = int(round(point[0])), int(round(point[1]))
        frame[max(0, y - radius):y + radius + 1, max(0, x - radius):x + radius + 1] = 255
    return frame


def run_gesture(tracker, points, fps=30.0, trailing_blank=10, t0=0.0):
    """Feed a path through the tracker frame by frame and return the gesture it emits."""
    step = 1.0 / fps
    now = t0
    result = None
    rejected = None
    for point in points:
        update = tracker.process(frame_with_dot(point), now=now)
        result = update.gesture or result
        rejected = update.rejected or rejected
        now += step
    for _ in range(trailing_blank):
        update = tracker.process(frame_with_dot(None), now=now)
        result = update.gesture or result
        rejected = update.rejected or rejected
        now += step
    return result, rejected


# ---- resampling and normalization ---------------------------------------

@check("resample returns evenly spaced points")
def _():
    pts = resample(line(0, 0, 100, 0, n=7), 33)
    assert len(pts) == 33, f"got {len(pts)} points"
    gaps = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    spread = max(gaps) - min(gaps)
    assert spread < 0.5, f"uneven spacing, spread {spread:.3f}"


@check("normalize is invariant to translation and uniform scale")
def _():
    base = normalize(SHAPES["circle_cw"])
    moved = normalize([(x + 300, y - 120) for x, y in SHAPES["circle_cw"]])
    scaled = normalize([(x * 2.5, y * 2.5) for x, y in SHAPES["circle_cw"]])
    assert cosine(base, moved) > 0.999, f"translation changed it: {cosine(base, moved):.4f}"
    assert cosine(base, scaled) > 0.999, f"scale changed it: {cosine(base, scaled):.4f}"


@check("normalize preserves aspect ratio (a flat swipe stays flat)")
def _():
    flat = normalize(line(0, 100, 300, 100))
    square = normalize(circle(0, 0, 100))
    assert cosine(flat, square) < 0.5, "a flat swipe matched a circle"


@check("rotation invariance is off: up-stroke and down-stroke differ")
def _():
    up = normalize(line(250, 360, 250, 120))
    down = normalize(line(250, 120, 250, 360))
    similarity = cosine(up, down)
    assert similarity < 0.5, f"up and down look alike ({similarity:.3f}) — rotation invariance leaked in"


@check("degenerate paths produce a zero vector, not a crash")
def _():
    assert not normalize([(5.0, 5.0)] * 30).any(), "a stationary point produced a nonzero vector"
    assert not normalize([]).any(), "an empty path produced a nonzero vector"


# ---- tracker -------------------------------------------------------------

@check("tracker segments a swipe into one gesture")
def _():
    gesture, rejected = run_gesture(Tracker(), SHAPES["swipe_right"])
    assert gesture is not None, f"no gesture emitted (rejected: {rejected})"
    assert gesture.frames >= 12, f"only {gesture.frames} frames"
    assert gesture.path_length > 200, f"path only {gesture.path_length:.0f}px"
    assert 0.5 < gesture.duration < 3.0, f"duration {gesture.duration:.2f}s"


@check("tracker rejects a flick that is too short to be a cast")
def _():
    gesture, rejected = run_gesture(Tracker(), line(300, 240, 310, 244, n=5))
    assert gesture is None, "a 10px flick was accepted as a cast"
    assert rejected in ("too_few_points", "too_short"), f"unexpected reason {rejected!r}"


@check("tracker ignores a blob larger than max_area (a lamp)")
def _():
    tracker = Tracker(max_area=200.0)
    lamp = np.zeros((480, 640), dtype=np.uint8)
    lamp[100:160, 100:160] = 255          # 3600px, far over max_area
    update = tracker.process(lamp, now=0.0)
    assert update.blob is None, f"tracked a {update.blob.area:.0f}px blob"


@check("tracker holds the trail through a brief dropout")
def _():
    tracker = Tracker(lost_frames=6)
    now = 0.0
    for point in SHAPES["swipe_right"][:20]:
        tracker.process(frame_with_dot(point), now=now); now += 1 / 30
    for _ in range(3):                     # 3 lost frames, under lost_frames
        update = tracker.process(frame_with_dot(None), now=now); now += 1 / 30
        assert update.gesture is None, "gesture ended during a brief dropout"
    assert tracker.current_points, "the trail was discarded"


@check("threshold_mode fixed uses the configured threshold unchanged")
def _():
    tracker = Tracker(threshold=211)
    gray = np.full((480, 640), 205, dtype=np.uint8)
    assert tracker.working_threshold(gray) == 211, "fixed mode altered the threshold"
    update = tracker.process(gray, now=0.0)
    assert update.threshold == 211, f"reported {update.threshold}"
    assert update.blob is None, "a uniformly sub-threshold frame produced a blob"


# ---- recognition ---------------------------------------------------------

def trained_recognizer(path, rng, samples_per_spell=5):
    recognizer = Recognizer(path, min_confidence=0.85, min_margin=0.06)
    for spell_id, shape in SHAPES.items():
        for _ in range(samples_per_spell):
            recognizer.add_sample(spell_id, jitter(shape, noise=2.5, rng=rng))
    return recognizer


@check("40/40 jittered casts across four shapes are recognized")
def _():
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        recognizer = trained_recognizer(Path(tmp) / "t.json", rng)
        correct = 0
        total = 0
        worst = 1.0
        for spell_id, shape in SHAPES.items():
            for i in range(10):
                cast = jitter(
                    shape, noise=4.0,
                    scale=rng.uniform(0.7, 1.4),
                    dx=rng.uniform(-90, 90), dy=rng.uniform(-70, 70),
                    rng=rng,
                )
                result = recognizer.classify(cast)
                total += 1
                if result.accepted and result.spell_id == spell_id:
                    correct += 1
                    worst = min(worst, result.confidence)
                else:
                    print(f"        miss {spell_id} -> {result.spell_id} "
                          f"{result.confidence:.3f} ({result.reason})")
        print(f"        {correct}/{total} recognized, lowest accepted confidence {worst:.3f}")
        assert correct == total, f"{correct}/{total}"


@check("random noise is rejected, not forced into a spell")
def _():
    rng = random.Random(11)
    with tempfile.TemporaryDirectory() as tmp:
        recognizer = trained_recognizer(Path(tmp) / "t.json", rng)
        confidences = []
        for _ in range(20):
            noise = [(rng.uniform(0, 640), rng.uniform(0, 480)) for _ in range(40)]
            result = recognizer.classify(noise)
            confidences.append(result.confidence)
            assert not result.accepted, (
                f"random noise accepted as {result.spell_id} at {result.confidence:.3f}"
            )
        print(f"        mean confidence on noise: {sum(confidences) / len(confidences):.3f}")


@check("the margin gate rejects a cast between two similar spells")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        recognizer = Recognizer(Path(tmp) / "t.json", min_confidence=0.5, min_margin=0.25)
        recognizer.add_sample("lumos", line(100, 240, 340, 240))
        recognizer.add_sample("nox", line(100, 250, 340, 236))
        result = recognizer.classify(line(100, 245, 340, 238))
        assert not result.accepted, f"accepted {result.spell_id} with margin {result.margin:.3f}"
        assert result.reason == "low_margin", f"reason was {result.reason!r}"


@check("min_confidence rejects an untrained gesture")
def _():
    rng = random.Random(3)
    with tempfile.TemporaryDirectory() as tmp:
        recognizer = trained_recognizer(Path(tmp) / "t.json", rng)
        result = recognizer.classify(circle(260, 240, 90, clockwise=False))
        assert not (result.accepted and result.spell_id == "circle_cw"), (
            f"a counter-clockwise circle matched the clockwise one at {result.confidence:.3f}"
        )


@check("templates round-trip through disk")
def _():
    rng = random.Random(5)
    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "templates.json"
        original = trained_recognizer(path, rng)
        original.save()
        assert path.exists(), "no templates file written"

        reloaded = Recognizer(path)
        reloaded.load()
        assert reloaded.trained_spells() == original.trained_spells(), "spell set changed"
        assert sum(reloaded.sample_count(s) for s in reloaded.trained_spells()) == 20

        cast = jitter(SHAPES["zigzag"], noise=3.0, rng=rng)
        assert reloaded.classify(cast).spell_id == original.classify(cast).spell_id, \
            "reloaded templates classify differently"
    finally:
        shutil.rmtree(tmp)


@check("a truncated templates file does not take the process down")
def _():
    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "templates.json"
        path.write_text('{"version": 1, "spells": {"lumos": [{"id": "a", "poi')
        recognizer = Recognizer(path)
        recognizer.load()
        assert recognizer.trained_spells() == [], "loaded samples from a corrupt file"
    finally:
        shutil.rmtree(tmp)


@check("save is atomic: no .tmp file survives a completed write")
def _():
    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "templates.json"
        recognizer = Recognizer(path)
        recognizer.add_sample("lumos", SHAPES["swipe_right"])
        recognizer.save()
        leftovers = list(tmp.glob("*.tmp"))
        assert not leftovers, f"left behind {leftovers}"
        import json
        json.loads(path.read_text())     # must be valid JSON, not a truncated write
    finally:
        shutil.rmtree(tmp)


@check("separation reports leave-one-out accuracy and names confusions")
def _():
    rng = random.Random(13)
    with tempfile.TemporaryDirectory() as tmp:
        recognizer = trained_recognizer(Path(tmp) / "t.json", rng)
        report = recognizer.separation()
        assert report["total"] == 20, f"checked {report['total']} samples"
        assert report["accuracy"] >= 0.9, f"leave-one-out accuracy {report['accuracy']}"
        assert set(report["per_spell"]) == set(SHAPES), "per-spell breakdown is incomplete"
        print(f"        leave-one-out {report['correct']}/{report['total']}, "
              f"confusions: {report['confusions'] or 'none'}")


@check("samples per spell are capped, oldest dropped first")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        recognizer = Recognizer(Path(tmp) / "t.json", max_samples_per_spell=3)
        ids = [recognizer.add_sample("lumos", SHAPES["swipe_right"]).id for _ in range(5)]
        stored = [s.id for s in recognizer.samples["lumos"]]
        assert stored == ids[-3:], f"kept {stored}, expected the newest three {ids[-3:]}"


# ---- end to end ----------------------------------------------------------

@check("tracker to recognizer: a traced cast is recognized")
def _():
    rng = random.Random(17)
    with tempfile.TemporaryDirectory() as tmp:
        recognizer = Recognizer(Path(tmp) / "t.json")
        for spell_id, shape in SHAPES.items():
            for _ in range(4):
                gesture, rejected = run_gesture(Tracker(), jitter(shape, noise=1.5, rng=rng))
                assert gesture is not None, f"training gesture lost for {spell_id}: {rejected}"
                recognizer.add_sample(spell_id, gesture.points)

        for spell_id, shape in SHAPES.items():
            gesture, rejected = run_gesture(Tracker(), jitter(shape, noise=2.0, rng=rng))
            assert gesture is not None, f"cast lost for {spell_id}: {rejected}"
            result = recognizer.classify(gesture.points)
            assert result.accepted and result.spell_id == spell_id, (
                f"{spell_id} read as {result.spell_id} at {result.confidence:.3f} ({result.reason})"
            )


# ---- the frozen MQTT interface ------------------------------------------
#
# SPEC.md section 4 freezes these topic shapes, entity object_ids and payload
# keys. Home Assistant automations are written against them, so a change here
# silently breaks a user's house. These tests exist to make that change loud.

class StubClient:
    """Stands in for paho, capturing what would go on the wire."""

    def __init__(self):
        self.published: list[tuple[str, str, bool, int]] = []
        self.will: tuple | None = None

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain, qos))

    def topics(self):
        return [t for t, _, _, _ in self.published]

    def payload(self, topic):
        for t, payload, _, _ in self.published:
            if t == topic:
                return payload
        raise AssertionError(f"nothing published to {topic}; got {self.topics()}")

    def retained(self, topic):
        for t, _, retain, _ in self.published:
            if t == topic:
                return retain
        raise AssertionError(f"nothing published to {topic}")


def stub_bridge(**kwargs):
    bridge = MqttBridge(pulse_seconds=0.05, **kwargs)
    client = StubClient()
    bridge._client = client
    bridge.connected = True
    return bridge, client


@check("a cast publishes the four frozen spell topics")
def _():
    bridge, client = stub_bridge()
    assert bridge.publish_spell("lumos", 0.93, 1.25) is True, "publish reported failure"

    assert client.payload("wand/spell/lumos/state") == "ON", "state topic payload changed"
    assert client.retained("wand/spell/lumos/state") is False, "the state topic must not be retained"
    assert client.payload("wand/last_spell") == "Lumos", "last_spell must carry the display name"
    assert client.retained("wand/last_spell") is True, "last_spell must be retained"
    assert client.retained("wand/last_spell/attributes") is True, "attributes must be retained"
    assert client.retained("wand/event") is False, "wand/event must not be retained"

    attributes = json.loads(client.payload("wand/last_spell/attributes"))
    assert set(attributes) == {"spell_id", "confidence", "duration", "effect", "cast_at"}, \
        f"attribute keys changed: {sorted(attributes)}"
    assert attributes["spell_id"] == "lumos" and attributes["confidence"] == 0.93

    event = json.loads(client.payload("wand/event"))
    assert set(event) == {"spell", "name", "confidence"}, f"event keys changed: {sorted(event)}"


@check("the spell pulse turns the binary_sensor back off")
def _():
    bridge, client = stub_bridge()
    bridge.publish_spell("nox", 0.9, 1.0)
    assert ("wand/spell/nox/state", "OFF", False, 0) not in client.published, "OFF was immediate"
    time.sleep(0.25)                       # pulse_seconds is 0.05 in the stub
    assert ("wand/spell/nox/state", "OFF", False, 0) in client.published, \
        f"no OFF after the pulse: {client.published}"


@check("discovery publishes one device and the documented entity ids")
def _():
    bridge, client = stub_bridge()
    bridge.publish_discovery(["lumos"])

    config = json.loads(client.payload("homeassistant/binary_sensor/wand/lumos/config"))
    assert config["object_id"] == "wand_lumos", f"object_id changed to {config['object_id']}"
    assert config["unique_id"] == "wand_lumos", f"unique_id changed to {config['unique_id']}"
    assert config["state_topic"] == "wand/spell/lumos/state", "discovery points at the wrong topic"
    assert config["availability_topic"] == "wand/status", "availability topic changed"
    assert config["device"]["identifiers"] == ["wand"], \
        f"device identifiers changed to {config['device']['identifiers']}"

    sensor = json.loads(client.payload("homeassistant/sensor/wand/last_spell/config"))
    assert sensor["object_id"] == "wand_last_spell", f"object_id changed to {sensor['object_id']}"
    assert sensor["json_attributes_topic"] == "wand/last_spell/attributes", "attributes topic changed"
    assert sensor["device"]["identifiers"] == ["wand"], "the sensor is on a different device"
    assert client.retained("homeassistant/binary_sensor/wand/lumos/config") is True, \
        "discovery configs must be retained"


@check("removing discovery clears the retained config")
def _():
    bridge, client = stub_bridge()
    bridge.remove_discovery(["lumos"])
    assert client.payload("homeassistant/binary_sensor/wand/lumos/config") == "", \
        "an empty payload is how Home Assistant is told to forget an entity"
    assert client.retained("homeassistant/binary_sensor/wand/lumos/config") is True, \
        "the clear must itself be retained, or it won't stick"


@check("a custom base_topic moves every topic together")
def _():
    bridge, client = stub_bridge(base_topic="attic_wand")
    bridge.publish_spell("lumos", 0.9, 1.0)
    assert all(t.startswith("attic_wand/") for t in client.topics()), \
        f"topics escaped the base: {client.topics()}"


@check("casts during a broker outage are dropped and counted, not queued")
def _():
    bridge = MqttBridge(enabled=False)
    assert bridge.publish_spell("lumos", 0.9, 1.0) is False, "claimed to publish with no client"
    assert bridge.stats()["dropped"] >= 4, f"drops not counted: {bridge.stats()}"
    assert bridge.stats()["connected"] is False, "reported a connection it does not have"


def main() -> int:
    total = len(PASSED) + len(FAILED)
    print(f"\n{len(PASSED)}/{total} passed")
    if FAILED:
        print("\nFailures:")
        for failure in FAILED:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
