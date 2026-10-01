"""Local feedback: LEDs and sound, with a working no-op default.

Nobody should need a phone to know whether a spell landed. The device is on a
shelf with no screen, so the only honest answers are light and sound.

Everything here is optional. The default backends do nothing, hardware imports
happen lazily inside the backend that needs them, and a backend that raises is
disabled for the rest of the run after its first failure — a broken LED must
never stop spell recognition.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, time as clock_time

log = logging.getLogger(__name__)

# What each event should look like. Colours come from the spell for a cast.
BOOT = "boot"
MQTT_DOWN = "mqtt_down"
WAND_SEEN = "wand_seen"
CAST = "cast"
REJECTED = "rejected"
SAMPLE = "sample"


def _parse_clock(value: str) -> clock_time | None:
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        return clock_time(hour, minute)
    except (TypeError, ValueError):
        return None


def in_quiet_hours(window: list[str] | tuple[str, str], now: datetime | None = None) -> bool:
    """Whether `now` falls inside the quiet window.

    Handles a window that wraps midnight, which is the only shape anyone
    actually configures: 22:30 to 07:00 is two ranges, not one.
    """
    if not window or len(window) != 2:
        return False
    start, end = _parse_clock(window[0]), _parse_clock(window[1])
    if start is None or end is None:
        return False
    current = (now or datetime.now()).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


class NullLeds:
    """The default. Does nothing, costs nothing, imports nothing."""

    name = "none"

    def show(self, event: str, colour: str | None = None, brightness: float = 1.0) -> None:
        pass

    def close(self) -> None:
        pass


class NullSound:
    name = "none"

    def play(self, event: str, spell_id: str | None = None) -> None:
        pass

    def close(self) -> None:
        pass


class NeoPixelLeds:
    """WS2812B ring. Imports rpi_ws281x lazily, because a laptop has none."""

    name = "neopixel"

    def __init__(self, pin: int = 18, count: int = 16, brightness: float = 0.4):
        from rpi_ws281x import Adafruit_NeoPixel

        self._strip = Adafruit_NeoPixel(count, pin, brightness=int(brightness * 255))
        self._strip.begin()
        self._count = count

    def show(self, event: str, colour: str | None = None, brightness: float = 1.0) -> None:
        from rpi_ws281x import Color

        rgb = colour or "#ffb648"
        r, g, b = (int(rgb[i:i + 2], 16) for i in (1, 3, 5))
        scale = max(0.0, min(1.0, brightness))
        packed = Color(int(r * scale), int(g * scale), int(b * scale))
        for i in range(self._count):
            self._strip.setPixelColor(i, packed)
        self._strip.show()

    def close(self) -> None:
        try:
            self.show("off", "#000000", 0.0)
        except Exception:
            pass


class AplaySound:
    """Plays a wav per spell if one exists, else a shared chime."""

    name = "aplay"

    def __init__(self, sound_dir: str = "/data/sounds"):
        import shutil

        if not shutil.which("aplay"):
            raise RuntimeError("aplay is not installed")
        self._dir = sound_dir

    def play(self, event: str, spell_id: str | None = None) -> None:
        import os
        import subprocess

        for candidate in (f"{spell_id}.wav" if spell_id else None, f"{event}.wav", "chime.wav"):
            if not candidate:
                continue
            path = os.path.join(self._dir, candidate)
            if os.path.isfile(path):
                subprocess.Popen(["aplay", "-q", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return

    def close(self) -> None:
        pass


LED_BACKENDS = {"none": NullLeds, "neopixel": NeoPixelLeds}
SOUND_BACKENDS = {"none": NullSound, "aplay": AplaySound}


class Feedback:
    """Fans events out to the configured backends, on its own thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.leds = self._build(LED_BACKENDS, cfg.leds, "LED", pin=cfg.led_pin,
                                count=cfg.led_count, brightness=cfg.led_brightness)
        self.sound = self._build(SOUND_BACKENDS, cfg.sound, "sound", sound_dir=cfg.sound_dir)
        self.failures: dict[str, str] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _build(self, registry, name: str, kind: str, **kwargs):
        factory = registry.get(name)
        if factory is None:
            log.warning("Unknown %s backend %r; using none", kind, name)
            return registry["none"]()
        if factory in (NullLeds, NullSound):
            return factory()
        try:
            if factory is NeoPixelLeds:
                return factory(pin=kwargs["pin"], count=kwargs["count"],
                               brightness=kwargs["brightness"])
            return factory(sound_dir=kwargs["sound_dir"])
        except Exception as exc:
            # One line, not a stack trace: running without the hardware is an
            # expected configuration, not a crash.
            log.info("%s backend %r unavailable (%s); continuing without it", kind, name, exc)
            return registry["none"]()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if isinstance(self.leds, NullLeds) and isinstance(self.sound, NullSound):
            return                      # nothing to run; stay a true no-op
        self._thread = threading.Thread(target=self._loop, name="feedback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for backend in (self.leds, self.sound):
            try:
                backend.close()
            except Exception:
                pass

    def quiet(self, now: datetime | None = None) -> bool:
        return in_quiet_hours(self.cfg.quiet_hours, now)

    def event(self, name: str, colour: str | None = None, spell_id: str | None = None) -> None:
        """Queue an event. Never blocks the caller — including the capture loop."""
        try:
            self._queue.put_nowait((name, colour, spell_id))
        except queue.Full:
            pass                        # feedback is not worth stalling a cast for

    # -- the worker -------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                name, colour, spell_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            quiet = self.quiet()
            brightness = self.cfg.led_brightness * (0.25 if quiet else 1.0)
            self._safely("leds", lambda: self.leds.show(name, colour, brightness))
            if not quiet:
                self._safely("sound", lambda: self.sound.play(name, spell_id))

    def _safely(self, which: str, call) -> None:
        backend = getattr(self, which)
        if isinstance(backend, (NullLeds, NullSound)):
            return
        try:
            call()
        except Exception as exc:
            # Disabled for the rest of the run: a backend that raises once will
            # raise every frame, and the log would bury everything else.
            self.failures[which] = f"{type(exc).__name__}: {exc}"
            log.warning("%s backend failed and is now disabled: %s", which, exc)
            setattr(self, which, NullLeds() if which == "leds" else NullSound())

    def status(self) -> dict:
        return {
            "leds": getattr(self.leds, "name", "none"),
            "sound": getattr(self.sound, "name", "none"),
            "quiet_hours": list(self.cfg.quiet_hours),
            "quiet_now": self.quiet(),
            "failures": dict(self.failures),
        }
