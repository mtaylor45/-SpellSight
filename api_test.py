import math, tempfile, threading, time, types
import numpy as np, cv2
from fastapi.testclient import TestClient
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
eng.camera = FakeCam()
eng.start()

app = create_app(eng)
c = TestClient(app)

s = c.get("/api/status").json()
print("status keys:", sorted(s)[:6], "... spells:", len(s["spells"]))
assert s["mode"] == "run"

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
