#!/usr/bin/env python3
"""Full HTTP API test against a synthetic camera. No hardware, no broker.

Runs the real uvicorn server on a loopback port and drives it with urllib, so
this exercises the actual serving stack rather than a test double of it. Uses
only the standard library on the client side — no test-only dependency.

    python api_test.py
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from wandportal.camera import SyntheticCamera
from wandportal.config import Config
from wandportal.engine import Engine
from wandportal.mqtt_bridge import MqttBridge
from wandportal.recognizer import Recognizer
from wandportal.server import create_app
from wandportal.tracker import Tracker

logging.disable(logging.CRITICAL)

PASSED: list[str] = []
FAILED: list[str] = []

BASE = ""
CAMERA: SyntheticCamera
ENGINE: Engine


# ---- tiny HTTP client ----------------------------------------------------

def request(method: str, path: str, body: dict | None = None, raw: bool = False):
    """Return (status, parsed_body). Never raises on an HTTP error status."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = response.read()
            if raw:
                return response.status, payload
            return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, payload
        try:
            return exc.code, json.loads(payload) if payload else None
        except json.JSONDecodeError:
            return exc.code, payload


def get(path, raw=False):
    return request("GET", path, raw=raw)


def post(path, body=None):
    return request("POST", path, body if body is not None else {})


def delete(path, body=None):
    return request("DELETE", path, body)


TESTS: list = []


def check(name: str):
    """Register a test to run once the server is up.

    Unlike smoke_test's, these can't run at import time — they need a live
    server and a started engine, so registration and execution are separate.
    """
    def decorator(fn):
        def run() -> None:
            try:
                fn()
            except AssertionError as exc:
                FAILED.append(f"{name}: {exc}")
                print(f"  FAIL  {name}\n        {exc}")
            except Exception as exc:
                FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
            else:
                PASSED.append(name)
                print(f"  ok    {name}")
        TESTS.append(run)
        return fn
    return decorator


# ---- driving the synthetic camera ---------------------------------------

def cast(points, hold: float = 0.02, blank: float = 0.35) -> None:
    """Walk the synthetic wand tip along a path, then take it out of frame.

    Paced slower than the camera's frame rate so the engine sees every step,
    which is what a real 30fps capture would give it.
    """
    for point in points:
        CAMERA.set_position(point)
        time.sleep(hold)
    CAMERA.set_position(None)
    time.sleep(blank)


def swipe_right(n=26):
    return [(140 + i * 9, 240) for i in range(n)]


def swipe_down(n=26):
    return [(300, 120 + i * 9) for i in range(n)]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"server did not come up at {url}")


# ---- tests ---------------------------------------------------------------

@check("GET / serves the console")
def _():
    status, body = get("/", raw=True)
    assert status == 200, f"status {status}"
    assert b"Wand" in body and b"</html>" in body, "console HTML looks wrong"


@check("GET /api/status reports a running engine")
def _():
    status, body = get("/api/status")
    assert status == 200, f"status {status}"
    assert body["running"] is True, "engine is not running"
    assert body["mode"] == "cast", f"mode {body['mode']}"
    assert body["camera"]["synthetic"] is True, "not using the synthetic camera"
    assert body["mqtt"]["connected"] is False, "expected no broker"
    for key in ("threshold", "working_threshold", "active", "candidates"):
        assert key in body["tracker"], f"status is missing tracker.{key}"


@check("GET /api/spells lists the whole catalog")
def _():
    status, body = get("/api/spells")
    assert status == 200, f"status {status}"
    assert len(body["spells"]) == 45, f"{len(body['spells'])} spells"
    ids = [s["id"] for s in body["spells"]]
    assert "lumos" in ids and "nox" in ids, "catalog is missing the obvious ones"
    assert all({"id", "name", "effect", "samples", "trained"} <= set(s) for s in body["spells"])


@check("an unknown spell id is a 404, not a 500")
def _():
    for method, path in (
        ("GET", "/api/spells/avada_kedavro/samples"),
        ("POST", "/api/spells/avada_kedavro/test"),
        ("DELETE", "/api/spells/avada_kedavro/samples"),
    ):
        status, _ = request(method, path, {} if method == "POST" else None)
        assert status == 404, f"{method} {path} returned {status}"


