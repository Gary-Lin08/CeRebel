"""Limits on repeated attempts: failed sign-ins, admin password guesses, reset emails, feedback.

One server process (chat_server) handles these routes, so the counters live in memory; a
restart clears them, which only ever gives an attacker one fresh window.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class Limiter:
    """At most `limit` hits per key within `window_s` seconds."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 10_000:
                for stale in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window_s]:
                    self._hits.pop(stale, None)
            self._recent(key, now)
            self._hits.setdefault(key, deque()).append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear_all(self) -> None:
        with self._lock:
            self._hits.clear()


# Sign-in: an attacker guessing one account's password is stopped by the per-email limit; the
# per-IP limit is looser because many people can share an address (and the header can be forged).
LOGIN_PER_EMAIL = Limiter(10, 15 * 60)
LOGIN_PER_IP = Limiter(50, 15 * 60)
# "Forgot password" emails: no mailbox flooding.
RESET_PER_EMAIL = Limiter(3, 60 * 60)
RESET_PER_IP = Limiter(10, 60 * 60)
# Admin password guesses, per account and per IP.
ADMIN_FAILS = Limiter(5, 15 * 60)
# Written feedback per account per day.
FEEDBACK_PER_USER = Limiter(20, 24 * 60 * 60)
# Public website routes. A heartbeat every 30 seconds is 20 per 10 minutes, so this allows a full
# office behind one address. The IP header can be forged, so stored rows also have a global cap.
SITE_PING_PER_IP = Limiter(600, 10 * 60)
SITE_WRITES = Limiter(50_000, 24 * 60 * 60)
BACKER_PER_IP = Limiter(10, 60 * 60)


def clear_all() -> None:
    for limiter in (
        LOGIN_PER_EMAIL, LOGIN_PER_IP, RESET_PER_EMAIL, RESET_PER_IP, ADMIN_FAILS, FEEDBACK_PER_USER,
        SITE_PING_PER_IP, SITE_WRITES, BACKER_PER_IP,
    ):
        limiter.clear_all()
