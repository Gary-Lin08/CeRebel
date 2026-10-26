"""Data-retention enforcement for the consumer product.

Run once a day (cron / systemd timer):

    python3 -m motion_agent.app.retention --dry-run   # list what would be deleted
    python3 -m motion_agent.app.retention             # delete it

What it does, matching the public Privacy Policy:

1. Raw video. CEREBEL_RAW_VIDEO_RETENTION_DAYS (default 30) days after a capture was processed,
   delete the uploaded video and everything that shows the picture: upload/ (source and
   trimmed clip), jpegs/ (extracted frames), ik/session.mp4 and ik/poster.jpg, plus every file
   in the GEM-X output folder except hpe_results.pt. Derived results (3D body parameters,
   joint angles, estimated loads, meshes) are kept. A retention.json marker is left so the
   report can say the video was removed on schedule.
2. Inactive accounts. When an account has had no interaction for CEREBEL_INACTIVE_DELETE_DAYS
   (default 730, i.e. 24 months), delete the account and all its data, exactly like a
   user-requested deletion. Accounts with a renewing paid subscription are skipped and listed.
3. Unowned data. Session folders that belong to no account (except CEREBEL_DEMO_SESSION_ID)
   follow the same inactivity limit; GEM-X output folders whose session is gone follow the
   raw-video limit. Usage-log rows older than the inactivity limit are dropped.
4. Neck IK leftovers. Older builds wrote soma_raw.npz / soma_aligned.npz to
   kinetic-lens/retargeting_neck_v2/outputs/<session> (CEREBEL_NECK_OUTPUT_ROOT), outside the
   session folder. The session keeps its own copy in ik/, so these folders are removed as soon
   as their session is gone, and otherwise after the raw-video limit.
5. GEM-X logs. Older builds also wrote <session>_gemx.log next to the GEM-X output folders. Like
   the other files GEM-X leaves, they are removed after the raw-video limit.
6. Legacy usage log. The first web build (web_api.py) logged to <data>/product_events.jsonl
   (browser visitor ids, session ids). Its rows follow the same limit as events.jsonl, and the
   file is removed once empty. web_api.py takes no file lock, so do not run both at once.
7. Coach question counters. question_counts.json counts today's questions per account and per
   visitor IP; take_question only resets it on the next question, so an earlier day's counts are
   deleted here.

Every run is logged, and appended to <data>/product/retention.jsonl (ids and counts only).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from motion_agent.app.accounts import list_accounts, tokens_for_user
from motion_agent.app.billing import clear_old_question_counts
from motion_agent.app.config import Settings, load_settings
from motion_agent.app.consent import inactive_delete_days, raw_video_retention_days
from motion_agent.app.library import all_captures

LOG = logging.getLogger("motion_agent.retention")

# Paths inside a session folder that contain the picture itself.
RAW_SESSION_PATHS = (
    "upload",
    "jpegs",
    "ik/session.mp4",
    "ik/poster.jpg",
    "display/session.mp4",
    "display/poster.jpg",
)
# In the GEM-X output folder only this derived file is needed after processing.
GEMX_KEEP = {"hpe_results.pt"}
GEMX_LOG_SUFFIX = "_gemx.log"
MARKER = "retention.json"
LEGACY_EVENTS = "product_events.jsonl"


def _utc(ts: Any) -> datetime | None:
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        value = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError, OSError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except OSError:
        return 0


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def processed_at(session_dir: Path) -> datetime | None:
    """When processing finished: the IK result, else the last progress update, else the folder."""
    for rel in ("ik/opensim-motion.json", "upload_progress.json", "gemx.json"):
        stamp = _mtime(session_dir / rel)
        if stamp:
            return stamp
    return _mtime(session_dir)


def raw_video_paths(settings: Settings, session_id: str) -> list[Path]:
    from motion_agent.app.product_api import gemx_output_dir

    session_dir = settings.data_dir / "sessions" / session_id
    found = [session_dir / rel for rel in RAW_SESSION_PATHS if (session_dir / rel).exists()]
    gemx = gemx_output_dir(session_id)
    if gemx is not None:
        for item in sorted(gemx.rglob("*"), reverse=True):
            if item.is_file() and item.name not in GEMX_KEEP:
                found.append(item)
    return found


def _prune_empty_dirs(root: Path) -> None:
    for item in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if item.is_dir():
            try:
                item.rmdir()
            except OSError:
                pass


def last_interaction(account: dict[str, Any], captures: list[dict[str, Any]], tokens: list[dict[str, Any]]) -> datetime | None:
    stamps = [
        _utc(account.get(key))
        for key in ("created_at", "terms_accepted_at", "video_consent_at", "last_login_at", "last_active_at")
    ]
    stamps += [_utc(row.get("created_at")) for row in captures]
    stamps += [_utc(row.get("created_at")) for row in tokens]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def _renewing_subscription(settings: Settings, user_id: str) -> bool:
    from motion_agent.app import billing

    row = billing.subscription_for(settings.product_dir, user_id)
    return billing.is_pro(settings.product_dir, user_id) and not row.get("cancel_at_period_end")


def run(settings: Settings, *, dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
    from motion_agent.app.product_api import (
        gemx_output_dir,
        gemx_output_root,
        neck_output_root,
        purge_session_dir,
        remove_account,
        scrub_events,
    )

    now = now or datetime.now(timezone.utc)
    raw_days = raw_video_retention_days()
    inactive_days = inactive_delete_days()
    raw_cutoff = now - timedelta(days=raw_days)
    inactive_cutoff = now - timedelta(days=inactive_days)
    demo = os.environ.get("CEREBEL_DEMO_SESSION_ID", "").strip()
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "now": now.isoformat(),
        "raw_video_retention_days": raw_days,
        "inactive_delete_days": inactive_days,
        "accounts_deleted": [],
        "accounts_skipped": [],
        "raw_video_deleted": [],
        "orphan_sessions_deleted": [],
        "orphan_gemx_deleted": [],
        "neck_ik_deleted": [],
        "gemx_logs_deleted": [],
        "events_removed": 0,
        "legacy_events_removed": 0,
        "question_counts_cleared": 0,
    }

    # 1. Inactive accounts (first, so their sessions are not counted twice below).
    rows = all_captures(settings.product_dir)
    for account in list_accounts(settings.product_dir):
        user_id = str(account.get("user_id") or "")
        if not user_id:
            continue
        mine = [r for r in rows if r.get("user_id") == user_id]
        last = last_interaction(account, mine, tokens_for_user(settings.product_dir, user_id))
        if last is None or last >= inactive_cutoff:
            continue
        entry = {"user_id": user_id, "last_interaction": last.isoformat(),
                 "captures": len({r.get("session_id") for r in mine if r.get("session_id")})}
        if _renewing_subscription(settings, user_id):
            report["accounts_skipped"].append({**entry, "reason": "renewing paid subscription"})
            LOG.warning("inactive account %s has a renewing subscription; not deleted", user_id)
            continue
        if not dry_run:
            code, payload = remove_account(settings, {"user_id": user_id, "email": account.get("email")},
                                           reason="inactive")
            if code != 200:
                report["accounts_skipped"].append({**entry, "reason": payload.get("message") or str(code)})
                continue
        report["accounts_deleted"].append(entry)
        LOG.info("%s inactive account %s (last interaction %s)",
                 "would delete" if dry_run else "deleted", user_id, last.isoformat())

    gone_users = {e["user_id"] for e in report["accounts_deleted"]}
    rows = all_captures(settings.product_dir)
    owner = {str(r["session_id"]): r.get("user_id") for r in rows if r.get("session_id")}
    uploaded = {str(r["session_id"]): _utc(r.get("created_at")) for r in rows if r.get("session_id")}
    sessions_root = settings.data_dir / "sessions"
    session_dirs = sorted(p for p in sessions_root.iterdir() if p.is_dir()) if sessions_root.is_dir() else []

    for session_dir in session_dirs:
        sid = session_dir.name
        if sid == demo or owner.get(sid) in gone_users:
            continue
        # 2. Sessions nobody owns (legacy captures, leftovers) follow the inactivity limit.
        if owner.get(sid) is None:
            newest = max((m for m in (_mtime(p) for p in [session_dir, *session_dir.rglob("*")]) if m), default=None)
            if newest is not None and newest < inactive_cutoff:
                if not dry_run:
                    purge_session_dir(settings, sid)
                report["orphan_sessions_deleted"].append(sid)
                LOG.info("%s unowned session %s", "would delete" if dry_run else "deleted", sid)
                continue
        # 3. Raw video after the retention period; derived results stay.
        done = processed_at(session_dir)
        if uploaded.get(sid) is not None:
            # Processing ends within the hour; later rewrites of result files must not extend the clock.
            ceiling = uploaded[sid] + timedelta(days=1)
            done = min(done, ceiling) if done is not None else ceiling
        if done is None or done >= raw_cutoff:
            continue
        paths = raw_video_paths(settings, sid)
        if not paths:
            continue
        entry = {"session_id": sid, "processed_at": done.isoformat(), "files": len(paths),
                 "bytes": sum(_size(p) for p in paths)}
        if not dry_run:
            for path in paths:
                _remove(path)
            gemx = gemx_output_dir(sid)
            if gemx is not None:
                _prune_empty_dirs(gemx)
            marker = {
                "raw_video_deleted_at": now.isoformat(),
                "raw_video_retention_days": raw_days,
                "files": entry["files"],
            }
            (session_dir / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        report["raw_video_deleted"].append(entry)
        LOG.info("%s raw video of session %s (%d files, %d bytes, processed %s)",
                 "would delete" if dry_run else "deleted", sid, entry["files"], entry["bytes"], entry["processed_at"])

    # 4. GEM-X output folders whose session folder no longer exists.
    gemx_root = _existing_dir(gemx_output_root())
    if gemx_root is not None:
        for item in sorted(p for p in gemx_root.iterdir() if p.is_dir()):
            sid = item.name
            if sid == demo or (sessions_root / sid).is_dir():
                continue
            stamp = _mtime(item)
            if stamp is not None and stamp < raw_cutoff:
                if not dry_run:
                    shutil.rmtree(item, ignore_errors=True)
                report["orphan_gemx_deleted"].append(sid)
                LOG.info("%s GEM-X output without a session: %s", "would delete" if dry_run else "deleted", sid)
        # <session>_gemx.log files beside the output folders.
        for item in sorted(p for p in gemx_root.glob("*" + GEMX_LOG_SUFFIX) if p.is_file()):
            sid = item.name[: -len(GEMX_LOG_SUFFIX)]
            stamp = _mtime(item)
            if sid == demo or stamp is None or stamp >= raw_cutoff:
                continue
            if not dry_run:
                item.unlink(missing_ok=True)
            report["gemx_logs_deleted"].append(sid)
            LOG.info("%s GEM-X log of session %s", "would delete" if dry_run else "deleted", sid)

    # 5. Neck IK files older builds left outside the session folder (session/ik/ has a copy).
    neck_root = _existing_dir(neck_output_root())
    if neck_root is not None:
        for item in sorted(p for p in neck_root.iterdir() if p.is_dir()):
            sid = item.name
            if sid == demo:
                continue
            stamp = _mtime(item)
            if (sessions_root / sid).is_dir() and (stamp is None or stamp >= raw_cutoff):
                continue
            if not dry_run:
                shutil.rmtree(item, ignore_errors=True)
            report["neck_ik_deleted"].append(sid)
            LOG.info("%s neck IK files of session %s", "would delete" if dry_run else "deleted", sid)

    # 6. Old usage-log rows.
    if dry_run:
        report["events_removed"] = _count_old_events(settings, inactive_cutoff)
    else:
        report["events_removed"] = scrub_events(settings, older_than=inactive_cutoff)
    report["legacy_events_removed"] = _scrub_legacy_events(settings, inactive_cutoff, dry_run=dry_run)

    # 7. Question counters of an earlier day (they include visitor IPs).
    report["question_counts_cleared"] = clear_old_question_counts(settings.product_dir, dry_run=dry_run, now=now)

    if not dry_run:
        _append_log(settings, report)
    return report


def _existing_dir(root: Path) -> Path | None:
    try:
        return root if root.is_dir() else None
    except OSError:
        return None


def _count_old_events(settings: Settings, cutoff: datetime) -> int:
    path = settings.product_dir / "events.jsonl"
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ts = _utc(json.loads(line).get("ts"))
        except (json.JSONDecodeError, AttributeError):
            continue
        if ts is not None and ts < cutoff:
            count += 1
    return count


def _scrub_legacy_events(settings: Settings, cutoff: datetime, *, dry_run: bool) -> int:
    """Drop web_api.py's usage-log rows (stamped received_at) older than the cutoff."""
    path = settings.data_dir / LEGACY_EVENTS
    if not path.is_file():
        return 0
    kept: list[str] = []
    removed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ts = _utc(json.loads(line).get("received_at"))
        except (json.JSONDecodeError, AttributeError):
            ts = None
        if ts is not None and ts < cutoff:
            removed += 1
        else:
            kept.append(line)
    if removed and not dry_run:
        if kept:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(k + "\n" for k in kept), encoding="utf-8")
            tmp.replace(path)
        else:
            path.unlink(missing_ok=True)
    return removed


def _append_log(settings: Settings, report: dict[str, Any]) -> None:
    settings.product_dir.mkdir(parents=True, exist_ok=True)
    with (settings.product_dir / "retention.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, default=str) + "\n")


def summary_line(report: dict[str, Any]) -> str:
    verb = "would delete" if report["dry_run"] else "deleted"
    return (
        f"retention {verb}: {len(report['raw_video_deleted'])} raw videos, "
        f"{len(report['accounts_deleted'])} inactive accounts "
        f"({len(report['accounts_skipped'])} skipped), "
        f"{len(report['orphan_sessions_deleted'])} unowned sessions, "
        f"{len(report['orphan_gemx_deleted'])} orphan GEM-X folders, "
        f"{len(report['neck_ik_deleted'])} neck IK folders, "
        f"{len(report['gemx_logs_deleted'])} GEM-X logs, "
        f"{report['events_removed']} old usage events "
        f"(+{report['legacy_events_removed']} legacy), "
        f"{report['question_counts_cleared']} stale question counters"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce CeRebel data retention.")
    parser.add_argument("--dry-run", action="store_true", help="only list what would be deleted")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    report = run(load_settings(), dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(summary_line(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