@check("training records samples from traced casts")
def _():
    status, body = post("/api/train/start", {"spell": "lumos"})
    assert status == 200 and body["training"] == "lumos", f"{status} {body}"

    _, status_body = get("/api/status")
    assert status_body["mode"] == "train", "engine did not enter training mode"

    for _ in range(4):
        cast(swipe_right())

    status, body = post("/api/train/stop")
    assert status == 200, f"stop returned {status}"

    status, body = get("/api/spells/lumos/samples")
    assert status == 200, f"status {status}"
    assert len(body["samples"]) == 4, f"recorded {len(body['samples'])} samples, expected 4"
    assert all(s["points"] > 10 for s in body["samples"]), "samples have too few points"


@check("training samples reach disk without blocking the capture loop")
def _():
    # Saves during training are handed to a worker thread, so give it a moment
    # before asserting the file caught up.
    path = Path(ENGINE.recognizer.templates_path)
    deadline = time.time() + 3.0
    stored = None
    while time.time() < deadline:
        if path.exists():
            stored = json.loads(path.read_text())
            if len(stored.get("spells", {}).get("lumos", [])) == 4:
                break
        time.sleep(0.05)
    assert stored is not None, f"no templates file written at {path}"
    assert len(stored["spells"]["lumos"]) == 4, \
        f"disk has {len(stored['spells']['lumos'])} samples, memory has 4"
    assert stored["version"] == 1 and "resample_points" in stored, "template file shape changed"


