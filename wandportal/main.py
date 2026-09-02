"""Entrypoint: python -m wand"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from . import config as config_module
from .engine import Engine


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wandportal", description="IR wand spell recognition for Home Assistant")
    p.add_argument("-c", "--config", default=None, help="path to config.yaml")
    p.add_argument("--no-server", action="store_true", help="run headless, no web console")
    p.add_argument("--list-spells", action="store_true", help="print the spell catalog and exit")
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
    engine = Engine(cfg)

    stopping = False

    def shutdown(*_):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        logging.info("Shutting down")
        engine.stop()

    signal.signal(signal.SIGINT, lambda *a: (shutdown(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (shutdown(), sys.exit(0)))

    engine.start()
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
