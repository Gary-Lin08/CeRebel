"""Progress view: compare two of the signed-in player's own swings.

Only the account's own captures can be compared (never the demo, never another account).
Free accounts compare a swing with the one right before it; Pro accounts may pick any two.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from motion_agent.app import billing
from motion_agent.app.config import Settings
from motion_agent.app.library import list_captures
from motion_agent.motion.adapter import adapt_session
from motion_agent.motion.swing_compare import METRIC_BY_FINDING, compare_snapshots, swing_snapshot
from motion_agent.motion.swing_overlay import overlay_for_finding
from motion_agent.storage.video_store import media_url, resolve_media_file

LOG = logging.getLogger("motion_agent.compare")

PROCESSING = {"uploading", "extracting", "queued", "gemx", "ik", "processing"}
NOT_A_SWING = {"error", "no_swing", "deleted"}
PRIVATE = {"ok": False, "error": True, "message": "This capture is private"}


def _session_dir(settings: Settings, session_id: str) -> Path:
    return settings.data_dir / "sessions" / session_id


def _has_motion(settings: Settings, session_id: str) -> bool:
    return (_session_dir(settings, session_id) / "ik" / "opensim-motion.json").is_file()


def _row_ready(settings: Settings, row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "")
    return (
        status not in PROCESSING
        and status not in NOT_A_SWING
        and bool(row.get("session_id"))
        and _has_motion(settings, str(row["session_id"]))
    )


def _order_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("created_at") or ""), str(row.get("session_id") or ""))


def ready_captures(settings: Settings, user_id: str, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The account's measurable swings, oldest first."""
    rows = list_captures(settings.product_dir, user_id) if rows is None else rows
    return sorted((r for r in rows if _row_ready(settings, r)), key=_order_key)


def default_pair(settings: Settings, user_id: str, rows: list[dict[str, Any]] | None = None) -> dict[str, str] | None:
    ready = ready_captures(settings, user_id, rows)
    if len(ready) < 2:
        return None
    return {"before": str(ready[-2]["session_id"]), "after": str(ready[-1]["session_id"])}


def library_compare_block(settings: Settings, user_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "default": default_pair(settings, user_id, rows),
        "any_pair": billing.is_pro(settings.product_dir, user_id),
    }


def _load_pose3d(settings: Settings, session_id: str) -> dict[str, Any] | None:
    path = _session_dir(settings, session_id) / "ik" / "pose3d.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _media(settings: Settings, session_id: str) -> dict[str, Any]:
    if resolve_media_file(session_id, "session.mp4", settings.data_dir) is not None:
        return {"video_url": media_url(session_id), "video_removed": False}
    removed = (_session_dir(settings, session_id) / "retention.json").is_file()
    return {"video_url": None, "video_removed": removed}


