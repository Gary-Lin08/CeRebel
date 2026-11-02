"""Developer dashboard (/admin): every account with its sign-ins, visits, swings, feedback, coach
questions and payments.

The page is public; its data is not. Every /api/admin/* call needs both:
  1. a signed-in account whose email is listed in CEREBEL_ADMIN_EMAILS (comma-separated), and
  2. an admin session, opened with CEREBEL_ADMIN_PASSWORD on top of that sign-in.
Admin sessions are random tokens kept in memory for CEREBEL_ADMIN_SESSION_HOURS (default 12; a
restart signs admins out). Signing out of the account, or a password reset, ends admin access too.
With either variable unset, /admin and every /api/admin/* route answer 404.

Admin sign-ins, video playback and reset links go to admin_audit.jsonl (who, when, which account
or swing), as the privacy policy promises.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from motion_agent.app import activity, billing, questions, rate_limit, site_traffic
from motion_agent.app.accounts import create_password_reset, list_accounts, user_from_token
from motion_agent.app.config import Settings
from motion_agent.app.file_lock import locked
from motion_agent.app.library import all_captures

SESSION_TTL_S = float(os.environ.get("CEREBEL_ADMIN_SESSION_HOURS") or 12) * 3600
RESET_LINK_TTL = timedelta(hours=24)
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESS_LOCK = threading.Lock()
_AUDIT_LOCK = threading.Lock()
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CUSTOMER = re.compile(r"^cus_[A-Za-z0-9]+$")
NOT_FOUND = (404, {"error": True, "message": "not found"})
LOGIN_KINDS = {"signup", "login", "password_reset", "password_changed", "password_reset_requested"}
FEEDBACK_KINDS = {"client_trust_yes", "client_trust_no", "feedback"}


def admin_emails() -> set[str]:
    raw = os.environ.get("CEREBEL_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _password() -> str:
    return (os.environ.get("CEREBEL_ADMIN_PASSWORD") or "").strip()


def enabled() -> bool:
    return bool(admin_emails() and _password())


# ---------------------------------------------------------------- sign-in


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user(settings: Settings, headers: Any) -> dict[str, Any] | None:
    from motion_agent.app.product_api import token_from_headers

    return user_from_token(settings.product_dir, token_from_headers(headers))


def open_session(settings: Settings, headers: Any, body: dict[str, Any], ip: str) -> tuple[int, dict[str, Any]]:
    user = _user(settings, headers)
    if not user:
        return 401, {"error": True, "message": "请先用管理员邮箱登录。"}
    keys = ("u:" + str(user["user_id"]), "ip:" + ip)
    if any(rate_limit.ADMIN_FAILS.blocked(k) for k in keys):
        return 429, {"error": True, "message": "尝试次数太多，请 15 分钟后再试。"}
    email = str(user.get("email") or "").lower()
    is_admin = email in admin_emails()
    password_ok = hmac.compare_digest(str(body.get("password") or "").encode("utf-8"), _password().encode("utf-8"))
    if not (is_admin and password_ok):
        for k in keys:
            rate_limit.ADMIN_FAILS.hit(k)
        # A non-admin account is logged by its random id only, not its email.
        actor = email if is_admin else "user:" + str(user["user_id"])
        audit(settings, actor, "admin_denied", reason="password" if is_admin else "not_admin")
        if not is_admin:
            return 403, {"error": True, "message": "这个账号没有后台权限。"}
        return 401, {"error": True, "message": "后台密码不对。"}
    for k in keys:
        rate_limit.ADMIN_FAILS.clear(k)
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _SESS_LOCK:
        for stale in [k for k, v in _SESSIONS.items() if v["expires"] <= now]:
            _SESSIONS.pop(stale, None)
        _SESSIONS[_digest(token)] = {"user_id": user["user_id"], "email": email, "expires": now + SESSION_TTL_S}
    audit(settings, email, "admin_login")
    return 200, {
        "ok": True,
        "admin_token": token,
        "email": email,
        "expires_at": datetime.fromtimestamp(now + SESSION_TTL_S, timezone.utc).isoformat(),
    }


def _session(settings: Settings, headers: Any) -> dict[str, Any] | None:
    token = str(headers.get("X-Admin-Token") or "").strip()
    if not token:
        return None
    key = _digest(token)
    with _SESS_LOCK:
        sess = _SESSIONS.get(key)
        if sess and sess["expires"] <= time.time():
            _SESSIONS.pop(key, None)
            sess = None
    if not sess or sess["email"] not in admin_emails():
        return None
    # The account sign-in must still be valid and belong to the same admin.
    user = _user(settings, headers)
    if not user or user.get("user_id") != sess["user_id"]:
        return None
    return {**sess, "key": key}


# ---------------------------------------------------------------- routing


def handle(
    settings: Settings, method: str, path: str, query: dict[str, list[str]], headers: Any,
    body: dict[str, Any], ip: str,
) -> tuple[int, dict[str, Any]]:
    if not enabled():
        return NOT_FOUND
    if method == "POST" and path == "/api/admin/session":
        return open_session(settings, headers, body, ip)
    sess = _session(settings, headers)
    if sess is None:
        return 401, {"error": True, "relogin": True, "message": "后台登录已过期，请重新登录。"}

    def arg(name: str) -> str:
        return str((query.get(name) or [""])[0]).strip()

    if method == "GET":
        if path == "/api/admin/live":
            return 200, live(settings)
        if path == "/api/admin/traffic":
            return 200, traffic(settings, arg("days"))
        if path == "/api/admin/backers":
            return 200, backer_overview(settings)
        if path == "/api/admin/overview":
            return 200, overview(settings)
        if path == "/api/admin/users":
            return 200, {"ok": True, "users": users(settings)}
        if path == "/api/admin/user":
            return user_detail(settings, arg("user_id"))
        if path == "/api/admin/payments":
            return payments(settings, arg("user_id"))
        if path == "/api/admin/feedback":
            return 200, {"ok": True, "feedback": all_feedback(settings)}
        if path == "/api/admin/questions":
            return 200, {"ok": True, "questions": all_questions(settings)}
        if path == "/api/admin/audit":
            return 200, {"ok": True, "rows": audit_rows(settings)}
        if path == "/api/admin/video":
            return video(settings, sess, arg("session_id"))
    if method == "POST":
        if path == "/api/admin/logout":
            with _SESS_LOCK:
                _SESSIONS.pop(sess["key"], None)
            return 200, {"ok": True}
        if path == "/api/admin/reset-link":
            return make_reset_link(settings, sess, str(body.get("user_id") or ""))
        if path == "/api/admin/backers/remove":
            return remove_backer(settings, sess, str(body.get("id") or ""))
        if path == "/api/admin/backers/exported":
            audit(settings, sess["email"], "backers_export", rows=int(body.get("rows") or 0))
            return 200, {"ok": True}
    return NOT_FOUND


# ---------------------------------------------------------------- data


def _utc(text: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _events(settings: Settings) -> list[dict[str, Any]]:
    path = settings.product_dir / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _newest_first(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The log is appended in time order, but sort anyway: rows from other processes can interleave."""
    return sorted(events, key=lambda e: str(e.get("ts") or ""), reverse=True)


