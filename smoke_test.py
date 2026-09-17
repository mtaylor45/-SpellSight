"""Synthetic end-to-end check: no camera, no broker."""
import math, random, tempfile, os, sys
import numpy as np

from wandportal import config as cfgmod
from wandportal.recognizer import Recognizer, normalize, resample
from wandportal.tracker import BlobTracker, render_trace, path_length
from wandportal.spells import resolve, CATALOG, BY_ID

# --- config
cfg = cfgmod.load("config.yaml")
assert cfg.mqtt.base_topic == "wand"
os.environ["WAND_TRACKER_THRESHOLD"] = "231"
cfg2 = cfgmod.load("config.yaml")
assert cfg2.tracker.threshold == 231, cfg2.tracker.threshold
del os.environ["WAND_TRACKER_THRESHOLD"]
print("config ok")

# --- spells
sp = resolve(["lumos","nox"]); assert len(sp)==2
print(f"catalog ok ({len(CATALOG)} spells)")

# --- gesture generators with jitter, like a real hand
def jitter(pts, n=1.5):
    return [(x+random.gauss(0,n), y+random.gauss(0,n)) for x,y in pts]

def arc(t0,t1,r=100,cx=200,cy=200,steps=40):
    return [(cx+r*math.cos(t0+(t1-t0)*i/steps), cy+r*math.sin(t0+(t1-t0)*i/steps)) for i in range(steps+1)]

def line(x0,y0,x1,y1,steps=30):
    return [(x0+(x1-x0)*i/steps, y0+(y1-y0)*i/steps) for i in range(steps+1)]

def zigzag():
    return line(100,100,200,180)+line(200,180,100,260)+line(100,260,200,340)

shapes = {
  "lumos":      lambda: arc(math.pi, 2.6*math.pi),          # loop
  "nox":        lambda: line(300,100,100,300),              # diagonal down-left
  "alohomora":  lambda: line(100,200,300,200)+line(300,200,300,320), # L
  "colloportus":lambda: zigzag(),
}

cfg.recognizer.templates_path = tempfile.mktemp(suffix=".json")
rec = Recognizer(cfg.recognizer)
random.seed(7)
for sid, fn in shapes.items():
    for _ in range(5):
        rec.add_sample(sid, jitter(fn()))
print("trained:", rec.counts())

# --- persistence round trip
rec2 = Recognizer(cfg.recognizer)
assert rec2.counts() == rec.counts()

# --- classification of fresh, jittered casts
ok = 0; trials = 0
for sid, fn in shapes.items():
    for _ in range(10):
        trials += 1
        m = rec2.classify(jitter(fn(), 3.0), enabled=list(shapes))
        if m.accepted and m.spell_id == sid: ok += 1
        elif not m.accepted: print("  rejected", sid, round(m.confidence,3), m.rejected_reason)
        else: print("  WRONG", sid, "->", m.spell_id, round(m.confidence,3))
print(f"live accuracy {ok}/{trials}")

# --- a garbage wave should be rejected
noise = [(random.uniform(0,400), random.uniform(0,400)) for _ in range(40)]
m = rec2.classify(noise, enabled=list(shapes))
print("noise ->", m.spell_id, round(m.confidence,3), "accepted:", m.accepted)

print("self-test:", rec2.self_test(enabled=list(shapes)))

# --- tracker against synthetic IR frames
import cv2
tr = BlobTracker(cfg.tracker)
gest = None
pts = arc(math.pi, 2.6*math.pi)
for i, (x,y) in enumerate(pts):
    f = np.zeros((480,640), np.uint8)
    cv2.circle(f, (int(x),int(y)), 4, 255, -1)
    g = tr.update(f)
    assert g is None
for _ in range(cfg.tracker.lost_frames + 1):
    g = tr.update(np.zeros((480,640), np.uint8))
    if g: gest = g
assert gest is not None, "tracker never emitted a gesture"
print(f"tracker ok: {len(gest.points)} pts, {gest.path_length:.0f}px")
img = render_trace(gest.points); assert img.shape == (200,200)
m = rec2.classify(gest.points, enabled=list(shapes))
print("tracked loop ->", m.spell_id, round(m.confidence,3))

# --- resample edge cases
assert resample([(1,1),(1,1)], 8).shape == (8,2)
assert abs(np.linalg.norm(normalize(line(0,0,10,10)))-1) < 1e-9
print("edge cases ok")

# --- the frozen MQTT interface (SPEC.md section 4)
# Topic shapes, retain flags, payload keys and entity object_ids are what Home
# Assistant automations are written against. These assertions exist so that
# changing one fails here instead of in somebody's house.
import json, time
from wandportal.mqtt_bridge import MqttBridge

