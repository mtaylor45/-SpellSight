# Development Plan

Daily working plan for the stretch before hardware arrives, and through physical
tuning. **This file is the source of truth for what happens next** — a daily
session reads it, executes the first unfinished day, and checks the boxes.

Companion to `SPEC.md` (what is built and what isn't) and `CLAUDE.md` (the rules
that constrain how). Where this plan and SPEC.md disagree, SPEC.md wins.

## Status legend

- `[ ]` not started · `[~]` in progress · `[x]` done (PR merged)
- **HW** — cannot be finished without hardware; the code path can still be built
  and tested synthetically, only the constants wait

## Principles for every day

1. **One day, one PR.** Small enough to review in a sitting.
2. **Both suites pass** with no camera and no broker before anything is pushed:
   `python smoke_test.py && python api_test.py`.
3. **The frozen interface in SPEC.md §4 does not move.** Adding topics under
   `wand/` is fine; repurposing is not.
4. **Recognition behaviour does not change** unless the day says it does. New
   modes ship switched off.
5. **Nothing new blocks the capture loop.**
6. If a day turns out to be wrong, say so in the PR and amend this file rather
   than quietly doing something else.

---

## The gaps this plan closes

Found by auditing the merged code against its own documents. Each is referenced
by id in the day that closes it.

| id | Gap | Evidence |
|---|---|---|
| G1 | **Startup crashes with no camera.** `Camera.open()` raises, `Engine.start()` does not catch it. Under `restart: unless-stopped` / `Restart=always` this is a boot loop. | Verified: `python -m wandportal` exits 1 with `RuntimeError: Could not open camera source 0` |
| G2 | **`wand/health` does not exist** though CLAUDE.md's "Fail loudly" rule names it as required. | No match for `health` in `wandportal/` |
| G3 | **`threshold_mode` does not exist** in code or config, though CLAUDE.md mandates keeping `fixed` working. | Appears only in `SPEC.md` and `CLAUDE.md` |
| G4 | **Camera degradation is invisible.** `/api/status` exposes only `camera_fps` — no open state, last error, or reopen count. | `engine.status()` |
| G5 | **Tuning is memory-only.** Hours of threshold work vanish on restart. | `/api/tune` mutates `cfg` and returns |
| G6 | **`templates_path` is absolute** (`/data/templates.json`). Breaks the README's bare-metal Pi path and the systemd unit, where `ProtectSystem=strict` + `ReadWritePaths=/opt/wand-portal/data` forbid writing `/data`. Redundant under Docker, which already sets the env var. | `config.yaml`, `Dockerfile`, `systemd/wand-portal.service` |
| G7 | **README's Docker snippet is wrong.** `cp config.yaml config.local.yaml`, but compose mounts `./config.yaml`. | `README.md`, `docker-compose.yml` |
| G8 | **No CI.** Nothing runs the regression suites automatically. | no `.github/` |
| G9 | **The frozen MQTT interface has no test.** A topic or payload key can be renamed without anything failing. | `smoke_test.py` |
| G10 | **No `sd_notify`,** so `WatchdogSec` cannot be restored to the systemd unit. | `main.py` |
| G11 | **`/api/tune` values are unbounded.** `threshold: -5` or `9999` is accepted and silently kills detection. | `TuneRequest` has no `Field` bounds |
| G12 | **No template backup.** A bad training run or a stray `clear` overwrites the only copy of an evening's work. | `Recognizer.save()` |
| G13 | **A typo'd spell id in `config.yaml` crashes with a raw traceback** at startup rather than a readable message. | `resolve()` raises inside `Engine.__init__` |
| G14 | **The console is unauthenticated** and the MJPEG stream is always on — a camera in a living room, open on the LAN. | `server.py`, spec R3.3 |

## Opportunities identified

Beyond the SPEC phases. Scheduled ones name their day; the rest are backlog.

| id | Idea | Why it earns its place |
|---|---|---|
| O1 | **Threshold sweep assistant** (day 7) | Physical tuning is currently "drag a slider and squint". A sweep that reports blob count per threshold makes step 1 of SPEC §11 empirical |
| O2 | **Rejection ring buffer** (day 14) | On a concealed device, "why didn't it fire?" is otherwise unanswerable. Keep the last N frames and the trace behind a rejected cast |
| O3 | **Templates export/import over HTTP** (day 14) | The README promises templates are copyable between installs; there is no endpoint for it |
| O4 | **`--selftest` CLI flag** (day 14) | Run separation and exit, over SSH on the Pi, without opening the console |
| O5 | Per-spell confidence/margin overrides | Some gestures separate far better than others; one global threshold is the weakest link |
| O6 | `wand/cmd` command topic | Let Home Assistant trigger a discovery republish or template reload |
| O7 | Cast history as an HA sensor | Automations that need debouncing or "second cast within 5s" have nothing to read |
| O8 | Persisted config validation report | Surface every config problem at once at startup, not the first one as a traceback |

---

## Days

