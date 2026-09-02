# Wand Portal — Design & Specification

**Handoff document for implementation.**
Phase 1 is written, tested, and included in this repo. Phases 2–4 are specified
here and not yet built.

---

## 1. What this is

A concealed infrared camera watches the living room. A wand tipped with
retroreflective tape traces a shape in the air. The shape is recognized as a
spell, and the spell is published over MQTT as a Home Assistant entity.

The device lives in the open, inside a prop that reads as an object from the
Harry Potter universe. It has no screen, no keyboard, and no visible camera. It
must survive being unplugged, must not need a laptop to recover, and must work
for a guest who has never seen it before.

Everything downstream — what Lumos actually does — lives in Home Assistant
automations, not here. This device's only job is to turn a gesture into a
trustworthy event.

### Goals

1. A trained spell is recognized on the first attempt at least 9 times in 10,
   at a casting distance of 1.5–3.5 m, in a room with the lamps on.
2. No false cast in 24 hours of ordinary living-room activity — people walking
   through, a TV on, pets, a fire lit.
3. Recovery from a power cut with no human action, in under 60 seconds.
4. A person casting a spell knows within 300 ms whether it worked, without
   looking at a phone.
5. Adding a new spell takes under two minutes and no code change.

### Non-goals

- **Wand identity.** Every wand looks identical to the camera. Spells are not
  per-person. Multi-wand disambiguation is a different project.
- **Local show control.** No servos, no fog, no scene logic on the Pi. Home
  Assistant owns all of that. The reference project put show control on-device;
  we deliberately don't.
- **Cloud anything.** No remote access, no external API, no telemetry.
- **Voice.** No microphone. Adding one to a concealed living-room device is a
  materially different privacy proposition and is out of scope.
- **Recognizing untrained gestures.** There is no generalization. If it wasn't
  trained, it is rejected.

---

## 2. Current state — Phase 1 (complete)

Working, with two test suites that run without a camera or a broker.

```
wandportal/
  camera.py        Threaded capture, always hands back the newest frame
  tracker.py       Threshold → contour → centroid, plus gesture segmentation
  recognizer.py    Resample, normalize, cosine match, JSON persistence
  mqtt_bridge.py   MQTT client with Home Assistant discovery
  engine.py        The capture → track → classify → publish loop
  server.py        FastAPI: MJPEG stream, training and tuning API
  spells.py        45-spell catalog
  web/index.html   Mobile training console
```

**Recognition approach.** Each cast is resampled to 64 evenly spaced points,
uniformly scaled, centered, flattened to a 128-dimensional unit vector, and
compared against stored samples by cosine similarity. It is a $1 Recognizer
variant with rotation normalization deliberately disabled, because for wand
casting an up-stroke and a down-stroke should be different spells.

A cast publishes only if the best match clears `min_confidence` **and** beats
the runner-up by `min_margin`. The margin test is what stops a sloppy wave from
picking arbitrarily between two similar spells.

**Verified.** 40/40 on jittered synthetic casts across four gesture shapes;
random noise correctly rejected at 0.084 confidence; tracker segmentation and
the full HTTP API exercised against a synthetic camera.

**Not verified.** Anything involving real optics, real ambient light, or real
hardware. That is what Phase 2 exists to fix.

---

## 3. The problem Phase 1 doesn't solve

Phase 1 was designed for a dark basement, where the retroreflector is
trivially the brightest thing in frame. A living room breaks three assumptions.

**Ambient infrared.** Daylight, incandescent and halogen lamps, a fireplace, and
a plasma-adjacent TV all emit strongly in the near-IR. At 3pm on a sunny day a
sunlit patch of carpet will be brighter than the wand tip. A fixed grayscale
threshold cannot work across a day/night cycle.

**Auto-exposure.** Consumer USB cameras hunt continuously. When someone in a
white shirt walks through frame, the camera stops down, the wand tip dims, and
the threshold stops matching. Auto-exposure must be disabled outright — this is
the single most common cause of "it worked yesterday."

**No feedback path.** The only way to know a cast registered is the web console.
That is fine for a device on a workbench and useless for a prop on a shelf.

There is a convenient convergence here: **the material that conceals the camera
is the same material that solves the ambient problem.** An IR-pass acrylic panel
(opaque near-black to the eye, >85% transmission above 800nm) hides the camera
and illuminators completely while rejecting most of the visible and short-wave
ambient that would otherwise swamp the sensor. Concealment and filtering are one
component, not two.

