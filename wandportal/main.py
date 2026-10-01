"""Entrypoint: python -m wand"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from . import config as config_module
from . import sdnotify
from .engine import Engine


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wandportal", description="IR wand spell recognition for Home Assistant")
    p.add_argument("-c", "--config", default=None, help="path to config.yaml")
    p.add_argument("--no-server", action="store_true", help="run headless, no web console")
    p.add_argument("--list-spells", action="store_true", help="print the spell catalog and exit")
    p.add_argument("--selftest", action="store_true",
                   help="run the leave-one-out separation check and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.list_spells:
        from .spells import CATALOG
        width = max(len(s.id) for s in CATALOG)
        for s in CATALOG:
            print(f"{s.id:<{width}}  {s.name:<20} {s.suggested}")
        return 0

    cfg = config_module.load(args.config)

    problems = config_module.validate(cfg)
    if problems:
        logging.error("Refusing to start — %d problem(s) in %s:", len(problems), cfg.config_path)
        for problem in problems:
            logging.error("  - %s", problem)
        return 2

    if args.selftest:
        # Answer "are these spells actually separable?" over SSH, without
        # opening the console or touching the camera.
        from .recognizer import Recognizer
        from .spells import resolve
        enabled = [s.id for s in resolve(cfg.spells)]
        report = Recognizer(cfg.recognizer).self_test(enabled=enabled)
        print(f"samples   {report['samples']}")
        print(f"correct   {report['correct']}")
        print(f"accuracy  {report['accuracy']:.1%}" if report["samples"] else "accuracy  n/a")
        if report["confusions"]:
            print("confusions:")
            for spell, against in sorted(report["confusions"].items()):
                for other, count in sorted(against.items()):
                    print(f"  {spell} read as {other} x{count}")
        else:
            print("confusions: none")
        # Non-zero when a spell is being misread, so a cron or a deploy check
        # can act on it rather than needing a human to read the output.
        return 0 if report["samples"] and not report["confusions"] else 1

    if cfg.server.enabled and not cfg.server.auth_token:
        loopback = cfg.server.host in ("127.0.0.1", "localhost", "::1")
        if not loopback:
            logging.warning(
                "The console is unauthenticated on %s:%s — anyone on this network can "
                "watch the camera and retrain spells. Set server.auth_token in %s, or "
                "bind to 127.0.0.1.",
                cfg.server.host, cfg.server.port, cfg.config_path,
            )

    engine = Engine(cfg)

    stopping = False

    def shutdown(*_):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        logging.info("Shutting down")
        sdnotify.stopping()
        watchdog_stop.set()
        engine.stop()

    signal.signal(signal.SIGINT, lambda *a: (shutdown(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (shutdown(), sys.exit(0)))

    engine.start()

    # Tell systemd we are up, and keep telling it. No-ops outside systemd.
    watchdog_stop = threading.Event()
    sdnotify.start_watchdog(watchdog_stop)
    sdnotify.ready()
    logging.info("Tracking %d spells: %s", len(engine.spells), ", ".join(engine.spell_ids))

    if args.no_server or not cfg.server.enabled:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
        return 0

    import uvicorn
    from .server import create_app

    logging.info("Console at http://%s:%s", cfg.server.host, cfg.server.port)
    try:
        uvicorn.run(
            create_app(engine),
            host=cfg.server.host,
            port=cfg.server.port,
            log_level="warning",
            access_log=False,
        )
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