### Day 1 — Guardrails `[~]` — [PR #4](https://github.com/mtaylor45/-SpellSight/pull/4)
**Goal.** Make it impossible to break the frozen interface quietly, and clear the two known bugs.
**Closes.** G6, G7, G8, G9
**Changes.** GitHub Actions workflow running both suites on push and PR (Python 3.11, installs `requirements.txt` + `requirements-dev.txt`). MQTT contract tests against a stub paho client, asserting every topic shape, retain flag, payload key and entity `object_id` in SPEC.md §4. `templates_path` back to the relative default. README Docker snippet corrected to match compose.
**Acceptance.** CI green on the PR. Renaming any topic or payload key fails a test. `python -m wandportal` writes templates under `./data` on a bare checkout; Docker still uses `/data` via the env var.
**Depends on.** nothing

### Day 2 — Camera resilience and honest status `[ ]`
**Goal.** Survive a missing or re-enumerating camera, and say so out loud.
**Closes.** G1, G4
**Changes.** `Engine.start()` no longer dies when the camera is absent; the capture thread retries with backoff and the app reaches a serving state. Track `opened`, `last_error`, `reopens`. Surface them in `/api/status` and the console header.
**Acceptance.** `python -m wandportal` with no camera reaches "watching for spells", serves the console, and reports `opened: false` with a reason. Unplug/replug (simulated by a stub source) increments `reopens` and recovers without a restart. Docker `restart: unless-stopped` no longer boot-loops on a host with no `/dev/video0`.
**Depends on.** day 1

### Day 3 — Config safety `[ ]`
**Goal.** Stop a phone slider or a typo from bricking recognition.
**Closes.** G11, G12, G13
**Changes.** Bounds on every `TuneRequest` field, rejecting out-of-range with 422 and a readable message. Startup config validation that reports every problem at once (unknown spell ids, impossible ranges) and exits with a clear message, not a traceback. Rotate `templates.json` to a small ring of timestamped backups on each save.
**Acceptance.** `threshold: -5` and `threshold: 9999` are refused and recognition is unaffected. A typo'd spell id names the offender and the valid ids without a stack trace. Ten saves leave the newest N backups and no unbounded growth.
**Depends on.** day 1

### Day 4 — Ambient estimation and the `threshold_mode` seam `[ ]`
**Goal.** Measure ambient without changing a single recognition decision.
**Closes.** G3 · **Starts.** R2.2
**Changes.** `threshold_mode: fixed | adaptive` in `TrackerConfig`, defaulting to `fixed`. Ambient estimator — configurable percentile of a downscaled grayscale frame, recomputed every N frames, excluding the tracked blob's neighbourhood — behind an interface a differencing source (R2.4) can also satisfy. Ambient, working threshold and headroom in `/api/status` and the Tune panel.
**Acceptance.** With `fixed`, behaviour is identical and both suites pass unmodified. A stationary bright blob does not inflate the ambient estimate. Percentile cost is measured on a downscaled frame and reported in the PR.
**Depends on.** day 2

### Day 5 — `wand/health` and the headroom sensor `[ ]` **← the measuring instrument**
**Goal.** Give the hardware session something to read.
**Closes.** G2 · **Implements.** R2.3
**Changes.** Retained `wand/health` at most every 30 s: `ambient`, `threshold`, `headroom`, `fps`, `blobs_rejected_oversize`, `camera_reopens`, `uptime_s`. Discovery-published `sensor.wand_optical_headroom`. Publishing on its own thread.
**Acceptance.** Headroom drops measurably when a bright source enters view (synthetic frame proves the arithmetic). Rate limited to one publish per 30 s regardless of frame rate. `camera_reopens` increments on a simulated reset. A new topic under `wand/` — §4 untouched.
**Depends on.** days 2, 4

### Day 6 — Persist tuning `[ ]`
**Goal.** Stop losing an evening's tuning to a restart — before the tuning starts.
**Closes.** G5 · **Implements.** R3.4
**Changes.** `POST /api/config/save` writing tracker and recognizer values back to the YAML **preserving comments and key order** (targeted line rewrite; `ruamel.yaml` only if it earns the dependency under CLAUDE.md). Unsaved-changes indicator in the Tune panel. 409, not 500, on a read-only mount — which is exactly how compose mounts it.
**Acceptance.** Saving preserves every comment. Env overrides still win after a restart. A read-only config returns 409 with a usable message.
**Depends on.** day 3

### Day 7 — Threshold sweep assistant `[ ]`
**Goal.** Make choosing a threshold a measurement, not a guess.
**Implements.** O1
**Changes.** An endpoint that sweeps the threshold across a range on the current frame and returns blob count and largest blob area per step; console renders it as a curve with the "only the wand is visible" band marked.
**Acceptance.** On a synthetic frame with one bright dot and one large bright patch, the sweep identifies the band where exactly one blob survives. Runs off the capture loop and never blocks it.
**Depends on.** days 4, 6

