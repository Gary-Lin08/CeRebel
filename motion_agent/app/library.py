"""Per-user capture library. Anonymous demo still uses the newest ready session."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(product_dir: Path) -> Path:
    product_dir.mkdir(parents=True, exist_ok=True)
    return product_dir / "library.json"


def _read(product_dir: Path) -> list[dict[str, Any]]:
    path = _path(product_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write(product_dir: Path, rows: list[dict[str, Any]]) -> None:
    path = _path(product_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_capture(
    product_dir: Path,
    *,
    user_id: str,
    session_id: str,
    filename: str,
    status: str = "processing",
) -> dict[str, Any]:
    row = {
        "user_id": user_id,
        "session_id": session_id,
        "filename": filename,
        "status": status,
        "created_at": _now(),
        "deleted": False,
    }
    with _LOCK:
        rows = _read(product_dir)
        rows.append(row)
        _write(product_dir, rows)
    return row


def list_captures(product_dir: Path, user_id: str) -> list[dict[str, Any]]:
    rows = [
        r
        for r in _read(product_dir)
        if r.get("user_id") == user_id and not r.get("deleted")
    ]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def session_ids_for_user(product_dir: Path, user_id: str) -> list[str]:
    return [str(row["session_id"]) for row in list_captures(product_dir, user_id) if row.get("session_id")]


def owns(product_dir: Path, user_id: str, session_id: str) -> bool:
    return any(r.get("session_id") == session_id for r in list_captures(product_dir, user_id))


def owner_of(product_dir: Path, session_id: str) -> str | None:
    """Return the account that uploaded this capture, including deleted rows."""
    for row in _read(product_dir):
        if row.get("session_id") == session_id and row.get("user_id"):
            return str(row["user_id"])
    return None


def update_status(product_dir: Path, session_id: str, status: str, **extra: Any) -> None:
    with _LOCK:
        rows = _read(product_dir)
        for row in rows:
            if row.get("session_id") == session_id and not row.get("deleted"):
                row["status"] = status
                row.update(extra)
        _write(product_dir, rows)


def delete_capture(product_dir: Path, user_id: str, session_id: str) -> bool:
    with _LOCK:
        rows = _read(product_dir)
        found = False
        for row in rows:
            if row.get("user_id") == user_id and row.get("session_id") == session_id:
                row["deleted"] = True
                row["status"] = "deleted"
                found = True
        if found:
            _write(product_dir, rows)
        return found


PRACTICE_LOG_LIMIT = 100


def log_practice(product_dir: Path, user_id: str, session_id: str, finding_id: str) -> int | None:
    """Record that the user practiced the drill for one fix of their capture.

    Stored on the capture's own row, so deleting the capture or the account removes it too.
    Returns how many times that fix has been practiced, or None when the capture is not theirs.
    """
    with _LOCK:
        rows = _read(product_dir)
        for row in rows:
            if row.get("user_id") == user_id and row.get("session_id") == session_id and not row.get("deleted"):
                log = [p for p in (row.get("practice") or []) if isinstance(p, dict)]
                log.append({"finding_id": finding_id, "at": _now()})
                row["practice"] = log[-PRACTICE_LOG_LIMIT:]
                _write(product_dir, rows)
                return sum(1 for p in row["practice"] if p.get("finding_id") == finding_id)
        return None


def captures_today(product_dir: Path, user_id: str) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(
        1
        for r in list_captures(product_dir, user_id)
        if str(r.get("created_at") or "").startswith(today)
    )


def all_captures(product_dir: Path) -> list[dict[str, Any]]:
    """Every library row, including deleted ones (for the retention job)."""
    return [dict(r) for r in _read(product_dir)]


def purge_user_rows(product_dir: Path, user_id: str) -> int:
    """Hard-delete every library row of an account (used when the account itself is deleted)."""
    with _LOCK:
        rows = _read(product_dir)
        keep = [r for r in rows if r.get("user_id") != user_id]
        removed = len(rows) - len(keep)
        if removed:
            _write(product_dir, keep)
        return removed