---

## 4. Frozen interface

Home Assistant automations will be built against this contract. **Do not change
topic shapes, entity object_ids, or payload keys without an explicit migration
note.** Breaking these silently breaks a user's house.

| Topic | Payload | Retained |
|---|---|---|
| `wand/status` | `online` / `offline` (LWT) | yes |
| `wand/spell/<id>/state` | `ON`, then `OFF` after `pulse_seconds` | no |
| `wand/last_spell` | Display name | yes |
| `wand/last_spell/attributes` | `{spell_id, confidence, duration, effect, cast_at}` | yes |
| `wand/event` | `{spell, name, confidence}` | no |

Entities: `binary_sensor.wand_<id>` per enabled spell, plus
`sensor.wand_last_spell`. All grouped under one device (`identifiers: [wand]`)
so the integration removes cleanly.

Phase 2+ may **add** topics under `wand/`. It may not repurpose these.

---

## 5. Phase 2 — Living-room optics (P0)

Without this phase the device does not work in the target environment. This is
the phase to build first and the one to spend real time on.

### R2.1 — Lock camera exposure, gain, and white balance

Auto-exposure must be off and stay off. OpenCV's property setters are
unreliable across UVC drivers, so drive `v4l2-ctl` directly and verify by
reading the control back.

Add to `CameraConfig`:

```yaml
camera:
  lock_exposure: true
  exposure_absolute: 50      # driver units; tuned per camera
  gain: 20
  auto_white_balance: false
  v4l2_extra: {}             # escape hatch: raw control=value pairs
```

Implement in `camera.py`, applied on open **and** on every reopen after a read
failure — a USB re-enumeration resets controls to defaults, which is a silent
failure mode that looks like drift.

Acceptance:
- [ ] `v4l2-ctl --get-ctrl=exposure_auto` reports manual mode after startup
- [ ] Applied controls are read back and logged at INFO; a control that fails to
      apply logs a WARNING naming the control, and does not raise
- [ ] After a forced USB reset, controls are reapplied within one reopen cycle
- [ ] With exposure locked, mean frame brightness varies by under 5% when a
      person in a white shirt crosses the frame
- [ ] Degrades cleanly where `v4l2-ctl` is absent (dev laptop, macOS): logs once
      at INFO, continues

### R2.2 — Ambient-adaptive threshold

Replace the fixed `tracker.threshold` with a floor plus a margin above measured
ambient.

```yaml
tracker:
  threshold_mode: adaptive   # "fixed" keeps Phase 1 behavior
  threshold_floor: 200       # never go below this
  threshold_margin: 25       # required brightness above ambient
  ambient_percentile: 99.0
  ambient_interval: 15       # frames between recalculations
```

Every `ambient_interval` frames, compute the `ambient_percentile` of a
downscaled grayscale frame and set the working threshold to
`clamp(ambient + margin, floor, 254)`. Downscale before the percentile — full-res
`np.percentile` on every frame is not free on a Pi.

Exclude the currently tracked blob's neighborhood from the ambient calculation,
or a wand held still will inflate ambient and threshold itself out of existence.

Acceptance:
- [ ] Working threshold is exposed in `/api/status` and shown in the console's
      Tune panel alongside the measured ambient
- [ ] Simulated ambient step from 40 to 180 causes the threshold to track within
      3 recalculation intervals
- [ ] A stationary bright blob does not raise the ambient estimate
- [ ] `threshold_mode: fixed` reproduces Phase 1 behavior exactly, and the
      existing `smoke_test.py` still passes unmodified

### R2.3 — Optical health signal

The device is concealed and unattended, so silent degradation is the real
enemy. Publish enough to alert on in Home Assistant.

New retained topic `wand/health`, at most once every 30 s:

```json
{"ambient": 62, "threshold": 210, "headroom": 148, "fps": 29.4,
 "blobs_rejected_oversize": 0, "camera_reopens": 0, "uptime_s": 84210}
```

Plus a discovery-published `sensor.wand_optical_headroom`.

Acceptance:
- [ ] Headroom drops measurably when a lamp is switched on in view
- [ ] `camera_reopens` increments on a forced USB reset
- [ ] Rate limited to one publish per 30 s regardless of frame rate

### R2.4 — Pulsed illumination differencing (P2, stretch)

The complete answer to ambient IR: drive the illuminators from a GPIO, capture
alternate lit and unlit frames, and subtract. Everything that isn't retroreflective
cancels, including sunlight.

