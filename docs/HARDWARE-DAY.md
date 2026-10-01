# Hardware day

The checklist for the session SPEC.md §11 calls the one everything else waits
on: *"Buy hardware; measure ambient headroom in the actual room, at the actual
times of day, with and without the acrylic. Everything else is guesswork until
this number exists."*

Do not cut the enclosure before finishing this.

---

## Before you start

- [ ] Camera, illuminators and Pi assembled on a board. **No enclosure.**
- [ ] Wand tipped with retroreflective tape.
- [ ] The acrylic panel to hand, but not mounted — you need readings both ways.
- [ ] `mqtt.host` pointing at your broker, so `wand/health` is recorded rather
      than just watched.
- [ ] A tape measure, and something to mark the prop position with.

Start it and open the console. Everything below is readable from a phone.

---

## 1. Aim it

The framing aid exists because a concealed camera cannot be aimed by eye.

- [ ] Stand where people will cast from (1.5–3.5 m).
- [ ] Watch the **aim** pip: it should read `in zone · good`.
- [ ] `too close` / `too far` means move the prop, not the person.
- [ ] HARDWARE.md: aim so the casting zone sits mid-frame *vertically* — angled
      up catches light fittings, angled down catches sunlit floor.
- [ ] **Mark the position.** You will knock it.

## 2. Lock the camera

- [ ] Confirm `camera.controls` in `/api/status` lists the controls that applied.
- [ ] Anything in `controls_failed` is a control this driver names differently —
      note the names from `v4l2-ctl --list-ctrls` and set them in `v4l2_extra`.
- [ ] Walk through frame in a white shirt. Mean brightness should barely move.
      If it hunts, exposure did not lock and nothing below is trustworthy.

## 3. The numbers — this is the point of the day

Record each row from `wand/health` or the Tune panel. **Three times of day,
twice each: with and without the acrylic.**

| When | Acrylic | `ambient` | `threshold` | `headroom` | `blobs_rejected_oversize` | Clean band (Measure cutoff) |
|---|---|---|---|---|---|---|
| 09:00 | no | | | | | |
| 09:00 | yes | | | | | |
| 15:00 | no | | | | | |
| 15:00 | yes | | | | | |
| 21:00 | no | | | | | |
| 21:00 | yes | | | | | |

Do a sunny day **and** an overcast one if you can. 15:00 sunny is the worst case
and the one that decides the design.

### What the numbers mean

- **headroom** — how far the cutoff sits above the room. Small is trouble.
  Zero means the room has caught up and no wand can stand out.
- **clean band width** — how much room for error the threshold has. A band of 10
  will not survive a cloud passing.
- **blobs_rejected_oversize** climbing — a lamp or a window is entering the area
  window as ambient rises.

### The decision this feeds

HARDWARE.md: if the acrylic costs **more than about 30% of headroom**, go
thinner or move to 940 nm illuminators.

```
headroom cost = 1 - (headroom with acrylic / headroom without)
```

- [ ] Cost at 09:00: ______
- [ ] Cost at 15:00: ______  ← the one that matters
- [ ] Cost at 21:00: ______

## 4. Set the threshold

- [ ] **Measure cutoff** in the Tune panel, at the worst time of day you recorded.
- [ ] Take the suggested midpoint; confirm the band is wider than ~20.
- [ ] **Save to config.yaml** — tuning is in memory until you do.

## 5. Decide on adaptive

Only now is this a real decision, because only now do you have the numbers.

- [ ] Does `ambient` move more than ~30 between 09:00 and 15:00?
  - No → leave `threshold_mode: fixed`.
  - Yes → set `adaptive`, with `threshold_margin` from the gap between your
    measured ambient and the clean band's lower edge, and `threshold_floor` at
    the band's lower edge from your darkest reading.
- [ ] Re-run **Measure cutoff** after switching and confirm the band still holds.

## 6. Train and check separation

- [ ] Train 6 spells, five or six passes each, from the casting distance.
- [ ] `python -m wandportal --selftest` over SSH. Exits non-zero on any confusion.
- [ ] Any confusion: redraw one of the pair rather than lowering `min_confidence`.
- [ ] Export the templates (`/api/templates/export`) before changing anything else.

## 7. Then, and only then

- [ ] Design the enclosure around the geometry these numbers imply.
- [ ] Record the final values in `config.yaml` and commit them.
- [ ] Live with it for a week; count false positives; revisit `min_margin`.

---

## Values SPEC.md Phase 2 is waiting on

Everything here is a guess until this table is filled in.

| Setting | Current | Measured | From |
|---|---|---|---|
| `camera.exposure_absolute` | 50 (TODO) | | step 2 |
| `camera.gain` | 20 (TODO) | | step 2 |
| `tracker.threshold` | 220 | | step 4 |
| `tracker.threshold_floor` | 200 | | step 5 |
| `tracker.threshold_margin` | 25 | | step 5 |
| `tracker.max_area` | 500 | | step 3, from oversize counts |
| Illuminator wavelength | 850 nm | | step 3, headroom cost |