def _emails(accounts: list[dict[str, Any]]) -> dict[str, str]:
    return {str(a.get("user_id")): str(a.get("email") or "") for a in accounts if a.get("user_id")}


def _last_seen(account: dict[str, Any], visits: list[dict[str, Any]] | None) -> str | None:
    stamps = [s for s in [account.get("last_active_at"), *[v.get("end") for v in visits or []]] if _utc(s)]
    return max(stamps, key=lambda s: _utc(s)) if stamps else None


def _subscription(settings: Settings, user_id: str, row: dict[str, Any] | None = None) -> dict[str, Any]:
    row = dict(row if row is not None else billing.subscription_for(settings.product_dir, user_id))
    return {
        "pro": billing.is_pro(settings.product_dir, user_id),
        "status": row.get("status"),
        "plan": row.get("plan"),
        "current_period_end": row.get("current_period_end"),
        "cancel_at_period_end": bool(row.get("cancel_at_period_end")),
        "customer": row.get("customer"),
        "updated_at": row.get("updated_at"),
    }


def overview(settings: Settings) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    accounts = list_accounts(settings.product_dir)
    captures = all_captures(settings.product_dir)
    events = _events(settings)
    visits = activity.all_visits(settings.product_dir)
    bill = billing._read(settings.product_dir)  # noqa: SLF001 - read-only view of the same file
    emails = _emails(accounts)

    created = [_utc(a.get("created_at")) for a in accounts]
    seen = [_utc(_last_seen(a, visits.get(str(a.get("user_id"))))) for a in accounts]

    def since(stamps: list[datetime | None], days: int) -> int:
        cutoff = now - timedelta(days=days)
        return sum(1 for s in stamps if s and s >= cutoff)

    day_counts = Counter(c.date().isoformat() for c in created if c)
    signups_by_day = [
        {"day": d, "count": day_counts.get(d, 0)}
        for d in ((now - timedelta(days=i)).date().isoformat() for i in range(29, -1, -1))
    ]
    pro_users = [uid for uid in bill.get("users", {}) if billing.is_pro(settings.product_dir, uid)]
    kinds = Counter(str(e.get("kind")) for e in events)
    month_ago = now - timedelta(days=30)
    recent = [{**e, "email": emails.get(str(e.get("user_id") or ""))} for e in _newest_first(events[-150:])]
    return {
        "ok": True,
        "now": now.isoformat(),
        "accounts": {
            "total": len(accounts),
            "new_7d": since(created, 7),
            "new_30d": since(created, 30),
            "active_1d": since(seen, 1),
            "active_7d": since(seen, 7),
            "active_30d": since(seen, 30),
            "video_consent": sum(1 for a in accounts if a.get("video_consent_at")),
        },
        "signups_by_day": signups_by_day,
        "billing": {
            "pro": len(pro_users),
            "plans": dict(Counter(str(bill["users"][u].get("plan") or "unknown") for u in pro_users)),
            "founding_used": int(bill.get("founding_used") or 0),
            "checkouts_started": kinds.get("checkout_created", 0),
            "paywall_hits": kinds.get("paywall_hit", 0),
        },
        "uploads": {
            "total": len(captures),
            "by_status": dict(Counter(str(r.get("status") or "unknown") for r in captures)),
            "saved_events": kinds.get("upload_saved", 0),
            "pipeline_errors": kinds.get("pipeline_error", 0),
            "failure_rate": round(kinds.get("pipeline_error", 0) / kinds["upload_saved"], 3)
            if kinds.get("upload_saved") else 0.0,
        },
        "feedback": {
            "trust_yes": kinds.get("client_trust_yes", 0),
            "trust_no": kinds.get("client_trust_no", 0),
            "written": kinds.get("feedback", 0),
        },
        "questions_30d": sum(
            1 for e in events if e.get("kind") == "chat_question" and (_utc(e.get("ts")) or now) >= month_ago
        ),
        "event_counts": dict(kinds),
        "recent_events": recent,
    }