def _side(settings: Settings, row: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    sid = str(row["session_id"])
    view = snap.get("view") or {}
    first = (snap.get("findings") or [None])[0]
    return {
        "session_id": sid,
        "filename": row.get("filename"),
        "created_at": row.get("created_at"),
        "swing_index": snap.get("swing_index", 0),
        "swing_count": snap.get("swing_count", 1),
        "events": snap.get("events") or {},
        "phases": [
            {
                "id": p.get("id"),
                "label": p.get("label"),
                "start_time": p.get("start_time"),
                "end_time": p.get("end_time"),
            }
            for p in snap.get("phases") or []
        ],
        "view": {k: view.get(k) for k in ("id", "label", "azimuth_deg", "known")},
        "fps": round(float(snap.get("fps") or 0.0), 2),
        "media": _media(settings, sid),
        "main_issue": {"finding_id": first["id"], "headline": first.get("headline")} if first else None,
    }


def _highlight(snap: dict[str, Any], finding_id: str | None, label: str | None) -> dict[str, Any] | None:
    """How to mark last time's main fix on this swing's video, and when."""
    spec = METRIC_BY_FINDING.get(finding_id or "")
    events = snap.get("events") or {}
    if spec is None:
        return None
    lead, trail = ("right", "left") if snap.get("handedness") == "left" else ("left", "right")
    if spec.event == "impact":
        start, end = events.get("impact"), events.get("finish")
    else:
        start, end = events.get("address"), events.get("top")
    if start is None or end is None:
        return None
    return {
        "finding_id": finding_id,
        "overlay": overlay_for_finding(str(finding_id), None, lead, trail),
        "start_time": start,
        "end_time": end,
        # The knee figure is bend lost, not the angle drawn at the knee, so it gets no label there.
        "label": None if spec.id == "trail_knee_loss" else label,
    }


def _swing_arg(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def compare_payload(
    settings: Settings,
    user: dict[str, Any],
    before_id: str | None,
    after_id: str | None,
    *,
    before_swing: Any = 0,
    after_swing: Any = 0,
) -> tuple[int, dict[str, Any]]:
    """GET /api/compare. Returns (status code, payload)."""
    user_id = str(user["user_id"])
    before_id = (before_id or "").strip()
    after_id = (after_id or "").strip()
    if not before_id or not after_id:
        return 400, {"ok": False, "error": True, "message": "Pick two swings to compare."}
    if before_id == after_id:
        return 400, {"ok": False, "error": True, "message": "Pick two different swings."}

    rows = list_captures(settings.product_dir, user_id)
    owned = {str(r.get("session_id")): r for r in rows}
    if before_id not in owned or after_id not in owned:
        return 403, dict(PRIVATE)
    for sid in (before_id, after_id):
        status = str(owned[sid].get("status") or "")
        if status in PROCESSING:
            return 409, {"ok": False, "error": True, "message": "That swing is still processing."}
        if status in NOT_A_SWING or not _has_motion(settings, sid):
            return 422, {"ok": False, "error": True, "message": "That clip doesn't have a swing we could measure."}

    before_row, after_row = owned[before_id], owned[after_id]
    swings = (_swing_arg(before_swing), _swing_arg(after_swing))
    reordered = _order_key(before_row) > _order_key(after_row)
    if reordered:
        before_row, after_row = after_row, before_row
        swings = (swings[1], swings[0])

    pro = billing.is_pro(settings.product_dir, user_id)
    ready_ids = [str(r["session_id"]) for r in ready_captures(settings, user_id, rows)]
    adjacent = ready_ids.index(str(after_row["session_id"])) - ready_ids.index(str(before_row["session_id"])) == 1
    if not pro and not adjacent:
        return 402, {
            "ok": False,
            "error": True,
            "upgrade": True,
            "reason": "compare_any_pair",
            "message": "Comparing any two swings is part of Pro. Free accounts compare each swing with the one before it.",
            "default": default_pair(settings, user_id, rows),
        }

    try:
        snaps = []
        for row, swing in ((before_row, swings[0]), (after_row, swings[1])):
            sid = str(row["session_id"])
            seq = adapt_session(_session_dir(settings, sid))
            snaps.append(swing_snapshot(seq, swing, pose3d=_load_pose3d(settings, sid)))
        result = compare_snapshots(snaps[0], snaps[1])
    except Exception:  # noqa: BLE001
        LOG.exception("compare failed for %s / %s", before_row.get("session_id"), after_row.get("session_id"))
        return 500, {"ok": False, "error": True, "message": "We couldn't compare these swings right now. Try again soon."}

    from motion_agent.app.product_api import record_event

    main = result.get("main_issue") or {}
    sides = (_side(settings, before_row, snaps[0]), _side(settings, after_row, snaps[1]))
    for side, snap, label in zip(sides, snaps, (main.get("before_display"), main.get("after_display"))):
        side["highlight"] = _highlight(snap, main.get("finding_id"), label)
    record_event(
        settings,
        "compare_view",
        user_id=user_id,
        before=before_row["session_id"],
        after=after_row["session_id"],
        adjacent=adjacent,
        level=result["comparability"]["level"],
        main_status=main.get("status"),
    )
    return 200, {
        "ok": True,
        "version": 1,
        "reordered": reordered,
        "adjacent": adjacent,
        "any_pair": pro,
        "before": sides[0],
        "after": sides[1],
        **result,
    }
