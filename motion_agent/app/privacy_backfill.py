"""One-off clean-up of data that builds before the 2026-10 privacy fixes left behind.

    python3 -m motion_agent.app.privacy_backfill            # report only, changes nothing
    python3 -m motion_agent.app.privacy_backfill --apply    # do it

1. Deleted accounts. Older builds removed the account and its session folders but kept its
   GEM-X output folder, neck IK files, library rows and usage-log rows. An account counts as
   deleted when its user id is in library.json or events.jsonl but not in accounts.json. For
   each one this does what remove_account does now: purge its captures (session folder, GEM-X
   and neck outputs), its library rows, its local billing row and its usage events. Its captures
   are its library rows plus its upload_saved events; the demo and captures of live accounts are
   never touched. Old account_deleted rows stay, without the user id, like the ones written now.
2. hpe_results.pt. Every file under CEREBEL_GEMX_OUTPUT_ROOT is stripped with hpe_privacy (head
   shape zeroed, net_outputs dropped). Files written in the last --min-age-minutes are skipped,
   since their capture may still be processing. Needs torch, so run it with the product Python.

Prints ids and counts only. --apply also appends the report to <data>/product/privacy_backfill.jsonl.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from motion_agent.app.accounts import delete_account, list_accounts, tokens_for_user
from motion_agent.app.config import Settings, load_settings
from motion_agent.app.file_lock import locked
from motion_agent.app.library import all_captures, purge_user_rows

LOG = logging.getLogger("motion_agent.privacy_backfill")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _events(settings: Settings) -> list[dict[str, Any]]:
    path = settings.product_dir / "events.jsonl"
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def deleted_accounts(settings: Settings) -> dict[str, list[str]]:
    """{user_id: [session ids]} for accounts that are gone but still have rows or captures."""
    live = {str(a["user_id"]) for a in list_accounts(settings.product_dir) if a.get("user_id")}
    demo = os.environ.get("CEREBEL_DEMO_SESSION_ID", "").strip()
    captures = all_captures(settings.product_dir)
    live_sessions = {str(r["session_id"]) for r in captures if r.get("user_id") in live and r.get("session_id")}
    found: dict[str, set[str]] = {}

    def note(user_id: Any, session_id: Any) -> None:
        if not user_id or str(user_id) in live:
            return
        sessions = found.setdefault(str(user_id), set())
        sid = str(session_id or "")
        if sid and sid != demo and sid not in live_sessions:
            sessions.add(sid)

    for row in captures:
        note(row.get("user_id"), row.get("session_id"))
    for row in _events(settings):
        # Only an upload ties a session to its uploader; a question may be about the demo.
        note(row.get("user_id"), row.get("session_id") if row.get("kind") == "upload_saved" else None)
    return {uid: sorted(sids) for uid, sids in sorted(found.items())}


def _anonymize_deletion_rows(settings: Settings, user_ids: set[str]) -> int:
    """Keep old account_deleted rows (they count deletions) but drop the user id from them."""
    path = settings.product_dir / "events.jsonl"
    if not path.is_file() or not user_ids:
        return 0
    out: list[str] = []
    changed = 0
    # Same lock as record_event/scrub_events: chat_server may append while we rewrite.
    with locked(path):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            if isinstance(row, dict) and row.get("kind") == "account_deleted" and row.get("user_id") in user_ids:
                row.pop("user_id", None)
                line = json.dumps(row, default=str)
                changed += 1
            out.append(line)
        if changed:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(o + "\n" for o in out), encoding="utf-8")
            tmp.replace(path)
    return changed


def purge_deleted_accounts(settings: Settings, *, apply: bool) -> list[dict[str, Any]]:
    from motion_agent.app.product_api import (
        _forget_billing,
        gemx_output_dir,
        neck_output_dir,
        purge_session_dir,
        scrub_events,
    )

    report = []
    events = _events(settings)
    captures = all_captures(settings.product_dir)
    for user_id, sessions in deleted_accounts(settings).items():
        entry: dict[str, Any] = {
            "user_id": user_id,
            "session_ids": sessions,
            "session_dirs": sum((settings.data_dir / "sessions" / s).is_dir() for s in sessions),
            "gemx_dirs": sum(gemx_output_dir(s) is not None for s in sessions),
            "neck_dirs": sum(neck_output_dir(s) is not None for s in sessions),
            "library_rows": sum(r.get("user_id") == user_id for r in captures),
            "tokens": len(tokens_for_user(settings.product_dir, user_id)),
            "events": sum(r.get("user_id") == user_id or r.get("session_id") in sessions for r in events
                          if r.get("kind") != "account_deleted"),
            "deletion_rows_anonymized": sum(r.get("kind") == "account_deleted" and r.get("user_id") == user_id
                                            for r in events),
        }
        if apply:
            for sid in sessions:
                purge_session_dir(settings, sid)
            purge_user_rows(settings.product_dir, user_id)
            if entry["tokens"]:
                delete_account(settings.product_dir, user_id)
            _forget_billing(settings.product_dir, user_id)
            _anonymize_deletion_rows(settings, {user_id})
            entry["events"] = scrub_events(settings, user_id=user_id, session_ids=sessions)
        report.append(entry)
        LOG.info("%s leftovers of deleted account %s: %s", "removed" if apply else "would remove", user_id,
                 {k: v for k, v in entry.items() if k != "user_id"})
    return report


def _hpe_privacy():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import hpe_privacy

    return hpe_privacy


def _is_sanitized(path: Path) -> bool:
    import torch

    hpe_privacy = _hpe_privacy()
    pred = torch.load(path, map_location="cpu", weights_only=False)
    return isinstance(pred, dict) and pred.get(hpe_privacy.MARKER_KEY, {}).get("version") == hpe_privacy.MARKER["version"]


def sanitize_hpe_results(root: Path, *, apply: bool, min_age: timedelta,
                         now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    hpe_privacy = _hpe_privacy()
    report: dict[str, Any] = {"root": str(root), "sanitized": [], "already_clean": 0,
                              "skipped_recent": [], "failed": []}
    paths = sorted(root.rglob("hpe_results.pt")) if root.is_dir() else []
    if not paths:
        return report
    try:
        import torch  # noqa: F401
    except ImportError:
        report["failed"].append({"session_id": None, "error": f"torch not installed for {sys.executable}"})
        LOG.error("torch is not installed for %s; run this with the product (or GEM-X) Python", sys.executable)
        return report
    for path in paths:
        sid = path.parent.name
        try:
            if _is_sanitized(path):
                report["already_clean"] += 1
                continue
            written = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if written > now - min_age:
                report["skipped_recent"].append(sid)
                continue
            if apply:
                hpe_privacy.sanitize_file(path)
        except Exception as exc:  # noqa: BLE001 - report and continue with the next file
            report["failed"].append({"session_id": sid, "error": type(exc).__name__})
            LOG.error("hpe_results.pt of %s: %s", sid, exc)
            continue
        report["sanitized"].append(sid)
    return report


def run(settings: Settings, *, apply: bool = False, accounts: bool = True, hpe: bool = True,
        min_age: timedelta = timedelta(hours=1)) -> dict[str, Any]:
    from motion_agent.app.product_api import gemx_output_root

    report: dict[str, Any] = {"applied": apply, "now": datetime.now(timezone.utc).isoformat()}
    if accounts:
        report["deleted_accounts"] = purge_deleted_accounts(settings, apply=apply)
    if hpe:
        report["hpe_results"] = sanitize_hpe_results(gemx_output_root(), apply=apply, min_age=min_age)
    if apply:
        settings.product_dir.mkdir(parents=True, exist_ok=True)
        with (settings.product_dir / "privacy_backfill.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, default=str) + "\n")
    return report


def summary_line(report: dict[str, Any]) -> str:
    verb = "done" if report["applied"] else "would do"
    parts = []
    if "deleted_accounts" in report:
        rows = report["deleted_accounts"]
        parts.append(
            f"{len(rows)} deleted accounts: {sum(len(r['session_ids']) for r in rows)} captures "
            f"({sum(r['session_dirs'] for r in rows)} session, {sum(r['gemx_dirs'] for r in rows)} GEM-X, "
            f"{sum(r['neck_dirs'] for r in rows)} neck folders), {sum(r['library_rows'] for r in rows)} library rows, "
            f"{sum(r['events'] for r in rows)} usage events removed, "
            f"{sum(r['deletion_rows_anonymized'] for r in rows)} deletion rows anonymized"
        )
    if "hpe_results" in report:
        h = report["hpe_results"]
        parts.append(
            f"hpe_results.pt: {len(h['sanitized'])} sanitized, {h['already_clean']} already clean, "
            f"{len(h['skipped_recent'])} skipped (recent), {len(h['failed'])} failed"
        )
    return f"privacy backfill {verb}: " + "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-off CeRebel privacy clean-up (see module docstring).")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: report only)")
    parser.add_argument("--skip-accounts", action="store_true", help="leave deleted-account leftovers alone")
    parser.add_argument("--skip-hpe", action="store_true", help="leave hpe_results.pt files alone")
    parser.add_argument("--min-age-minutes", type=float, default=60.0,
                        help="skip hpe_results.pt written more recently than this (default 60)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    report = run(load_settings(), apply=args.apply, accounts=not args.skip_accounts, hpe=not args.skip_hpe,
                 min_age=timedelta(minutes=args.min_age_minutes))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(summary_line(report))
    failed = report.get("hpe_results", {}).get("failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
