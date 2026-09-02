"""Entry point.

Builds the pieces, starts the engine, and then either runs the console or idles.
The engine starts before the server so that `--no-server` is a genuinely
headless mode rather than a degraded one — recognition never depends on HTTP.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from .camera import Camera
from .config import load_config
from .engine import Engine
from .mqtt_bridge import MqttBridge
from .recognizer import Recognizer
from .spells import SPELLS
from .tracker import Tracker

log = logging.getLogger("wandportal")


def build_engine(config) -> Engine:
    camera = Camera(
        index=config.camera.index,
        width=config.camera.width,
        height=config.camera.height,
        fps=config.camera.fps,
        flip_horizontal=config.camera.flip_horizontal,
        flip_vertical=config.camera.flip_vertical,
        reopen_delay=config.camera.reopen_delay,
    )
    tracker = Tracker(
        threshold=config.tracker.threshold,
        min_area=config.tracker.min_area,
        max_area=config.tracker.max_area,
        max_jump=config.tracker.max_jump,
        smoothing=config.tracker.smoothing,
        start_frames=config.tracker.start_frames,
        lost_frames=config.tracker.lost_frames,
        min_points=config.tracker.min_points,
        min_path_length=config.tracker.min_path_length,
        max_gesture_seconds=config.tracker.max_gesture_seconds,
        threshold_mode=config.tracker.threshold_mode,
    )
    recognizer = Recognizer(
        templates_path=config.recognizer.templates_path,
        resample_points=config.recognizer.resample_points,
        min_confidence=config.recognizer.min_confidence,
        min_margin=config.recognizer.min_margin,
        max_samples_per_spell=config.recognizer.max_samples_per_spell,
    )
    mqtt = MqttBridge(
        host=config.mqtt.host,
        port=config.mqtt.port,
        username=config.mqtt.username,
        password=config.mqtt.password,
        client_id=config.mqtt.client_id,
        base_topic=config.mqtt.base_topic,
        discovery_prefix=config.mqtt.discovery_prefix,
        device_name=config.mqtt.device_name,
        pulse_seconds=config.mqtt.pulse_seconds,
        keepalive=config.mqtt.keepalive,
        enabled=config.mqtt.enabled,
    )
    return Engine(config, camera, tracker, recognizer, mqtt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wandportal", description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--no-server", action="store_true", help="run headless, no web console")
    parser.add_argument("--list-spells", action="store_true", help="print the spell catalog and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.list_spells:
        width = max(len(s.id) for s in SPELLS)
        for spell in SPELLS:
            print(f"{spell.id:<{width}}  {spell.name:<20} {spell.effect}")
        print(f"\n{len(SPELLS)} spells")
        return 0

    config = load_config(args.config)
    engine = build_engine(config)
    engine.start()

    stopping = threading.Event()

    def shutdown(signum, _frame) -> None:
        log.info("Signal %s, shutting down", signum)
        stopping.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        if args.no_server or not config.server.enabled:
            log.info("Headless mode; no web console")
            while not stopping.is_set():
                stopping.wait(1.0)
        else:
            import uvicorn

            from .server import create_app

            uvicorn_config = uvicorn.Config(
                create_app(engine),
                host=config.server.bind,
                port=config.server.port,
                log_level="debug" if args.verbose else "warning",
            )
            server = uvicorn.Server(uvicorn_config)
            # uvicorn installs its own signal handlers; ours above only matter in
            # headless mode.
            server.run()
    finally:
        engine.stop()
        log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
