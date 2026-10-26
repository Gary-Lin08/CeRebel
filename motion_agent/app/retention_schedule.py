"""Run the data-retention job once a day on a machine with supervisord but no cron or timers.

The GPU box only has supervisord, which keeps programs running but cannot schedule them.
deploy/supervisor/cerebel-retention.conf keeps this loop alive. It sleeps until the daily time,
runs ``python -m motion_agent.app.retention`` in a child process (so a redeploy is picked up
without restarting the loop) and goes back to sleep.

    CEREBEL_RETENTION_AT=07:41     daily time in UTC (default 07:41, same as the US systemd timer)

The time of the last real run is read from <data>/product/retention.jsonl. Like a systemd timer
with Persistent=true, a run missed while the machine was off happens as soon as the loop starts
again; with no earlier run at all, the first one waits for the daily time. A failed run is
retried an hour later.

    python3 -m motion_agent.app.retention_schedule           # the loop (supervisor runs this)
    python3 -m motion_agent.app.retention_schedule --next    # print when the next run is due
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from motion_agent.app.config import load_settings

LOG = logging.getLogger("motion_agent.retention_schedule")

DEFAULT_AT = "07:41"
RETRY_AFTER = timedelta(hours=1)
# Wake at least this often, so a changed clock or a suspended machine cannot make the loop oversleep.
MAX_SLEEP_S = 3600.0
RUN_TIMEOUT_S = 6 * 3600
REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_at(value: str | None) -> tuple[int, int]:
    """'07:41' -> (7, 41). Invalid values fall back to the default."""
    text = (value or "").strip() or DEFAULT_AT
    try:
        hour, minute = (int(part) for part in text.split(":"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except ValueError:
        pass
    LOG.warning("CEREBEL_RETENTION_AT=%r is not HH:MM; using %s UTC", value, DEFAULT_AT)
    return parse_at(DEFAULT_AT)


def last_scheduled(now: datetime, at: tuple[int, int]) -> datetime:
    """The most recent daily slot at or before ``now``."""
    slot = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=1)


def next_scheduled(now: datetime, at: tuple[int, int]) -> datetime:
    """The first daily slot after ``now``."""
    return last_scheduled(now, at) + timedelta(days=1)


def last_run(product_dir: Path) -> datetime | None:
    """When the retention job last really ran (dry runs are not logged)."""
    path = product_dir / "retention.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
            if row.get("dry_run"):
                continue
            stamp = datetime.fromisoformat(str(row["now"]))
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
            continue
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return None


def due(now: datetime, last: datetime, at: tuple[int, int]) -> bool:
    """True once a daily slot has passed since ``last``."""
    return last < last_scheduled(now, at)


def run_retention() -> int:
    cmd = [sys.executable, "-u", "-m", "motion_agent.app.retention"]
    try:
        return subprocess.run(cmd, cwd=REPO_ROOT, timeout=RUN_TIMEOUT_S, check=False).returncode
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOG.error("retention run failed: %s", exc)
        return 1


def loop(
    product_dir: Path,
    at: tuple[int, int],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    runner: Callable[[], int] = run_retention,
    iterations: int | None = None,
) -> None:
    """Run ``runner`` once per daily slot. ``iterations`` bounds the loop for tests."""
    started = clock()
    done_at: datetime | None = None  # last successful run of this process
    retry_at: datetime | None = None
    if last_run(product_dir) is None:
        # Never ran here: wait for the daily time instead of deleting the moment this is installed.
        LOG.info("no earlier retention run logged; first run at %s", next_scheduled(started, at).isoformat())
    count = 0
    while iterations is None or count < iterations:
        count += 1
        now = clock()
        last = max((s for s in (last_run(product_dir), done_at) if s is not None), default=started)
        if due(now, last, at) and (retry_at is None or now >= retry_at):
            LOG.info("running retention (last run %s)", last.isoformat())
            code = runner()
            if code == 0:
                done_at, retry_at = clock(), None
            else:
                retry_at = clock() + RETRY_AFTER
                LOG.error("retention exited with %s; retrying at %s", code, retry_at.isoformat())
            continue
        wake = next_scheduled(now, at)
        if retry_at is not None and retry_at < wake:
            wake = retry_at
        sleep(max(1.0, min((wake - now).total_seconds(), MAX_SLEEP_S)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CeRebel data retention once a day.")
    parser.add_argument("--at", default=os.environ.get("CEREBEL_RETENTION_AT", DEFAULT_AT),
                        help="daily time in UTC, HH:MM (default: CEREBEL_RETENTION_AT or 07:41)")
    parser.add_argument("--next", action="store_true", help="print the last and next run and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    at = parse_at(args.at)
    product_dir = load_settings().product_dir
    if args.next:
        now = datetime.now(timezone.utc)
        last = last_run(product_dir)
        print(f"last run: {last.isoformat() if last else 'never'}")
        print(f"next run: {now.isoformat() if last and due(now, last, at) else next_scheduled(now, at).isoformat()}")
        return 0
    LOG.info("retention scheduler started; daily at %02d:%02d UTC", *at)
    loop(product_dir, at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
