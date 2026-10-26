"""Exclusive lock on a product data file, held across processes.

chat_server appends to events.jsonl and rewrites library.json; retention and privacy_backfill run
as separate processes and rewrite the same files (read everything, write a .tmp, replace). A
threading.Lock only covers one process, so every writer also takes an flock on <file>.lock. The
lock file is never replaced, so the lock still holds while the data file itself is swapped out.
Appenders must open the data file after taking the lock, or they may append to the old copy.
Readers need no lock: replace is atomic, they see the old or the new file.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
from pathlib import Path
from typing import Iterator


def lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def locked(path: Path, thread_lock: threading.Lock | None = None) -> Iterator[None]:
    """Hold `thread_lock` (if given) and an exclusive flock on `path`'s lock file. Not re-entrant."""
    with thread_lock if thread_lock is not None else contextlib.nullcontext():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the flock
