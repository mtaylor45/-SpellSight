import math, tempfile, threading, time, types
import numpy as np, cv2

try:
    # starlette raises RuntimeError (not ImportError) when httpx is absent.
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as exc:
    raise SystemExit(
        "api_test needs its test-only dependency:\n"
        "    pip install -r requirements-dev.txt\n"
        f"\n(import failed with: {exc})"
    )
from wandportal import config as cfgmod
from wandportal.engine import Engine
from wandportal.server import create_app

cfg = cfgmod.load("config.yaml")
cfg.mqtt.enabled = False
cfg.recognizer.templates_path = tempfile.mktemp(suffix=".json")

eng = Engine(cfg)

# fake camera: a bright dot tracing an arc, then blank frames
frames = []
for i in range(40):
    t = math.pi + 1.6*math.pi*i/39
    f = np.zeros((480,640,3), np.uint8)
    cv2.circle(f, (int(320+90*math.cos(t)), int(240+90*math.sin(t))), 4, (255,255,255), -1)
    frames.append(f)
frames += [np.zeros((480,640,3), np.uint8) for _ in range(15)]

class FakeCam:
    fps = 30.0
    def __init__(self): self.i = 0
    def start(self): return self
    def stop(self): pass
    def read(self):
        f = frames[min(self.i, len(frames)-1)]
        self.i += 1
        return self.i, f
    def health(self):
        # Part of the frame-source contract, alongside start/stop/read.
        return {"opened": True, "fps": self.fps, "reopens": 0,
                "last_error": None, "source": "fake"}
eng.camera = FakeCam()
eng.start()

app = create_app(eng)
c = TestClient(app)

s = c.get("/api/status").json()
print("status keys:", sorted(s)[:6], "... spells:", len(s["spells"]))
assert s["mode"] == "run"
assert set(s["camera"]) == {"opened", "fps", "reopens", "last_error", "source"}, sorted(s["camera"])
assert s["camera"]["opened"] is True and s["camera"]["reopens"] == 0, s["camera"]
assert "camera_fps" in s, "the console's existing camera_fps key must survive"
# Ambient, the cutoff in force, and headroom — the numbers the hardware session
# reads. In fixed mode the cutoff must equal the configured threshold exactly.
for key in ("threshold_mode", "working_threshold", "ambient", "headroom"):
    assert key in s["tracker"], f"status is missing tracker.{key}"
assert s["tracker"]["threshold_mode"] == "fixed", s["tracker"]["threshold_mode"]
assert s["tracker"]["working_threshold"] == s["tracker"]["threshold"], s["tracker"]

r = c.post("/api/mode", json={"mode":"train","spell_id":"lumos"})
assert r.status_code == 200, r.text
assert r.json()["training_spell"] == "lumos"

# let the fake cast play through in training mode
eng.camera.i = 0
time.sleep(2.0)
print("history:", eng.status()["history"][:1])
assert eng.recognizer.counts().get("lumos"), "no sample recorded"

png = c.get("/api/samples/lumos/0.png")
assert png.status_code == 200 and png.content[:4] == b"\x89PNG", png.status_code
print("trace png bytes:", len(png.content))

r = c.post("/api/mode", json={"mode":"tune"}); assert r.status_code==200
r = c.post("/api/tune", json={"threshold": 240, "min_confidence": 0.9})
assert r.json()["tracker"]["threshold"] == 240
assert r.json()["recognizer"]["min_confidence"] == 0.9

# Out-of-range tuning is refused, and recognition keeps the values it had.
# These arrive from a slider on a phone; applied blindly they stop the wand
# being detected at all, which looks exactly like broken hardware.
for bad in ({"threshold": -5}, {"threshold": 9999}, {"min_confidence": 1.5},
            {"lost_frames": 0}, {"min_area": 0}, {"cooldown": -1}):
    resp = c.post("/api/tune", json=bad)
    assert resp.status_code == 422, f"{bad} returned {resp.status_code}"
    field = list(bad)[0]
    assert field in resp.text, f"422 for {bad} does not name the field: {resp.text[:160]}"
# An inverted area window passes per-field bounds but matches nothing.
resp = c.post("/api/tune", json={"min_area": 900, "max_area": 500})
assert resp.status_code == 422 and "max_area" in resp.text, resp.text[:160]
live = c.get("/api/status").json()
assert live["tracker"]["threshold"] == 240, live["tracker"]["threshold"]
assert live["recognizer"]["min_confidence"] == 0.9, live["recognizer"]["min_confidence"]
print("tuning bounds ok (6 rejected, state intact)")

assert c.post("/api/mode", json={"mode":"bogus"}).status_code == 400
assert c.post("/api/cast/lumos").status_code == 200      # mqtt off -> logs a warning
assert c.post("/api/cast/nosuch").status_code == 404
print("self-test:", c.post("/api/self-test").json())

assert c.get("/").status_code == 200 and b"Wand Portal" in c.get("/").content
assert c.delete("/api/samples/lumos/0").status_code == 200
assert c.delete("/api/samples/lumos").status_code == 200
assert len(eng.jpeg(70)) > 100
eng.stop()
print("API OK")
