"""Simple in-memory LRU-ish cache for interactive demo latency."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, Hashable


class ResultCache:
    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._store: OrderedDict[str, Any] = OrderedDict()

    @staticmethod
    def make_key(prefix: str, payload: Hashable | dict | list) -> str:
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload, sort_keys=True, default=str)
        else:
            raw = repr(payload)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}:{digest}"

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def set(self, key: str, value: Any) -> Any:
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


GLOBAL_CACHE = ResultCache()