# ---------------------------------------------------------------- live, traffic, backers


PAYMENT_DONE = "billing_checkout.session.completed"
# Product events worth showing in the live feed; everything else would drown it.
FEED_KINDS = {"signup", "login", "upload_saved", "capture_finalized", "checkout_created", "paywall_hit", PAYMENT_DONE}
TRAFFIC_DAYS = {"7": 7, "30": 30, "90": 90}


def _product_tail(settings: Settings) -> list[dict[str, Any]]:
    return site_traffic.tail_jsonl(settings.product_dir / "events.jsonl")


def live(settings: Settings) -> dict[str, Any]:
    """What the realtime page polls every 5 seconds, so it avoids reading whole files where it can."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    emails = _emails(list_accounts(settings.product_dir))
    who = site_traffic.online(now=now)
    for row in who:
        row["email"] = emails.get(str(row.get("user_id") or ""))

    product = _product_tail(settings)
    backers = site_traffic.backers(settings.product_dir)
    feed: list[dict[str, Any]] = [
        {
            "ts": r.get("ts"), "type": r.get("event"), "visitor": r.get("visitor"), "site": r.get("site"),
            "path": r.get("path"), "label": r.get("label"), "source": site_traffic.source_of(r),
            "country": r.get("country"), "email": emails.get(str(r.get("user_id") or "")),
        }
        for r in site_traffic.rows(settings.product_dir)[-60:]
    ]
    feed += [
        {"ts": e.get("ts"), "type": e.get("kind"), "email": emails.get(str(e.get("user_id") or "")), "plan": e.get("plan")}
        for e in product if e.get("kind") in FEED_KINDS
    ]
    feed += [
        {"ts": b.get("created_at"), "type": "backer", "email": b.get("email"), "source": b.get("utm_source") or b.get("ref"),
         "country": b.get("country")}
        for b in backers[:60]
    ]
    ours = admin_emails()
    feed = [r for r in feed if str(r.get("email") or "").lower() not in ours]
    feed.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    bill = billing._read(settings.product_dir)  # noqa: SLF001 - read-only view of the same file

    def is_today(stamp: Any) -> bool:
        return str(stamp or "").startswith(today)

    return {
        "ok": True,
        "now": now.isoformat(),
        "online": who,
        "per_minute": site_traffic.per_minute(now=now),
        "today": {
            **site_traffic.today_counts(settings.product_dir, now=now),
            "signups": sum(1 for e in product if e.get("kind") == "signup" and is_today(e.get("ts"))),
            "backers": sum(1 for b in backers if is_today(b.get("created_at"))),
            "payments": sum(1 for e in product if e.get("kind") == PAYMENT_DONE and is_today(e.get("ts"))),
        },
        "backers_total": len(backers),
        "backer_store_error": site_traffic.backer_store_error(),
        "latest_backer": backers[0].get("email") if backers else None,
        "pro_total": sum(1 for uid in bill.get("users", {}) if billing.is_pro(settings.product_dir, uid)),
        "feed": feed[:50],
    }


def _running_total(stamps: list[datetime | None], labels: list[str]) -> list[int]:
    """How many stamps fall on or before each day."""
    days = sorted(s.date().isoformat() for s in stamps if s)
    out, seen = [], 0
    for label in labels:
        while seen < len(days) and days[seen] <= label:
            seen += 1
        out.append(seen)
    return out


def traffic(settings: Settings, days_arg: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    days = TRAFFIC_DAYS.get(days_arg, 30)
    out = site_traffic.traffic(settings.product_dir, days, now=now)
    labels = out["labels"]
    accounts = list_accounts(settings.product_dir)
    events = _events(settings)
    backers = site_traffic.backers(settings.product_dir)
    created = [_utc(a.get("created_at")) for a in accounts]
    counts = Counter(c.date().isoformat() for c in created if c)
    start = labels[0]

    def users_with(kind: str) -> int:
        return len({e.get("user_id") for e in events
                    if e.get("kind") == kind and e.get("user_id") and str(e.get("ts") or "")[:10] >= start})

    paid_at = _first_payments(events)
    out.update({
        "ok": True,
        "signups": [counts.get(d, 0) for d in labels],
        "backers_running": _running_total([_utc(b.get("created_at")) for b in backers], labels),
        "paying_running": _running_total([_utc(t) for t in paid_at.values()], labels),
        "funnel": [
            {"name": "访客", "count": out["total_visitors"]},
            {"name": "点过按钮", "count": out["clickers"]},
            {"name": "注册", "count": sum(1 for c in created if c and c.date().isoformat() >= start)},
            {"name": "上传过挥杆", "count": users_with("upload_saved")},
            {"name": "付费", "count": sum(1 for t in paid_at.values() if str(t)[:10] >= start)},
        ],
        "backers_in_window": sum(1 for b in backers if str(b.get("created_at") or "")[:10] >= start),
    })
    return out


def _first_payments(events: list[dict[str, Any]]) -> dict[str, str]:
    """Each account's first completed checkout."""
    first: dict[str, str] = {}
    for e in events:
        uid, ts = str(e.get("user_id") or ""), str(e.get("ts") or "")
        if e.get("kind") == PAYMENT_DONE and uid and (uid not in first or ts < first[uid]):
            first[uid] = ts
    return first


