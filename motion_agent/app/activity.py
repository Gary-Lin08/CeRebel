"""Signed-in visits: when each account used the app and for how long (for the admin dashboard).

A visit starts with the first signed-in request and grows while requests keep arriving less than
VISIT_GAP apart; the page pings once a minute while it is visible, so reading a report counts too.
Stored per account in data/product/activity.json, deleted with the account, and pruned by the
retention job after the inactivity limit.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from motion_agent.app.file_lock import locked

_LOCK = threading.Lock()
VISIT_GAP = timedelta(minutes=30)
# Requests arrive in bursts; the file is rewritten at most once a minute per account and process.
WRITE_EVERY = timedelta(seconds=60)
MAX_VISITS = 500
_last_write: dict[str, datetime] = {}


def _path(product_dir: Path) -> Path:
    product_dir.mkdir(parents=True, exist_ok=True)
    return product_dir / "activity.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def _utc(text: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def touch(product_dir: Path, user_id: str, *, now: datetime | None = None) -> None:
    """Extend the account's current visit, or start a new one after a gap."""
    if not user_id:
        return
    now = now or datetime.now(timezone.utc)
    key = f"{product_dir}:{user_id}"
    last = _last_write.get(key)
    if last is not None and timedelta(0) <= now - last < WRITE_EVERY:
        return
    _last_write[key] = now
    path = _path(product_dir)
    with locked(path, _LOCK):
        data = _read(path)
        row = data.setdefault(user_id, {})
        visits = row.setdefault("visits", [])
        end = _utc(visits[-1].get("end")) if visits else None
        if end is not None and timedelta(0) <= now - end <= VISIT_GAP:
            visits[-1]["end"] = now.isoformat()
        else:
            visits.append({"start": now.isoformat(), "end": now.isoformat()})
            del visits[:-MAX_VISITS]
        _write(path, data)


def visits_for(product_dir: Path, user_id: str) -> list[dict[str, Any]]:
    """Newest first, each with its length in seconds."""
    rows = (_read(_path(product_dir)).get(user_id) or {}).get("visits") or []
    return [_with_seconds(v) for v in reversed(rows) if isinstance(v, dict)]


def all_visits(product_dir: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        uid: [_with_seconds(v) for v in (row.get("visits") or []) if isinstance(v, dict)]
        for uid, row in _read(_path(product_dir)).items()
        if isinstance(row, dict)
    }


def _with_seconds(visit: dict[str, Any]) -> dict[str, Any]:
    start, end = _utc(visit.get("start")), _utc(visit.get("end"))
    seconds = max(0, int((end - start).total_seconds())) if start and end else 0
    return {"start": visit.get("start"), "end": visit.get("end"), "seconds": seconds}


def forget(product_dir: Path, user_id: str) -> None:
    """Remove every visit of a deleted account."""
    path = _path(product_dir)
    with locked(path, _LOCK):
        data = _read(path)
        if data.pop(user_id, None) is not None:
            _write(path, data)
    for key in [k for k in _last_write if k.endswith(":" + user_id)]:
        _last_write.pop(key, None)


def prune(product_dir: Path, *, older_than: datetime, keep_users: set[str] | None = None,
          dry_run: bool = False) -> int:
    """Drop visits that ended before the cutoff, and accounts that no longer exist."""
    path = _path(product_dir)
    removed = 0
    with locked(path, _LOCK):
        data = _read(path)
        for uid in list(data):
            row = data[uid] if isinstance(data[uid], dict) else {}
            visits = [v for v in row.get("visits") or [] if isinstance(v, dict)]
            if keep_users is not None and uid not in keep_users:
                removed += len(visits)
                del data[uid]
                continue
            kept = [v for v in visits if (_utc(v.get("end")) or older_than) >= older_than]
            removed += len(visits) - len(kept)
            if kept:
                data[uid] = {**row, "visits": kept}
            else:
                del data[uid]
        if removed and not dry_run:
            _write(path, data)
    return removed
