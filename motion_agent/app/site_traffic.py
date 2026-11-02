"""Website visitors and the glasses presale list, for the admin dashboard.

Two public, unauthenticated routes feed this module:
  POST /api/site/ping     page views, clicks and a heartbeat from cerebel.tech and the Playground
  POST /api/site/backers  the "Become a backer" form on cerebel.tech

Who is online right now lives in memory, from the heartbeats; a restart empties it until the next
heartbeat, 30 seconds later. Page views and clicks go to data/product/site_events.jsonl and are
deleted after CEREBEL_SITE_EVENT_RETENTION_DAYS (default 90) by the retention job.

Stored for a visitor: a random browser ID, the page without its query string, the referring site's
hostname, UTM tags, country, time zone, device type and browser family. The IP address and the full
user agent are never written; the IP is only a key for the in-memory rate limit.

Presale sign-ups live in Supabase once CEREBEL_SUPABASE_URL, CEREBEL_SUPABASE_KEY and
CEREBEL_BACKERS_ADMIN_TOKEN are set, so a GPU server outage cannot lose one: cerebel.tech writes to the
join_backer_list function directly, and this module reads through admin_list_backers. Without those
variables they go to data/product/backers.json. Either way there is one row per email, kept until an
admin removes it. They are not tied to accounts, so deleting an account does not remove them; an
account's rows in site_events.jsonl do go with the account.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from motion_agent.app import rate_limit
from motion_agent.app.file_lock import locked

ONLINE_WINDOW_S = 90  # the page pings every 30 seconds while visible
VISIT_GAP_S = 30 * 60
MAX_PRESENCE = 5000
MINUTES_KEPT = 30
MAX_BACKERS = 20_000
EVENTS = {"page_view", "cta_click", "form_start", "heartbeat"}
SITES = {"main", "playground"}
OWN_HOSTS = {"cerebel.tech", "www.cerebel.tech", "playground.cerebel.tech"}
_VISITOR = re.compile(r"^[A-Za-z0-9-]{6,64}$")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_BOT = re.compile(r"bot|spider|crawl|slurp|headless|lighthouse|preview|monitor|curl|wget|python-|go-http", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_EVENTS_LOCK = threading.Lock()
_BACKERS_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_presence: dict[str, dict[str, Any]] = {}
_minutes: dict[int, set[str]] = {}
_cache: dict[str, Any] = {"key": None, "rows": []}
REMOTE_CACHE_S = 15.0  # the live page polls every 5 seconds
_remote: dict[str, Any] = {"at": None, "rows": [], "error": None}


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("CEREBEL_SITE_EVENT_RETENTION_DAYS") or 90))
    except ValueError:
        return 90


def _events_path(product_dir: Path) -> Path:
    product_dir.mkdir(parents=True, exist_ok=True)
    return product_dir / "site_events.jsonl"


def _backers_path(product_dir: Path) -> Path:
    product_dir.mkdir(parents=True, exist_ok=True)
    return product_dir / "backers.json"


def _utc(text: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- cleaning what the browser sends


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub(" ", value).strip()[:limit]


def _page(value: Any) -> str:
    """Path only: a query string can hold a reset token or a share link."""
    text = _text(value, 400)
    if not text:
        return "/"
    return (text.split("?")[0].split("#")[0] or "/")[:120]


def _ref_host(value: Any) -> str:
    """The referring site's hostname, or nothing for our own pages."""
    text = _text(value, 400)
    if not text:
        return ""
    try:
        host = (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""
    return "" if host in OWN_HOSTS else host[:80]


def _country(headers: Any) -> str:
    for name in ("CF-IPCountry", "X-Vercel-IP-Country"):
        code = str(headers.get(name) or "").strip().upper()
        if _COUNTRY.match(code) and code != "XX":
            return code
    return ""


def _device(ua: str) -> str:
    low = ua.lower()
    if "ipad" in low or "tablet" in low:
        return "tablet"
    if "mobi" in low or "iphone" in low or "android" in low:
        return "mobile"
    return "desktop"


def _browser(ua: str) -> str:
    low = ua.lower()
    for needle, name in (
        ("micromessenger", "WeChat"), ("edg/", "Edge"), ("opr/", "Opera"), ("firefox/", "Firefox"),
        ("crios/", "Chrome"), ("chrome/", "Chrome"), ("safari/", "Safari"),
    ):
        if needle in low:
            return name
    return "other"


# ---------------------------------------------------------------- public routes


def handle_ping(product_dir: Path, body: dict[str, Any], headers: Any, ip: str,
                user: dict[str, Any] | None = None, *, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    event = str(body.get("eventType") or "")
    visitor = str(body.get("visitor") or body.get("sessionId") or "")
    if event not in EVENTS or not _VISITOR.match(visitor):
        return 400, {"error": True, "message": "bad event"}
    if rate_limit.SITE_PING_PER_IP.blocked(ip):
        return 429, {"error": True, "message": "too many requests"}
    rate_limit.SITE_PING_PER_IP.hit(ip)
    ua = str(headers.get("User-Agent") or "")
    if not ua or _BOT.search(ua):
        return 200, {"ok": True}
    now = now or datetime.now(timezone.utc)
    site = str(body.get("site") or "")
    row = {
        "ts": now.isoformat(),
        "event": event,
        "visitor": visitor,
        "site": site if site in SITES else "main",
        "path": _page(body.get("path")),
        "ref": _ref_host(body.get("referrer")),
        "utm_source": _text(body.get("utmSource"), 60),
        "utm_medium": _text(body.get("utmMedium"), 60),
        "utm_campaign": _text(body.get("utmCampaign"), 80),
        "country": _country(headers),
        "tz": _text(body.get("tz"), 50),
        "device": _device(ua),
        "browser": _browser(ua),
        "user_id": (user or {}).get("user_id"),
    }
    _note_presence(row, now)
    if event != "heartbeat":
        row["target"] = _text(body.get("target"), 400).split("?")[0][:120]
        row["label"] = _text(body.get("label"), 80)
        if not rate_limit.SITE_WRITES.blocked("all"):
            rate_limit.SITE_WRITES.hit("all")
            path = _events_path(product_dir)
            with locked(path, _EVENTS_LOCK), path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 200, {"ok": True}


def handle_backer(product_dir: Path, body: dict[str, Any], headers: Any, ip: str,
                  *, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    email = _text(body.get("email"), 254).lower()
    if not _EMAIL.match(email):
        return 400, {"error": True, "message": "Enter a valid email address."}
    if rate_limit.BACKER_PER_IP.blocked(ip):
        return 429, {"error": True, "message": "Too many tries. Please try again later."}
    rate_limit.BACKER_PER_IP.hit(ip)
    if _remote_config():
        try:
            _rpc("join_backer_list", {"p": {k: body.get(k) for k in (
                "email", "name", "use", "list", "path", "referrer", "utmSource", "utmCampaign", "visitor",
            )}})
        except RuntimeError:
            return 503, {"error": True, "message": "Please try again later."}
        return 200, {"ok": True}
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    fields = {
        "name": _text(body.get("name"), 80),
        "use": _text(body.get("use"), 60),
        "list": _text(body.get("list"), 40) or "glasses-presale",
        "path": _page(body.get("path")),
        "ref": _ref_host(body.get("referrer")),
        "utm_source": _text(body.get("utmSource"), 60),
        "utm_campaign": _text(body.get("utmCampaign"), 80),
        "country": _country(headers),
        "visitor": str(body.get("visitor") or "") if _VISITOR.match(str(body.get("visitor") or "")) else "",
    }
    path = _backers_path(product_dir)
    with locked(path, _BACKERS_LOCK):
        rows = _read_backers(path)
        current = rows.get(email)
        if current is None and len(rows) >= MAX_BACKERS:
            return 503, {"error": True, "message": "Please try again later."}
        if current is None:
            rows[email] = {"id": secrets.token_hex(8), "email": email, "created_at": stamp, "signups": 1, **fields}
        else:
            # Signing up again keeps the first date and source; only what they newly typed changes.
            current.update({k: fields[k] for k in ("name", "use") if fields[k]})
            current["signups"] = int(current.get("signups") or 1) + 1
            current["updated_at"] = stamp
        _write_backers(path, rows)
    # The same answer whether or not the email was already listed: the form must not reveal who is on it.
    return 200, {"ok": True}


# ---------------------------------------------------------------- who is online


def _note_presence(row: dict[str, Any], now: datetime) -> None:
    stamp = now.timestamp()
    minute = int(stamp // 60)
    with _STATE_LOCK:
        current = _presence.get(row["visitor"])
        fresh = current is None or stamp - current["last_seen"] > VISIT_GAP_S
        entry: dict[str, Any] = {"first_seen": stamp, "ref": "", "utm_source": ""} if fresh else current
        for key in ("visitor", "site", "path", "device", "browser", "user_id", "country", "tz"):
            if row.get(key) or key not in entry:
                entry[key] = row.get(key)
        # A visit keeps the source it arrived from, not the page it moved to.
        entry["ref"] = entry["ref"] or row["ref"]
        entry["utm_source"] = entry["utm_source"] or row["utm_source"]
        entry["last_seen"] = stamp
        _presence[row["visitor"]] = entry
        if len(_presence) > MAX_PRESENCE:
            for stale in sorted(_presence, key=lambda v: _presence[v]["last_seen"])[:500]:
                _presence.pop(stale, None)
        _minutes.setdefault(minute, set()).add(row["visitor"])
        for old in [m for m in _minutes if m <= minute - MINUTES_KEPT]:
            _minutes.pop(old, None)


def online(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """Visitors seen in the last ONLINE_WINDOW_S seconds, most recent first."""
    stamp = (now or datetime.now(timezone.utc)).timestamp()
    with _STATE_LOCK:
        rows = [dict(v) for v in _presence.values() if stamp - v["last_seen"] <= ONLINE_WINDOW_S]
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    for r in rows:
        r["seconds"] = max(0, int(r["last_seen"] - r["first_seen"]))
        r["first_seen"] = datetime.fromtimestamp(r["first_seen"], timezone.utc).isoformat()
        r["last_seen"] = datetime.fromtimestamp(r["last_seen"], timezone.utc).isoformat()
    return rows


def per_minute(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """Distinct visitors in each of the last MINUTES_KEPT minutes, oldest first (since the last restart)."""
    minute = int((now or datetime.now(timezone.utc)).timestamp() // 60)
    with _STATE_LOCK:
        counts = {m: len(v) for m, v in _minutes.items()}
    return [
        {"minute": datetime.fromtimestamp(m * 60, timezone.utc).isoformat(), "count": counts.get(m, 0)}
        for m in range(minute - MINUTES_KEPT + 1, minute + 1)
    ]


def clear_presence() -> None:
    with _STATE_LOCK:
        _presence.clear()
        _minutes.clear()


# ---------------------------------------------------------------- stored page views


def rows(product_dir: Path) -> list[dict[str, Any]]:
    """Every stored row, oldest first. Re-parsed only when the file changes: the live page polls."""
    path = _events_path(product_dir)
    try:
        info = path.stat()
    except OSError:
        return []
    key = (str(path), info.st_mtime_ns, info.st_size)
    with _STATE_LOCK:
        if _cache["key"] == key:
            return _cache["rows"]
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    with _STATE_LOCK:
        _cache.update(key=key, rows=out)
    return out


def source_of(row: dict[str, Any]) -> str:
    return str(row.get("utm_source") or row.get("ref") or "")


def _top(counter: Counter, limit: int = 8) -> list[dict[str, Any]]:
    return [{"name": name, "count": count} for name, count in counter.most_common(limit)]


def today_counts(product_dir: Path, *, now: datetime | None = None) -> dict[str, int]:
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    mine = [r for r in rows(product_dir) if str(r.get("ts") or "").startswith(today)]
    return {
        "visitors": len({r.get("visitor") for r in mine}),
        "page_views": sum(1 for r in mine if r.get("event") == "page_view"),
    }


def traffic(product_dir: Path, days: int, *, now: datetime | None = None) -> dict[str, Any]:
    """Visitors per day and where they came from, over the last `days` days (UTC)."""
    now = now or datetime.now(timezone.utc)
    labels = [(now - timedelta(days=i)).date().isoformat() for i in range(days - 1, -1, -1)]
    window = [r for r in rows(product_dir) if str(r.get("ts") or "")[:10] >= labels[0]]
    main: dict[str, set] = {d: set() for d in labels}
    playground: dict[str, set] = {d: set() for d in labels}
    views: Counter = Counter()
    first: dict[str, dict[str, Any]] = {}
    pages: Counter = Counter()
    clicks: Counter = Counter()
    clickers: set = set()
    for r in window:
        day = str(r.get("ts") or "")[:10]
        visitor = r.get("visitor")
        if day not in main:
            continue
        (playground if r.get("site") == "playground" else main)[day].add(visitor)
        first.setdefault(visitor, r)
        if r.get("event") == "page_view":
            views[day] += 1
            pages[("Playground " if r.get("site") == "playground" else "") + str(r.get("path") or "/")] += 1
        elif r.get("event") == "cta_click":
            clickers.add(visitor)
            clicks[str(r.get("label") or r.get("target") or "(未命名)")] += 1
    return {
        "days": days,
        "labels": labels,
        "visitors_main": [len(main[d]) for d in labels],
        "visitors_playground": [len(playground[d]) for d in labels],
        "page_views": [views[d] for d in labels],
        "total_visitors": len(first),
        "total_page_views": sum(views.values()),
        "clickers": len(clickers),
        "sources": _top(Counter(source_of(r) or "直接访问" for r in first.values())),
        "countries": _top(Counter(str(r.get("country") or "未知") for r in first.values())),
        "devices": _top(Counter(str(r.get("device") or "未知") for r in first.values())),
        "browsers": _top(Counter(str(r.get("browser") or "未知") for r in first.values())),
        "pages": _top(pages),
        "clicks": _top(clicks),
    }


def forget(product_dir: Path, user_id: str) -> None:
    """Remove a deleted account's rows and presence."""
    with _STATE_LOCK:
        for visitor in [v for v, r in _presence.items() if r.get("user_id") == user_id]:
            _presence.pop(visitor, None)
    _rewrite(product_dir, lambda r: r.get("user_id") != user_id)


def prune(product_dir: Path, *, older_than: datetime, dry_run: bool = False) -> int:
    return _rewrite(product_dir, lambda r: (_utc(r.get("ts")) or older_than) >= older_than, dry_run=dry_run)


def _rewrite(product_dir: Path, keep: Any, *, dry_run: bool = False) -> int:
    path = _events_path(product_dir)
    if not path.is_file():
        return 0
    with locked(path, _EVENTS_LOCK):
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        kept = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and keep(row):
                kept.append(line)
        removed = len(lines) - len(kept)
        if removed and not dry_run:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(k + "\n" for k in kept), encoding="utf-8")
            tmp.replace(path)
    return removed


# ---------------------------------------------------------------- presale list


def _read_backers(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Never start over from an unreadable list: the next write would erase everyone on it.
        raise RuntimeError("backers.json is unreadable") from None
    return data if isinstance(data, dict) else {}


def _write_backers(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _remote_config() -> tuple[str, str, str] | None:
    url = (os.environ.get("CEREBEL_SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("CEREBEL_SUPABASE_KEY") or "").strip()
    token = (os.environ.get("CEREBEL_BACKERS_ADMIN_TOKEN") or "").strip()
    return (url, key, token) if url and key and token else None


def _rpc(name: str, args: dict[str, Any]) -> Any:
    """Call a Postgres function through Supabase. Raises RuntimeError on any failure."""
    config = _remote_config()
    if config is None:
        raise RuntimeError("Supabase is not configured")
    url, key, _token = config
    req = urllib.request.Request(
        f"{url}/rest/v1/rpc/{name}",
        data=json.dumps(args).encode("utf-8"),
        headers={"apikey": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
    except (OSError, ValueError) as exc:  # URLError and HTTPError are OSErrors
        raise RuntimeError(f"Supabase {name} failed: {getattr(exc, 'code', type(exc).__name__)}") from None
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        raise RuntimeError(f"Supabase {name} sent an unreadable answer") from None


def _remote_backers() -> list[dict[str, Any]]:
    """The list from Supabase, briefly cached. If a read fails, keep the last good list and say so."""
    token = _remote_config()[2]  # type: ignore[index]
    with _STATE_LOCK:
        at = _remote["at"]
        if at is not None and time.monotonic() - at < REMOTE_CACHE_S:
            return _remote["rows"]
    try:
        rows_ = _rpc("admin_list_backers", {"token": token})
        fresh = [{**r, "use": r.get("use_case") or ""} for r in rows_ if isinstance(r, dict)]
        error = None
    except RuntimeError as exc:
        fresh, error = None, str(exc)
    with _STATE_LOCK:
        _remote["at"] = time.monotonic()
        _remote["error"] = error
        if fresh is not None:
            _remote["rows"] = fresh
        return _remote["rows"]


def backer_store_error() -> str | None:
    """Why the last read of the list failed, or None. The rows shown may then be out of date or empty."""
    with _STATE_LOCK:
        return _remote["error"] if _remote_config() else None


def _forget_remote_cache() -> None:
    with _STATE_LOCK:
        _remote.update(at=None, rows=[], error=None)


def backers(product_dir: Path) -> list[dict[str, Any]]:
    """Newest first."""
    if _remote_config():
        return _remote_backers()
    rows_ = [r for r in _read_backers(_backers_path(product_dir)).values() if isinstance(r, dict)]
    rows_.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows_


def remove_backer(product_dir: Path, backer_id: str) -> str | None:
    """Delete one row by its id; returns the email removed, or None."""
    if _remote_config():
        email = _rpc("admin_remove_backer", {"token": _remote_config()[2], "backer_id": backer_id})  # type: ignore[index]
        _forget_remote_cache()
        return email if isinstance(email, str) else None
    path = _backers_path(product_dir)
    with locked(path, _BACKERS_LOCK):
        data = _read_backers(path)
        for email, row in data.items():
            if isinstance(row, dict) and row.get("id") == backer_id:
                del data[email]
                _write_backers(path, data)
                return email
    return None


def tail_jsonl(path: Path, max_bytes: int = 131_072) -> list[dict[str, Any]]:
    """The newest rows of a JSONL file without reading all of it, oldest first."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="ignore").splitlines()
    if size > max_bytes:
        lines = lines[1:]  # the first line was cut in half
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