def backer_overview(settings: Settings) -> dict[str, Any]:
    """The presale list (email only, no payment) and the paying Pro subscribers, side by side."""
    accounts = list_accounts(settings.product_dir)
    by_email = {str(a.get("email") or "").lower() for a in accounts}
    emails = _emails(accounts)
    paid_at = _first_payments(_events(settings))
    bill = billing._read(settings.product_dir).get("users", {})  # noqa: SLF001
    paying = []
    for uid, row in bill.items():
        sub = _subscription(settings, uid, row)
        if not (sub["pro"] or sub["status"]):
            continue
        paying.append({
            "user_id": uid,
            "email": emails.get(uid),
            "plan": sub["plan"],
            "status": sub["status"],
            "pro": sub["pro"],
            "since": paid_at.get(uid) or sub["updated_at"],
            "current_period_end": sub["current_period_end"],
            "cancel_at_period_end": sub["cancel_at_period_end"],
        })
    paying.sort(key=lambda r: str(r.get("since") or ""), reverse=True)
    backers = [
        {**b, "has_account": str(b.get("email") or "") in by_email}
        for b in site_traffic.backers(settings.product_dir)
    ]
    return {"ok": True, "backers": backers, "backer_store_error": site_traffic.backer_store_error(), "paying": paying, "founding_used": int(billing._read(settings.product_dir).get("founding_used") or 0)}  # noqa: SLF001