class StubClient:
    """Stands in for paho, capturing what would go on the wire."""
    def __init__(self): self.sent = []
    def publish(self, topic, payload="", qos=0, retain=False):
        self.sent.append((topic, payload, retain))
    def topics(self): return [t for t, _, _ in self.sent]
    def find(self, topic):
        for t, payload, retain in self.sent:
            if t == topic:
                return payload, retain
        raise AssertionError(f"nothing published to {topic!r}; got {self.topics()}")

mcfg = cfgmod.load("config.yaml").mqtt
mcfg.pulse_seconds = 0.05
bridge = MqttBridge(mcfg, resolve(["lumos", "nox"]))
stub = StubClient(); bridge._client = stub; bridge.connected = True

assert bridge.t_status == "wand/status", bridge.t_status
assert bridge.t_last == "wand/last_spell", bridge.t_last
assert bridge.t_attrs == "wand/last_spell/attributes", bridge.t_attrs
assert bridge.t_event == "wand/event", bridge.t_event
assert bridge.t_spell("lumos") == "wand/spell/lumos/state", bridge.t_spell("lumos")

bridge.cast(BY_ID["lumos"], 0.93, 1.25)
payload, retain = stub.find("wand/spell/lumos/state")
assert payload == "ON" and retain is False, (payload, retain)
payload, retain = stub.find("wand/last_spell")
assert payload == "Lumos" and retain is True, (payload, retain)
attrs, retain = stub.find("wand/last_spell/attributes")
assert retain is True, "last_spell attributes must be retained"
attrs = json.loads(attrs)
assert set(attrs) == {"spell_id", "confidence", "duration", "effect", "cast_at"}, sorted(attrs)
assert attrs["spell_id"] == "lumos" and attrs["confidence"] == 0.93, attrs
event, retain = stub.find("wand/event")
assert retain is False, "wand/event must not be retained"
assert set(json.loads(event)) == {"spell", "name", "confidence"}, sorted(json.loads(event))

time.sleep(0.25)   # pulse_seconds elapses on its own timer thread
assert ("wand/spell/lumos/state", "OFF", False) in stub.sent, "no OFF after the pulse"

stub.sent.clear()
bridge.publish_discovery()
cfgd, retain = stub.find("homeassistant/binary_sensor/wand/lumos/config")
assert retain is True, "discovery configs must be retained"
cfgd = json.loads(cfgd)
assert cfgd["object_id"] == "wand_lumos", cfgd["object_id"]
assert cfgd["unique_id"] == "wand_lumos", cfgd["unique_id"]
assert cfgd["state_topic"] == "wand/spell/lumos/state", cfgd["state_topic"]
assert cfgd["availability_topic"] == "wand/status", cfgd["availability_topic"]
assert cfgd["device"]["identifiers"] == ["wand"], cfgd["device"]["identifiers"]
sensor = json.loads(stub.find("homeassistant/sensor/wand/last_spell/config")[0])
assert sensor["object_id"] == "wand_last_spell", sensor["object_id"]
assert sensor["json_attributes_topic"] == "wand/last_spell/attributes", sensor
assert sensor["device"]["identifiers"] == ["wand"], "entities must share one device"

stub.sent.clear()
bridge.remove_discovery()
cleared, retain = stub.find("homeassistant/binary_sensor/wand/lumos/config")
assert cleared == "" and retain is True, "clearing discovery needs an empty retained payload"

# A renamed base_topic must move every topic together, not just some.
alt = cfgmod.load("config.yaml").mqtt
alt.base_topic = "attic_wand"
b2 = MqttBridge(alt, resolve(["lumos"]))
s2 = StubClient(); b2._client = s2; b2.connected = True
b2.cast(BY_ID["lumos"], 0.9, 1.0)
assert all(t.startswith("attic_wand/") for t in s2.topics()), s2.topics()

# With no broker a cast is dropped, not queued and fired later in a burst.
b3 = MqttBridge(cfgmod.load("config.yaml").mqtt, resolve(["lumos"]))
b3.cast(BY_ID["lumos"], 0.9, 1.0)          # _client is None until start()
assert not b3.connected
print("mqtt contract ok (SPEC.md section 4)")

# --- camera resilience (plan G1, G4)
# The capture loop is driven against a stub VideoCapture so an absent camera and
# an unplug/replug can be exercised with no hardware. This is the failure mode
# that used to kill the process at startup, which under Restart=always is a boot
# loop on the one device that must survive being unplugged.
import threading
import wandportal.camera as camera_mod
from wandportal.camera import Camera

