"""systemd readiness and watchdog notification.

Deliberately not a dependency: it is a datagram to a unix socket whose path
systemd puts in NOTIFY_SOCKET, and the whole protocol used here is two strings.
Pulling in python-systemd for that would fail CLAUDE.md's bar for a new runtime
dependency, and would not build on a dev laptop anyway.

Every function is a no-op when NOTIFY_SOCKET is unset, which is the normal case
outside systemd — a dev machine, Docker, the test suite.
"""

from __future__ import annotations

import logging
import os
import socket
import threading

log = logging.getLogger(__name__)


def _send(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    # A leading @ means an abstract namespace socket, spelled with a NUL.
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
        return True
    except OSError as exc:
        # Never fatal: failing to tell systemd we are alive must not be the
        # reason we stop being alive.
        log.debug("sd_notify %r failed: %s", message, exc)
        return False


def ready() -> bool:
    """Tell systemd the service is up. Needed by Type=notify."""
    return _send("READY=1")


def stopping() -> bool:
    return _send("STOPPING=1")


def watchdog_interval() -> float | None:
    """Half of WatchdogSec, in seconds, or None if no watchdog is configured.

    Half, because systemd expects pings comfortably inside the window and a
    single slow frame should not look like a hang.
    """
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec or not os.environ.get("NOTIFY_SOCKET"):
        return None
    try:
        return max(1.0, int(usec) / 2_000_000)
    except ValueError:
        return None


def start_watchdog(stop: threading.Event) -> threading.Thread | None:
    """Ping systemd until `stop` is set. Returns None when there is no watchdog.

    The unit carried WatchdogSec before this existed, with nothing sending the
    pings — so systemd declared the process hung and restarted it every 60
    seconds. That is why day 1 removed it and why this has to land before it
    comes back.
    """
    interval = watchdog_interval()
    if interval is None:
        return None

    def loop() -> None:
        while not stop.wait(interval):
            _send("WATCHDOG=1")

    thread = threading.Thread(target=loop, name="sd-watchdog", daemon=True)
    thread.start()
    log.info("systemd watchdog: pinging every %.1fs", interval)
    return thread
