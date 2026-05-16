"""YPK Job Scraper - entry point.

Runs a continuous loop that calls `tick()` every 5 seconds.
This is the minimal skeleton; pipeline stages will be added incrementally.
See context.md for the overall architecture and development style.
"""

from __future__ import annotations

import signal
import time
from datetime import datetime

TICK_INTERVAL_SECONDS = 5

_stop = False


def _request_stop(signum, _frame) -> None:
    global _stop
    _stop = True
    print(f"\nReceived signal {signum}. Stopping after current tick...", flush=True)


def tick(tick_number: int) -> None:
    """Single iteration of the pipeline. Currently a no-op heartbeat."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] tick #{tick_number}", flush=True)


def main() -> int:
    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (OSError, ValueError):
            pass

    print(
        f"Starting YPK Job Scraper. Ticking every {TICK_INTERVAL_SECONDS}s. "
        "Press Ctrl+C to stop.",
        flush=True,
    )

    tick_number = 0
    while not _stop:
        tick_number += 1
        started = time.monotonic()
        try:
            tick(tick_number)
        except Exception as exc:
            print(f"tick #{tick_number} failed: {exc!r}", flush=True)

        elapsed = time.monotonic() - started
        remaining = max(0.0, TICK_INTERVAL_SECONDS - elapsed)
        # Sleep in small slices so Ctrl+C is responsive.
        slept = 0.0
        while slept < remaining and not _stop:
            step = min(0.5, remaining - slept)
            time.sleep(step)
            slept += step

    print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