def remove_backer(settings: Settings, sess: dict[str, Any], backer_id: str) -> tuple[int, dict[str, Any]]:
    if not _ID.match(backer_id):
        return 400, {"error": True, "message": "bad id"}
    try:
        email = site_traffic.remove_backer(settings.product_dir, backer_id)
    except RuntimeError:
        return 502, {"error": True, "message": "连不上 Supabase，没有移除，请稍后再试。"}
    if email is None:
        return 404, {"error": True, "message": "这条记录已不存在。"}
    # The log keeps the row's random id, not the address of someone who asked to be removed.
    audit(settings, sess["email"], "backer_removed", backer_id=backer_id)
    return 200, {"ok": True}


def users(settings: Settings) -> list[dict[str, Any]]:
    accounts = list_accounts(settings.product_dir)
    captures = all_captures(settings.product_dir)
    events = _events(settings)
    visits = activity.all_visits(settings.product_dir)
    bill_users = billing._read(settings.product_dir).get("users", {})  # noqa: SLF001
    by_user: dict[str, Counter] = {}
    for e in events:
        uid = str(e.get("user_id") or "")
        if uid:
            by_user.setdefault(uid, Counter())[str(e.get("kind"))] += 1
    out = []
    for a in accounts:
        uid = str(a.get("user_id") or "")
        mine = [r for r in captures if r.get("user_id") == uid]
        kinds = by_user.get(uid, Counter())
        v = visits.get(uid) or []
        sub = _subscription(settings, uid, bill_users.get(uid) or {})
        out.append({
            "user_id": uid,
            "email": a.get("email"),
            "created_at": a.get("created_at"),
            "last_login_at": a.get("last_login_at"),
            "last_seen_at": _last_seen(a, v),
            "logins": kinds.get("login", 0),
            "visits": len(v),
            "total_seconds": sum(int(x.get("seconds") or 0) for x in v),
            "uploads": len(mine),
            "swings": sum(1 for r in mine if not r.get("deleted")),
            "ready": sum(1 for r in mine if r.get("status") == "ready" and not r.get("deleted")),
            "failed": sum(1 for r in mine if r.get("status") in {"error", "rejected", "no_swing"}),
            "feedback": sum(kinds.get(k, 0) for k in FEEDBACK_KINDS),
            "questions": kinds.get("chat_question", 0),
            "coach_code": a.get("coach_code"),
            "video_consent_at": a.get("video_consent_at"),
            "pro": sub["pro"],
            "plan": sub["plan"] if sub["pro"] else "free",
            "subscription_status": sub["status"],
        })
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return out


def _video_files(settings: Settings, session_id: str) -> list[tuple[str, str, Path]]:
    """(kind, path under /media/<sid>/, file): the playback copy, then the original upload."""
    base = settings.data_dir / "sessions" / session_id
    out: list[tuple[str, str, Path]] = []
    for candidate in (base / "display" / "session.mp4", base / "ik" / "session.mp4"):
        if candidate.is_file():
            out.append(("playback", "session.mp4", candidate))
            break
    for original in sorted((base / "upload").glob("source_video.*")) if (base / "upload").is_dir() else []:
        if original.is_file():
            out.append(("original", "upload/" + original.name, original))
            break
    return out


