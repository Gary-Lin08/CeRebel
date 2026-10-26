"""In-process sliding-window limits for sign-in and sign-up.

The product runs one web process, so counters live in memory: a restart forgets them, which
only ever loosens the limit for a few minutes. Keys are emails and visitor IPs; nothing is
written to disk.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


class SlidingWindow:
    """At most `limit` hits per key within `window_s` seconds."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        return hits

    def blocked(self, key: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._live(key, now)
            if not hits:
                self._hits.pop(key, None)
            return len(hits) >= self.limit

    def hit(self, key: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._live(key, now).append(now)
            if len(self._hits) > 50_000:
                # Bound memory under a spray of distinct keys: drop keys with no recent hits.
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window_s]:
                    del self._hits[k]

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


WINDOW_S = 15 * 60
# Failed sign-ins: per account (guessing one password) and per visitor (spraying many accounts).
LOGIN_FAILS_PER_EMAIL = SlidingWindow(_env_int("CEREBEL_LOGIN_FAILS_PER_EMAIL", 10), WINDOW_S)
LOGIN_FAILS_PER_IP = SlidingWindow(_env_int("CEREBEL_LOGIN_FAILS_PER_IP", 50), WINDOW_S)
# New accounts per visitor per hour.
SIGNUPS_PER_IP = SlidingWindow(_env_int("CEREBEL_SIGNUPS_PER_IP_HOUR", 20), 3600)

TOO_MANY_LOGINS = "Too many sign-in attempts. Wait 15 minutes and try again."
TOO_MANY_SIGNUPS = "Too many new accounts from this network. Try again in an hour."