Not for v1. USB cameras give no frame-exposure sync signal, so pairing frames to
LED state means either accepting a slower effective rate or moving to a CSI
camera with exposure control. **Do design R2.1–R2.3 so this can be added as an
alternative frame source rather than a rewrite** — keep ambient estimation and
thresholding behind an interface that a differencing source can also satisfy.

---

## 6. Phase 3 — Standalone appliance (P0/P1)

### R3.1 — Local feedback (P0)

Nobody should need a phone to know whether a spell landed.

New `wandportal/feedback.py`, with all backends optional and a working no-op
default so Docker and dev machines are unaffected.

| Event | LED | Sound |
|---|---|---|
| Boot, MQTT connected | slow amber breathe | — |
| MQTT unreachable | slow red breathe | — |
| Wand enters frame | steady amber, brighter | — |
| Cast accepted | flash in the spell's colour, 400 ms | per-spell wav if present, else a shared chime |
| Cast rejected | single dim red pulse, 200 ms | — |
| Training sample recorded | two amber blinks | click |

Add `color: str = "#ffb648"` to the `Spell` dataclass and set canon-appropriate
values on the obvious ones — Lumos warm white, Nox deep blue, Incendio orange,
Aguamenti blue, Avada Kedavra green, Expecto Patronum pale silver-blue.

```yaml
feedback:
  leds: none          # none | neopixel | gpio
  led_pin: 18
  led_count: 16
  led_brightness: 0.4
  sound: none         # none | aplay | pygame
  sound_dir: /data/sounds
  quiet_hours: ["22:30", "07:00"]   # LEDs dim, sound muted
```

Run the LED animation on its own thread at ~50 Hz. It must never block the
capture loop, and an exception in a feedback backend must be caught, logged
once, and the backend disabled for the rest of the run — a broken LED must not
stop spell recognition.

Acceptance:
- [ ] `leds: none, sound: none` produces byte-identical recognition behavior and
      no new imports at runtime
- [ ] Missing `rpi_ws281x` / `gpiozero` logs one INFO line and continues
- [ ] Feedback latency from gesture end to LED change is under 100 ms
- [ ] Quiet hours suppress sound and cap brightness, and are honored across midnight
- [ ] A backend raising on every call is disabled after the first failure

### R3.2 — Boot and power resilience (P0)

Living-room devices get unplugged. Assume it, don't defend against it.

- `systemd/wand-portal.service`: `Restart=always`, `RestartSec=5`,
  `After=network-online.target`, hardware watchdog enabled
- Start and reach "watching for spells" without a network — MQTT retries in the
  background, recognition works meanwhile, casts during the outage are dropped
  and counted, not queued
- Template writes stay atomic (Phase 1 already writes to `.tmp` and renames);
  verify this survives a power cut mid-save with a fault-injection test
- Document the `/data` overlay-filesystem option in `docs/HARDWARE.md` for SD
  card longevity, but do not require it

Acceptance:
- [ ] Cold boot to first recognized cast in under 60 s on a Pi 4
- [ ] Boot with the network cable unplugged reaches a working recognition state
- [ ] `kill -9` mid-training loses at most the in-flight sample; the templates
      file is always valid JSON
- [ ] Service survives 20 consecutive hard power cuts with templates intact

### R3.3 — Console access control (P1)

A camera in a living room streaming unauthenticated MJPEG on the LAN is a
mistake worth designing out.

```yaml
server:
  bind: 0.0.0.0
  auth_token: ""        # empty disables auth; warn loudly at startup
  stream_mode: on_demand   # always | on_demand | off
  setup_window_s: 900      # unauthenticated access allowed this long after boot
```

- Token accepted via `Authorization: Bearer` or a `?t=` query parameter, since
  `<img src>` cannot set headers
- `stream_mode: on_demand` serves frames only while the console is open, and
  suspends within 5 s of the last client disconnecting
- Startup logs a WARNING naming the risk when `auth_token` is empty and bind is
  not loopback

Acceptance:
- [ ] With a token set, `/stream.mjpg` and every `/api/*` route return 401 without it
- [ ] The console prompts for a token once and stores it in `localStorage`
- [ ] `stream_mode: off` returns 404 on the stream and the console shows a clear
      explanation rather than a broken image
- [ ] Tuning and training still work with the stream off, using the trace
      thumbnails alone

### R3.4 — Persist tuned values (P1)