@check("a sample renders as a trace thumbnail")
def _():
    _, body = get("/api/spells/lumos/samples")
    sample_id = body["samples"][0]["id"]
    status, payload = get(f"/api/spells/lumos/samples/{sample_id}/trace.png", raw=True)
    assert status == 200, f"status {status}"
    assert payload[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    status, _ = get("/api/spells/lumos/samples/deadbeef/trace.png", raw=True)
    assert status == 404, f"unknown sample returned {status}"


@check("a trained gesture is recognized and logged as a cast")
def _():
    before = get("/api/status")[1]["casts"]
    cast(swipe_right())
    time.sleep(0.4)
    _, body = get("/api/status")
    assert body["casts"] == before + 1, f"casts went {before} -> {body['casts']}"

    _, events = get("/api/events?limit=10")
    casts = [e for e in events["events"] if e["kind"] == "cast"]
    assert casts, f"no cast event logged: {events['events'][:3]}"
    assert casts[0]["spell"] == "lumos", f"recognized as {casts[0]['spell']}"
    assert casts[0]["confidence"] > 0.85, f"confidence {casts[0]['confidence']}"
    assert casts[0]["published"] is False, "claimed to publish with no broker attached"


@check("an untrained gesture is rejected with a reason")
def _():
    time.sleep(1.6)                       # clear the cooldown from the previous cast
    before = get("/api/status")[1]["rejections"]
    cast(swipe_down())
    time.sleep(0.4)
    _, body = get("/api/status")
    assert body["rejections"] == before + 1, f"rejections went {before} -> {body['rejections']}"
    _, events = get("/api/events?limit=10")
    rejected = [e for e in events["events"] if e["kind"] == "rejected"]
    assert rejected, "no rejection event logged"
    assert rejected[0]["reason"] in ("low_confidence", "low_margin"), \
        f"reason {rejected[0]['reason']!r}"


@check("the cooldown suppresses a repeated cast")
def _():
    time.sleep(1.6)
    cast(swipe_right())
    time.sleep(0.2)
    cast(swipe_right())
    time.sleep(0.4)
    _, events = get("/api/events?limit=15")
    assert any(e["kind"] == "cooldown" for e in events["events"]), \
        f"no cooldown event: {[e['kind'] for e in events['events'][:6]]}"


@check("tuning round-trips and takes effect")
def _():
    status, original = get("/api/tuning")
    assert status == 200 and "threshold" in original, f"{status} {original}"

    status, body = post("/api/tuning", {"threshold": 199, "min_confidence": 0.8})
    assert status == 200, f"status {status}"
    assert set(body["changed"]) == {"threshold", "min_confidence"}, f"changed {body['changed']}"
    assert body["persisted"] is False, "claimed to persist tuning, which R3.4 hasn't built yet"

    _, live = get("/api/status")
    assert live["tracker"]["threshold"] == 199, "threshold did not take effect"
    assert live["recognizer"]["min_confidence"] == 0.8, "min_confidence did not take effect"

    post("/api/tuning", {"threshold": original["threshold"],
                         "min_confidence": original["min_confidence"]})


@check("a bad tuning value is a 400, not a 500")
def _():
    status, _ = post("/api/tuning", {"threshold": "not-a-number"})
    assert status == 400, f"status {status}"
    _, live = get("/api/status")
    assert live["running"] is True, "a bad tuning value took the engine down"


@check("separation runs leave-one-out over stored samples")
def _():
    status, body = get("/api/separation")
    assert status == 200, f"status {status}"
    assert body["total"] >= 4, f"checked {body['total']} samples"
    assert "per_spell" in body and "confusions" in body, "report is missing sections"


@check("discovery is attempted and reports the broker state honestly")
def _():
    status, body = post("/api/discovery", {"spells": ["lumos"]})
    assert status == 200, f"status {status}"
    assert body["spells"] == ["lumos"], f"targets {body['spells']}"
    assert body["published"] == 0, "claimed to publish discovery with no broker"
    assert body["connected"] is False, "claimed a broker connection"

    status, body = delete("/api/discovery", {"spells": ["lumos"]})
    assert status == 200, f"status {status}"


@check("a test cast reports that it could not publish")
def _():
    status, body = post("/api/spells/nox/test")
    assert status == 200, f"status {status}"
    assert body["published"] is False, "claimed to publish with no broker"
    _, events = get("/api/events?limit=5")
    assert events["events"][0]["kind"] == "test", "test cast was not logged"


@check("a single frame renders in both cast and tune views")
def _():
    for mode in ("cast", "tune"):
        status, payload = get(f"/frame.jpg?mode={mode}", raw=True)
        assert status == 200, f"{mode} returned {status}"
        assert payload[:2] == b"\xff\xd8", f"{mode} frame is not a JPEG"
    status, _ = get("/frame.jpg?mode=sideways", raw=True)
    assert status == 422, f"an invalid mode returned {status}"


@check("the MJPEG stream emits multipart frames")
def _():
    req = urllib.request.Request(BASE + "/stream.mjpg?mode=cast")
    with urllib.request.urlopen(req, timeout=10) as response:
        assert response.status == 200, f"status {response.status}"
        assert "multipart/x-mixed-replace" in response.headers["Content-Type"], \
            response.headers["Content-Type"]
        chunk = response.read(2048)
    assert b"--wandframe" in chunk, "no multipart boundary in the stream"
    assert b"image/jpeg" in chunk, "no JPEG part in the stream"


@check("deleting a sample removes it and persists the change")
def _():
    _, body = get("/api/spells/lumos/samples")
    before = len(body["samples"])
    assert before >= 2, f"only {before} samples to work with"
    sample_id = body["samples"][0]["id"]

    status, body = delete(f"/api/spells/lumos/samples/{sample_id}")
    assert status == 200, f"status {status}"
    assert body["remaining"] == before - 1, f"remaining {body['remaining']}"

    status, _ = delete(f"/api/spells/lumos/samples/{sample_id}")
    assert status == 404, f"deleting twice returned {status}"

    stored = json.loads(Path(ENGINE.recognizer.templates_path).read_text())
    assert len(stored["spells"]["lumos"]) == before - 1, "the deletion was not written to disk"


@check("clearing a spell removes every sample")
def _():
    status, body = delete("/api/spells/lumos/samples")
    assert status == 200, f"status {status}"
    assert body["removed"] >= 1, f"removed {body['removed']}"
    _, body = get("/api/spells/lumos/samples")
    assert body["samples"] == [], "samples survived the clear"
    _, body = get("/api/status")
    assert "lumos" not in body["recognizer"]["trained_spells"], "still reported as trained"


def main() -> int:
    global BASE, CAMERA, ENGINE

    print("api_test: full HTTP API against a synthetic camera\n")

    tmp = tempfile.TemporaryDirectory()
    config = Config()
    config.mqtt.enabled = False
    config.engine.cooldown = 1.5
    config.recognizer.templates_path = str(Path(tmp.name) / "templates.json")

    CAMERA = SyntheticCamera(fps=60)
    ENGINE = Engine(
        config,
        CAMERA,
        Tracker(),
        Recognizer(
            config.recognizer.templates_path,
            min_confidence=config.recognizer.min_confidence,
            min_margin=config.recognizer.min_margin,
        ),
        MqttBridge(enabled=False),
    )
    ENGINE.start()

    port = free_port()
    BASE = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(create_app(ENGINE), host="127.0.0.1", port=port, log_level="critical")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        wait_for_server(BASE + "/api/status")
        for test in TESTS:
            test()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        ENGINE.stop()
        tmp.cleanup()

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