def _capture_detail(settings: Settings, row: dict[str, Any]) -> dict[str, Any]:
    from motion_agent.app.consent import raw_video_retention_days
    from motion_agent.app.product_api import read_progress

    sid = str(row.get("session_id") or "")
    prog = (read_progress(settings, sid) or {}) if _ID.match(sid) else {}
    files = _video_files(settings, sid) if _ID.match(sid) else []
    created = _utc(row.get("created_at"))
    expires = created + timedelta(days=raw_video_retention_days() + 1) if created else None
    return {
        **row,
        "stage": prog.get("stage"),
        "progress_message": prog.get("message"),
        "error": prog.get("error"),
        "failed_at": prog.get("failed_at"),
        "bytes": prog.get("bytes") or (files[-1][2].stat().st_size if files else None),
        "has_video": bool(files) and not row.get("deleted"),
        "raw_video_deleted": (settings.data_dir / "sessions" / sid / "retention.json").is_file() if sid else False,
        "raw_video_expires_at": expires.isoformat() if expires else None,
    }


def _feedback_row(e: dict[str, Any], emails: dict[str, str]) -> dict[str, Any]:
    kind = str(e.get("kind"))
    rating = {"client_trust_yes": "yes", "client_trust_no": "no"}.get(kind, e.get("rating"))
    return {
        "ts": e.get("ts"),
        "user_id": e.get("user_id"),
        "email": emails.get(str(e.get("user_id") or "")),
        "session_id": e.get("session_id") or None,
        "rating": rating,
        "text": e.get("text") if kind == "feedback" else None,
    }


def user_detail(settings: Settings, user_id: str) -> tuple[int, dict[str, Any]]:
    accounts = list_accounts(settings.product_dir)
    account = next((a for a in accounts if a.get("user_id") == user_id), None) if _ID.match(user_id) else None
    if account is None:
        return 404, {"error": True, "message": "没有这个用户（可能已删除）。"}
    emails = _emails(accounts)
    rows = [r for r in all_captures(settings.product_dir) if r.get("user_id") == user_id]
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    sids = {str(r.get("session_id")) for r in rows if r.get("session_id")}
    events = _newest_first([
        e for e in _events(settings)
        if e.get("user_id") == user_id or (not e.get("user_id") and e.get("session_id") in sids)
    ])
    visits = activity.visits_for(settings.product_dir, user_id)
    return 200, {
        "ok": True,
        "account": account,
        "subscription": _subscription(settings, user_id),
        "logins": [e for e in events if e.get("kind") in LOGIN_KINDS],
        "visits": visits,
        "total_seconds": sum(int(v.get("seconds") or 0) for v in visits),
        "captures": [_capture_detail(settings, r) for r in rows],
        "feedback": [_feedback_row(e, emails) for e in events if e.get("kind") in FEEDBACK_KINDS],
        "questions": questions.rows(settings.product_dir, user_id=user_id, limit=500),
        "question_retention_days": questions.retention_days(),
        "events": events[:300],
    }


def all_feedback(settings: Settings) -> list[dict[str, Any]]:
    emails = _emails(list_accounts(settings.product_dir))
    return [_feedback_row(e, emails) for e in _newest_first(_events(settings)) if e.get("kind") in FEEDBACK_KINDS][:1000]


def all_questions(settings: Settings) -> list[dict[str, Any]]:
    emails = _emails(list_accounts(settings.product_dir))
    return [{**q, "email": emails.get(str(q.get("user_id") or ""))}
            for q in questions.rows(settings.product_dir, limit=1000)]


