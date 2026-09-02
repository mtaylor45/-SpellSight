# Hardware & Enclosure

Companion to `SPEC.md`. Everything here is a starting point to measure against,
not a finished design — see the note at the end about buying before building.

---

## Bill of materials

| Part | Notes |
|---|---|
| Raspberry Pi 4B 2 GB, or Pi 5 4 GB | Phase 1 uses ~15% of one Pi 4 core at 640×480/30fps. Pi 5 only if you want headroom for pulsed differencing later. |
| USB UVC camera, IR-cut filter removed | The "night vision" OV2710 / ELP modules sold for security use. Must expose `exposure_auto` and `exposure_absolute` through V4L2 — check before buying. |
| Wide lens, 100–120° | The casting arc is large and close. A 60° lens will clip the ends of gestures. |
| 850 nm IR illuminators | Stronger retroreflective return than 940 nm. Downside: a faint visible red glow, which matters for a "concealed" device — see below. |
| 3M Scotchlite retroreflective tape (7610 or 3000X) | The whole trick. A few mm on the wand tip. |
| IR-pass acrylic panel | Doubles as concealment and optical filter. See "Optics". |
| WS2812B ring, 12–16 px | Behind a diffuser. GPIO18. |
| MAX98357A I2S amp + 3 W speaker | Or any USB speaker if you'd rather not solder. |
| 5 V 3 A USB-C supply | Undervoltage on a Pi presents as random camera dropouts, which will waste a day of debugging. |
| Passive heatsink + vented base | See "Thermal". |

---

## Optics

This is where the project succeeds or fails. Budget more time here than on code.

### The retroreflection trick

Retroreflective tape returns light almost exactly back along its incoming path.
Light it from the camera's axis and it reads as a blindingly bright dot; light it
from 30° off-axis and most of the return goes somewhere other than your lens.

**Mount the illuminators as close to the lens as physically possible.** A ring
around the lens is ideal. This constraint outranks aesthetics — if the prop
design forces the LEDs 100 mm away from the camera, change the prop design.

### The ambient problem

A living room is full of near-IR: sunlight, incandescent and halogen bulbs, a
fireplace, some TVs. On a sunny afternoon, a sunlit patch of carpet can be
brighter to an IR-sensitive sensor than the wand tip. The Phase 1 fixed threshold
assumes a dark basement and will not survive contact with a window.

Three defenses, in order of value:

1. **An 850 nm bandpass or IR-pass filter over the lens.** Blocks everything
   below ~800 nm, which is most of the ambient energy, while passing your
   illuminator's band. Biggest single improvement available.
2. **Adaptive thresholding** (spec R2.2) — track ambient and require the blob to
   sit a fixed margin above it.
3. **Pulsed illumination with frame differencing** (spec R2.4) — the complete
   answer, since anything not retroreflective cancels out. Deferred because USB
   cameras give no exposure sync.

### Concealment and filtering are the same component

IR-pass acrylic (Plexiglas 3143 and equivalents) looks near-black to the eye and
transmits above 85% past 800 nm. A panel of it in front of the camera and
illuminators hides both completely *and* does the job of the bandpass filter.

This is worth designing around, because it means the concealment isn't a
compromise imposed on the optics — it's an improvement to them.

**Measure before you commit.** Cheap acrylic varies. Take a headroom reading
(`wand/health`, spec R2.3) with and without the panel in place. If the panel
costs more than roughly 30% of your headroom, either go thinner or move to 940 nm
illuminators, which sit further from the panel's cutoff.

### On "concealed"

850 nm LEDs produce a visible dark-red glow when running. Behind IR-pass acrylic
in a lit room they're invisible; in a dark room, at night, someone looking
directly at the panel will see a faint red shimmer. 940 nm eliminates this at the
cost of some return signal — a real tradeoff, not a free win.

Also worth deciding deliberately rather than by default: this is a camera in a
living room that guests can't see. Spec R3.3 covers keeping the stream off the
network by default, but the social question is yours. A visible-when-you-know-to-
look design ages better than a genuinely covert one.

---

## Geometry

| | Value |
|---|---|
| Prop mounting height | 1.2–1.8 m (shelf or wall, roughly chest height) |
| Casting distance | 1.5–3.5 m |
| Camera FOV | 100–120° horizontal |
| Casting arc | Assume a 1 m square of usable air at 2.5 m |

Aim so the casting zone sits in the middle of the frame vertically. A camera
angled up at the ceiling picks up light fittings; angled down it picks up sunlit
floor. Both are ambient sources you'd rather exclude by framing than filter out
in software.

Because the camera is concealed, it can't be aimed by eye — spec R4.2 adds a
console overlay for this. Aim it before the panel goes on, and mark the position.

---

## Thermal

A Pi 4 at 15% CPU is fine passively. A Pi 4 at 15% CPU inside a sealed wooden
prop, next to a ring of IR LEDs running continuously, is not.

- Vent the base and the top — convection needs both
- Passive heatsink on the SoC; avoid fans, since this sits in a quiet room
- Illuminator LEDs need their own thermal path to something metal
- Spec R4.1 (switching the illuminators off when idle) is as much a thermal
  measure as a power one
- Spec R4.3 puts SoC temperature into `wand/health`, so throttling shows up as a
  number rather than as mysterious recognition failures

---

## Prop concepts

Ranked by how well they hide a camera, not by how good they look.

**Foe-Glass / dark mirror.** The strongest option. A smoked or one-way panel is
already the aesthetic, so nothing needs concealing — the hardware just sits
behind it. Round frame, aged gilt, mounted at chest height. The in-universe
behavior (something watching from inside the glass) matches what the device
actually does, which is a rare alignment.

**Framed portrait.** Portraits in this universe watch people, so a lens behind a
painted pupil or a dark background detail is thematically honest. Gives real
depth for electronics. Harder: the aperture has to be small enough to disappear
and large enough not to vignette a 120° lens.

**Lantern or wall sconce.** The dark glass panel is expected, and a NeoPixel ring
behind the same diffuser is a free win for the feedback requirement — the lantern
literally lights up when you cast Lumos. Weakness: lanterns are usually mounted
high, which is the wrong height for casting.

**Stack of spellbooks.** Easy to build, easy to vent, but a lens in the spine of
a book at shelf height has a narrow, low viewing angle.

Whichever you pick, the panel needs to be flat and perpendicular to the lens
axis. A curved or angled cover adds refraction that will smear the blob.

---

## Build order

Do not cut the enclosure until the optics are measured.

1. Assemble camera, illuminators, and Pi on a board. No enclosure.
2. Put it in the actual room, at the actual height.
3. Record ambient headroom at 9am, 3pm, and 9pm. Sunny day and overcast.
4. Repeat with the acrylic panel held in front.
5. *Now* design the enclosure, using the geometry those numbers imply.

Step 3 is the number the whole project depends on. Everything in `SPEC.md`
Phase 2 is tuning against it, and a finished prop is expensive to re-cut.
