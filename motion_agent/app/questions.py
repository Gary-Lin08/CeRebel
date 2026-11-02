"""What signed-in users typed to the coach, for the admin dashboard and to improve the coach.

Only the question is kept, never the answer, for CEREBEL_QUESTION_RETENTION_DAYS (default 90).
Rows go away earlier with the swing they were asked about or with the account. Questions from
visitors who are not signed in are not stored.

Accounts that accepted terms before STORE_FROM_TERMS were told "we do not keep a transcript", so
their questions are not stored either until they accept the current policy.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motion_agent.app.file_lock import locked

_LOCK = threading.Lock()
MAX_CHARS = 1000
STORE_FROM_TERMS = "2026-10-26"


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("CEREBEL_QUESTION_RETENTION_DAYS") or 90))
    except ValueError:
        return 90


def _path(product_dir: Path) -> Path:
    product_dir.mkdir(parents=True, exist_ok=True)
    return product_dir / "questions.jsonl"


def may_store(product_dir: Path, user_id: str) -> bool:
    from motion_agent.app.accounts import profile

    return str(profile(product_dir, user_id).get("terms_version") or "") >= STORE_FROM_TERMS


def record(product_dir: Path, user_id: str, session_id: str | None, question: str) -> None:
    if not user_id or not question:
        return
    path = _path(product_dir)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "session_id": session_id or None,
        "question": question[:MAX_CHARS],
    }
    with locked(path, _LOCK), path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def rows(product_dir: Path, *, user_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Newest first."""
    path = _path(product_dir)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and (user_id is None or row.get("user_id") == user_id):
            out.append(row)
    out.reverse()
    return out[:limit] if limit else out


def scrub(product_dir: Path, *, user_id: str | None = None, session_ids: list[str] | None = None,
          older_than: datetime | None = None, dry_run: bool = False) -> int:
    """Remove rows of a deleted account or swing, or rows older than the cutoff."""
    path = _path(product_dir)
    if not path.is_file():
        return 0
    sids = set(session_ids or [])
    kept: list[str] = []
    removed = 0
    with locked(path, _LOCK):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                removed += 1
                continue
            drop = bool(user_id) and row.get("user_id") == user_id
            drop = drop or (bool(sids) and row.get("session_id") in sids)
            if older_than is not None and not drop:
                try:
                    ts = datetime.fromisoformat(str(row.get("ts")))
                    drop = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)) < older_than
                except (TypeError, ValueError):
                    drop = True
            if drop:
                removed += 1
            else:
                kept.append(line)
        if removed and not dry_run:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(k + "\n" for k in kept), encoding="utf-8")
            tmp.replace(path)
    return removed
