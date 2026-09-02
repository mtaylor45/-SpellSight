# Wand Portal

Infrared wand gesture recognition on a Raspberry Pi. Recognized spells publish
over MQTT as Home Assistant entities. The device lives concealed inside a prop in
a living room — no screen, no keyboard, unattended.

**Read `SPEC.md` before starting work.** It defines what is built (Phase 1),
what isn't (Phases 2–4), and the interface contract that must not break.

## Commands

```bash
python -m wandportal -c config.yaml     # run
python -m wandportal --list-spells      # print the 45-spell catalog
python -m wandportal --no-server -v     # headless, debug logging

python smoke_test.py                    # tracker + recognizer, no hardware
python api_test.py                      # full HTTP API against a fake camera

docker compose up -d --build
```

Both test files must pass with no camera and no broker attached. They are the
regression suite — run them after any change to `tracker.py`, `recognizer.py`,
`engine.py`, or `camera.py`.

## Architecture

```
camera.py        Threaded capture. Always returns the newest frame, never a queued one.
tracker.py       Threshold → contours → centroid, plus gesture start/end segmentation.
recognizer.py    Resample to 64 pts, normalize, cosine match, JSON persistence.
mqtt_bridge.py   MQTT client + Home Assistant discovery.
engine.py        The loop: capture → track → classify → publish.
server.py        FastAPI. MJPEG stream, training and tuning API.
spells.py        Spell catalog. Ids here become MQTT topics and HA entity ids.
web/index.html   Mobile training console. Single file, no build step, no framework.
```

## Rules

**The capture loop must never block.** Anything slow — feedback, health
publishing, config writes, sound — goes on its own thread. A frame missed
mid-gesture is a failed cast.

**Fail loudly.** This device is concealed and unattended, so silent degradation
is the worst possible failure. Anything that degrades recognition must show up in
`/api/status` and in the `wand/health` MQTT topic.

**Hardware imports are optional.** GPIO, NeoPixel, and audio backends must import
lazily and fall back to a working no-op. The project has to keep running on a dev
laptop and in Docker with no hardware attached.

**Don't change the MQTT contract.** Topic shapes, entity `object_id`s, and
payload keys in SPEC.md section 4 are frozen — Home Assistant automations depend
on them. Adding new topics under `wand/` is fine. Repurposing existing ones is not.

**Rotation invariance stays off.** In `recognizer.py`, an up-stroke and a
down-stroke must remain different spells. This is deliberate, not an oversight.

**Keep `threshold_mode: fixed` working.** It's the fallback when adaptive
thresholding misbehaves and the baseline the existing tests assert against.

## Dependencies

Standard library plus `requirements.txt`. New runtime dependencies need a
justification. Nothing that needs a build step for the web console — it is one
hand-written HTML file on purpose, because it has to be editable over SSH on a Pi
with no toolchain.

## Style

Type hints on public functions. Docstrings that say why, not what. Comments only
where the reasoning isn't obvious from the code — most of the existing ones mark
a non-obvious tradeoff, and those should survive refactors.