Tuning currently lives only in memory and is lost on restart, which is a bad
surprise after an hour of threshold work.

- `POST /api/config/save` writes current tracker and recognizer values back to
  the YAML, preserving comments (`ruamel.yaml`, or a targeted line rewrite —
  do not round-trip through `yaml.safe_dump` and destroy the file's comments)
- The console shows an unsaved-changes indicator in Tune mode

Acceptance:
- [ ] Saving preserves all comments and key order in `config.yaml`
- [ ] Env-var overrides still win over saved file values after a restart
- [ ] Saving with a read-only config mount returns a clear 409, not a 500

---

## 7. Phase 4 — Enclosure integration (P1/P2)

### R4.1 — Illuminator control (P1)
Drive the IR LEDs from a GPIO through a MOSFET rather than leaving them always
on. Off when idle saves heat and extends LED life; also a prerequisite for R2.4.
Config: `illuminator.pin`, `illuminator.mode: always | on_motion | pulsed`.

### R4.2 — Framing aid (P1)
A concealed camera cannot be aimed by eye. Add a console overlay showing the
casting zone and a live "is the wand in frame" readout, so aiming is a one-person
job with a phone in hand.

### R4.3 — Thermal supervision (P2)
Read the SoC temperature into `wand/health`. A Pi inside a sealed prop next to
IR LEDs will throttle, and throttling shows up as dropped frames and mystery
recognition failures.

---

## 8. Hardware

See `docs/HARDWARE.md` for the full bill of materials, optical geometry, and
enclosure notes. Summary of what the software must assume:

- Raspberry Pi 4B (2 GB is enough) or Pi 5, passive cooling with venting
- USB UVC camera, IR-cut filter removed, 100–120° lens
- 850 nm illuminators, mounted as close to the lens axis as physically possible
  — retroreflection is directional, and off-axis LEDs waste most of their output
- IR-pass acrylic front panel serving as both concealment and optical filter
- WS2812B ring, 12 or 16 pixels, behind a diffuser
- Casting zone: 1.5–3.5 m from the prop, prop mounted 1.2–1.8 m high

---

## 9. Conventions for implementation

- **Python 3.11+.** Standard library plus what's already in `requirements.txt`.
  New runtime dependencies need a justification in the PR description; anything
  hardware-specific must be an optional import with a working fallback.
- **The capture loop is sacred.** Nothing added to `engine._loop` may block.
  Feedback, health publishing, and config writes all belong on their own threads.
- **Fail visible, not silent.** A degraded optical path must surface in
  `/api/status` and `wand/health`. Silent degradation in a concealed device is
  the worst outcome in this project.
- **Both test files must keep passing** without a camera or broker:
  `python smoke_test.py && python api_test.py`. New features that touch tracking
  or recognition need a synthetic test in the same style.
- **Keep `threshold_mode: fixed` working.** It is the fallback when adaptive
  logic misbehaves, and the reference behavior for the existing tests.
- **Don't touch the frozen interface** in section 4 without saying so explicitly.

---

## 10. Open questions

Blocking:

1. **Which camera module?** R2.1's exposure values are driver-specific and can't
   be finalized until hardware is chosen. Buy first, tune second.
2. **Does the chosen IR-pass acrylic pass enough at 850 nm?** Measure headroom
   with and without the panel before committing to the enclosure. If it costs
   more than about 30% of headroom, switch to 940 nm illuminators or a thinner panel.

Non-blocking:

3. Is 16 NeoPixels behind the prop's diffuser bright enough to read across a lit
   room, or does the feedback need to be audio-first?
4. Should quiet hours come from Home Assistant rather than a local config, so it
   follows an existing house-wide "night" state? Leaning yes, but it adds an MQTT
   subscribe path that doesn't otherwise exist.
5. How many spells actually survive the separation check with real casts? The
   estimate of 6–10 is from the shape of the algorithm, not from measurement.

---

## 11. Suggested order

1. Buy hardware; measure ambient headroom in the actual room, at the actual
   times of day, with and without the acrylic. **Everything else is guesswork
   until this number exists.**
2. R2.1, then R2.2, then R2.3. Retune and retrain against real optics.
3. R3.2 and R3.1 — make it an appliance before making it pretty.
4. Live with it for a week. Count false positives. Revisit `min_margin`.
5. R3.3, R3.4, then Phase 4.

Do not build the enclosure until step 2 is done. The optical geometry will
change once real numbers exist, and a finished prop is expensive to re-cut.