### Day 8 — Adaptive threshold `[ ]` **HW** (constants only)
**Goal.** Finish R2.2 with the policy switched off until measured.
**Implements.** R2.2
**Changes.** `adaptive` mode: `clamp(ambient + margin, floor, 254)`. `fixed` stays the default. Config gains `threshold_floor`, `threshold_margin`, `ambient_percentile`, `ambient_interval` with SPEC's starting values.
**Acceptance.** A simulated ambient step from 40 to 180 tracks within 3 recalculation intervals. `fixed` reproduces day-1 behaviour exactly and the suites pass unmodified. Real constants deferred to the tuning session.
**Depends on.** day 4

### Day 9 — Console access control `[ ]`
**Goal.** Take the living-room camera off the open LAN.
**Closes.** G14 · **Implements.** R3.3
**Changes.** `auth_token` accepted via `Authorization: Bearer` or `?t=` (an `<img src>` cannot set headers). `stream_mode: always | on_demand | off`. A loud startup warning when the token is empty and bind is not loopback. Console prompts once and keeps the token in `localStorage`.
**Acceptance.** With a token set, the stream and every `/api/*` route return 401 without it. `off` returns 404 and the console explains rather than showing a broken image. Training and tuning still work from thumbnails alone with the stream off.
**Depends on.** day 1

### Day 10 — Exposure lock code path `[ ]` **HW** (values only)
**Goal.** Build R2.1's mechanism now; fill in the numbers when the camera exists.
**Implements.** R2.1 (partial)
**Changes.** Drive `v4l2-ctl` directly, verify by reading controls back, apply on open **and** on every reopen (a USB re-enumeration silently resets controls). `lock_exposure`, `exposure_absolute`, `gain`, `auto_white_balance`, `v4l2_extra` in config. `v4l-utils` is already in the Dockerfile.
**Acceptance.** Applied controls are logged at INFO; a control that fails logs a WARNING naming it and does not raise. Absent `v4l2-ctl` logs once at INFO and continues — which is this dev environment, so it is testable today. Values marked TODO until the camera is chosen.
**Depends on.** day 2

### Day 11 — Framing aid `[ ]`
**Goal.** Make a concealed camera aimable by one person with a phone.
**Implements.** R4.2
**Changes.** Console overlay marking the casting zone, plus a live "wand in frame" readout and a distance-plausibility hint from blob area.
**Acceptance.** The overlay renders in both cast and tune views. The readout tracks a synthetic dot entering and leaving the zone.
**Depends on.** day 5

### Day 12 — Boot and power resilience `[ ]`
**Goal.** Earn the watchdog back and prove the templates survive a power cut.
**Closes.** G10 · **Implements.** R3.2
**Changes.** `sd_notify` READY and WATCHDOG pings (no dependency — a few lines over a unix socket, no-op when `NOTIFY_SOCKET` is unset). Restore `WatchdogSec` to the unit. Fault-injection test killing the process mid-save.
**Acceptance.** Runs unchanged outside systemd. `kill -9` mid-training loses at most the in-flight sample and always leaves valid JSON. The unit no longer restart-loops with the watchdog enabled.
**Depends on.** day 3

### Day 13 — Feedback scaffolding `[ ]` **HW** (verification only)
**Goal.** Everything in R3.1 that does not need an LED soldered on.
**Implements.** R3.1 (partial)
**Changes.** `feedback.py` with `none` backends as the working default, lazy hardware imports, a backend that raises being disabled after its first failure, an animation thread that cannot block capture, quiet hours honoured across midnight, and `color` on `Spell` with canon-appropriate values.
**Acceptance.** `leds: none, sound: none` gives byte-identical recognition and no new runtime imports. A missing `rpi_ws281x` logs one INFO line and continues. A backend raising on every call is disabled after the first. Brightness and latency claims wait for hardware.
**Depends on.** day 3

### Day 14 — Diagnostics, portability, and hardware-day readiness `[ ]`
**Goal.** Answer "why didn't it fire?" and be ready for the camera to arrive.
**Implements.** O2, O3, O4
**Changes.** Ring buffer of the last N frames plus the trace behind a rejected cast, exposed for review in the console. Templates export/import over HTTP. `--selftest` flag running separation and exiting. A `docs/HARDWARE-DAY.md` checklist: what to measure at 9am/3pm/9pm, with and without the acrylic, and which numbers to write down.
**Acceptance.** A rejected cast is reviewable after the fact without a camera attached. Exported templates import into a clean install and classify identically. The checklist names every value SPEC.md Phase 2 needs.
**Depends on.** days 5, 7

---

## After the hardware arrives

The plan stops here on purpose. SPEC.md §11 is explicit that the ambient
headroom measurement comes before the rest, and that the enclosure waits on it:
re-tuning against real optics will reorder whatever is left. Day 5 and day 14
exist to make that session productive.

Backlog, unscheduled: O5, O6, O7, O8, R2.4 (pulsed differencing), and Phase 4
beyond the framing aid.