def payments(settings: Settings, user_id: str) -> tuple[int, dict[str, Any]]:
    """Subscription from billing.json plus the customer's invoices, read live from Stripe."""
    if not _ID.match(user_id):
        return 400, {"error": True, "message": "bad user id"}
    sub = _subscription(settings, user_id)
    customer = str(sub.get("customer") or "")
    out: dict[str, Any] = {"ok": True, "subscription": sub, "invoices": [], "dashboard_url": None, "note": None}
    if not _CUSTOMER.match(customer):
        out["note"] = "这个用户没有在 Stripe 付过款。"
        return 200, out
    key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    test = "test/" if key.startswith(("sk_test", "rk_test")) else ""
    out["dashboard_url"] = f"https://dashboard.stripe.com/{test}customers/{customer}"
    if not key:
        out["note"] = "服务器没有配置 STRIPE_SECRET_KEY，读不到发票。"
        return 200, out
    query = urllib.parse.urlencode({"customer": customer, "limit": 24})
    try:
        data = billing._stripe_request("GET", "invoices?" + query)  # noqa: SLF001 - same Stripe client
    except billing.BillingError as exc:
        out["note"] = "读取 Stripe 发票失败：" + exc.message
        return 200, out
    out["invoices"] = [
        {
            "id": inv.get("id"),
            "number": inv.get("number"),
            "created": inv.get("created"),
            "status": inv.get("status"),
            "amount_paid": inv.get("amount_paid"),
            "amount_due": inv.get("amount_due"),
            "currency": inv.get("currency"),
            "billing_reason": inv.get("billing_reason"),
            "hosted_invoice_url": inv.get("hosted_invoice_url"),
        }
        for inv in data.get("data") or []
        if isinstance(inv, dict)
    ]
    return 200, out


def video(settings: Settings, sess: dict[str, Any], session_id: str) -> tuple[int, dict[str, Any]]:
    from motion_agent.app.library import owner_of
    from motion_agent.app.media_urls import sign_url
    from motion_agent.app.product_api import media_session_live, probe_video

    if not _ID.match(session_id):
        return 400, {"error": True, "message": "bad session id"}
    if not media_session_live(settings, session_id):
        return 404, {"error": True, "message": "这个挥杆已被删除。"}
    files = _video_files(settings, session_id)
    if not files:
        return 404, {"error": True, "message": "视频文件已不在服务器上（按保留期自动删除，或上传时被拒）。"}
    audit(settings, sess["email"], "video_view", user_id=owner_of(settings.product_dir, session_id), session_id=session_id)
    return 200, {
        "ok": True,
        "session_id": session_id,
        "files": [
            {
                "kind": kind,
                "name": path.name,
                "bytes": path.stat().st_size,
                "url": sign_url(settings.product_dir, f"/media/{session_id}/{rel}"),
            }
            for kind, rel, path in files
        ],
        "meta": probe_video(files[-1][2]),
    }


def make_reset_link(settings: Settings, sess: dict[str, Any], user_id: str) -> tuple[int, dict[str, Any]]:
    from motion_agent.app.product_api import reset_link

    account = next((a for a in list_accounts(settings.product_dir) if a.get("user_id") == user_id), None)
    if account is None:
        return 404, {"error": True, "message": "没有这个用户。"}
    token = create_password_reset(settings.product_dir, str(account["email"]), ttl=RESET_LINK_TTL)
    if not token:
        return 404, {"error": True, "message": "没有这个用户。"}
    audit(settings, sess["email"], "reset_link", user_id=user_id)
    return 200, {
        "ok": True,
        "email": account["email"],
        "link": reset_link(token),
        "expires_hours": int(RESET_LINK_TTL.total_seconds() // 3600),
    }


# ---------------------------------------------------------------- audit log


def _audit_path(settings: Settings) -> Path:
    settings.product_dir.mkdir(parents=True, exist_ok=True)
    return settings.product_dir / "admin_audit.jsonl"


def audit(settings: Settings, actor: str, action: str, **fields: Any) -> None:
    path = _audit_path(settings)
    row = {"ts": datetime.now(timezone.utc).isoformat(), "admin": actor, "action": action, **fields}
    with locked(path, _AUDIT_LOCK), path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


def audit_rows(settings: Settings, limit: int = 500) -> list[dict[str, Any]]:
    path = _audit_path(settings)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    out.reverse()
    return out[:limit]


def prune_audit(settings: Settings, *, older_than: datetime, dry_run: bool = False) -> int:
    path = _audit_path(settings)
    if not path.is_file():
        return 0
    with locked(path, _AUDIT_LOCK):
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        kept = []
        for line in lines:
            try:
                ts = _utc(json.loads(line).get("ts"))
            except (json.JSONDecodeError, AttributeError):
                ts = None
            if ts is not None and ts >= older_than:
                kept.append(line)
        removed = len(lines) - len(kept)
        if removed and not dry_run:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(k + "\n" for k in kept), encoding="utf-8")
            tmp.replace(path)
    return removed