class FakeCapture:
    """Minimal stand-in for cv2.VideoCapture, driven by module-level state."""
    def __init__(self, source): self.released = False
    def isOpened(self): return FAKE["openable"]
    def set(self, *a): return True
    def get(self, prop): return 0
    def release(self): self.released = True
    def read(self):
        if not FAKE["readable"]:
            return False, None
        return True, np.zeros((16, 16, 3), np.uint8)

FAKE = {"openable": False, "readable": True, "opens": 0}
def fake_videocapture(source):
    FAKE["opens"] += 1
    return FakeCapture(source)

real_videocapture = camera_mod.cv2.VideoCapture
camera_mod.cv2.VideoCapture = fake_videocapture
try:
    ccfg = cfgmod.load("config.yaml").camera
    ccfg.reopen_delay = 0.05
    ccfg.reopen_max_delay = 0.05
    ccfg.reopen_after_failures = 3
    cam = Camera(ccfg)

    # 1. No camera at all: start() must not raise, and the thread must survive.
    FAKE["openable"] = False
    cam.start()
    time.sleep(0.25)
    assert cam._thread.is_alive(), "capture thread died with no camera"
    assert cam.opened is False, "claimed to be open with no camera"
    assert cam.last_error and "could not open" in cam.last_error, cam.last_error
    assert cam.read() == (0, None), cam.read()
    assert cam.health()["opened"] is False and cam.health()["reopens"] == 0

    # 2. Camera appears: the loop picks it up on its own, no restart.
    FAKE["openable"] = True
    time.sleep(0.3)
    assert cam.opened is True, f"did not recover when the camera appeared ({cam.last_error})"
    seq, frame = cam.read()
    assert frame is not None and seq > 0, (seq, frame)
    assert cam.last_error is None, cam.last_error

    # 3. Unplug: reads start failing, and it reopens rather than wedging.
    before = cam.reopens
    FAKE["readable"] = False
    time.sleep(0.4)
    assert cam.reopens > before, "a dead camera never triggered a reopen"

    # 4. Replug: recovers without a restart.
    FAKE["readable"] = True
    time.sleep(0.4)
    assert cam.opened is True, "did not recover after the camera came back"
    seq2, frame2 = cam.read()
    assert frame2 is not None and seq2 > seq, (seq, seq2)
    print(f"camera resilience ok (reopens={cam.reopens}, opens={FAKE['opens']})")
    cam.stop()
    assert not cam._thread.is_alive(), "capture thread outlived stop()"
finally:
    camera_mod.cv2.VideoCapture = real_videocapture

# --- config safety (plan G11, G12, G13)
# A misconfigured device is found at 11pm by someone holding a wand, not at a
# terminal, so every problem should surface at once and nothing should be able
# to silently stop detection.
from wandportal.config import validate

good = cfgmod.load("config.yaml")
assert validate(good) == [], validate(good)

bad = cfgmod.load("config.yaml")
bad.spells = ["lumos", "wingardium_leviosaa", "not_a_spell"]
bad.tracker.threshold = 300
bad.tracker.min_area = 900.0          # above max_area: matches nothing
bad.recognizer.min_confidence = 1.5
bad.camera.rotate = 45
bad.server.port = 0
problems = validate(bad)
joined = " | ".join(problems)
assert len(problems) >= 6, problems          # every problem at once, not the first
assert "wingardium_leviosaa" in joined and "not_a_spell" in joined, joined
assert "threshold" in joined and "min_area" in joined, joined
assert "min_confidence" in joined and "rotate" in joined and "port" in joined, joined
assert "Traceback" not in joined
empty = cfgmod.load("config.yaml"); empty.spells = []
assert any("empty" in p for p in validate(empty)), validate(empty)
print(f"config validation ok ({len(problems)} problems reported at once)")

# templates backups: a ring, not unbounded growth
bdir = tempfile.mkdtemp()
bcfg = cfgmod.load("config.yaml").recognizer
bcfg.templates_path = os.path.join(bdir, "templates.json")
bcfg.template_backups = 3
brec = Recognizer(bcfg)
for i in range(10):
    brec.add_sample("lumos", line(0, 0, 50 + i, 30))
backups = sorted(p for p in os.listdir(bdir) if p.endswith(".bak.json"))
assert len(backups) == 3, f"kept {len(backups)} backups, expected 3: {backups}"
assert len(set(backups)) == 3, f"backup names collided in a burst: {backups}"
assert os.path.isfile(bcfg.templates_path), "live templates file missing"
restored = json.loads(open(os.path.join(bdir, backups[-1])).read())
assert "samples" in restored and restored["samples"]["lumos"], "backup is not a usable templates file"
print(f"template backups ok (ring of {len(backups)}, newest restorable)")
