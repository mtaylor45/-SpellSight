"""Synthetic end-to-end check: no camera, no broker."""
import math, random, tempfile, os, sys
import numpy as np

from wandportal import config as cfgmod
from wandportal.recognizer import Recognizer, normalize, resample
from wandportal.tracker import BlobTracker, render_trace, path_length
from wandportal.spells import resolve, CATALOG

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
