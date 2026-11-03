"""Copy the product server's data files to Supabase, so the dashboard on cerebel.tech/admin keeps
working while this server is down.

The files in data/product/ stay the source. Every MIRROR_EVERY_S seconds a thread compares them
with what it last sent and pushes the difference: new and changed rows are upserted, rows that are
gone from the file (a deleted account, a scrubbed event, a pruned visit) are deleted from Supabase
too, so deletions follow the files and the privacy policy within one cycle. The first cycle after a
start asks Supabase which keys it holds, so rows deleted while this process was down go as well.

Not copied: password hashes and salts, login tokens, reset links, the question counters (they hold
visitor IPs), videos and body data.

Needs CEREBEL_SUPABASE_URL and CEREBEL_SUPABASE_SERVICE_KEY (a secret key: it bypasses row level
security, so it lives only in .env.product). Without them the mirror does nothing.

    python -m motion_agent.app.mirror --once    # one full cycle now, then print row counts
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

LOG = logging.getLogger("motion_agent.mirror")
MIRROR_EVERY_S = float(os.environ.get("CEREBEL_MIRROR_EVERY_S") or 30)
BATCH = 500

# Only these account fields leave the server.
ACCOUNT_FIELDS = (
    "user_id", "email", "created_at", "last_login_at", "last_active_at", "coach_code", "terms_version",
    "terms_accepted_at", "video_consent_at", "video_consent_version", "video_consent_retention_days",
    "password_changed_at", "level", "handedness",
)
SITE_EVENT_FIELDS = (
    "ts", "event", "visitor", "site", "path", "ref", "utm_source", "utm_medium", "utm_campaign", "country", "tz",
    "device", "browser", "target", "label", "user_id",
)


def _config() -> tuple[str, str] | None:
    url = (os.environ.get("CEREBEL_SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("CEREBEL_SUPABASE_SERVICE_KEY") or "").strip()
    return (url, key) if url and key else None


def enabled() -> bool:
    return _config() is not None


# ---------------------------------------------------------------- HTTP


class MirrorError(RuntimeError):
    pass


def _request(method: str, path: str, body: Any = None, prefer: str | None = None) -> Any:
    config = _config()
    if config is None:
        raise MirrorError("mirror is not configured")
    url, key = config
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):  # a legacy JWT service key also goes in Authorization; sb_secret_ keys must not
        headers["Authorization"] = "Bearer " + key
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body, default=str).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", errors="replace")
        raise MirrorError(f"{method} {path.split('?')[0]} -> {exc.code} {detail}") from None
    except (OSError, ValueError) as exc:
        raise MirrorError(f"{method} {path.split('?')[0]} failed: {type(exc).__name__}") from None
    return json.loads(raw) if raw else None


def _upsert(table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
    for i in range(0, len(rows), BATCH):
        _request(
            "POST", f"{table}?on_conflict={urllib.parse.quote(on_conflict)}", rows[i:i + BATCH],
            prefer="resolution=merge-duplicates,return=minimal",
        )


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _delete(table: str, column: str, values: Iterable[str]) -> None:
    values = list(values)
    for i in range(0, len(values), 100):
        chunk = ",".join(_quote(v) for v in values[i:i + 100])
        _request("DELETE", f"{table}?{column}=in.({urllib.parse.quote(chunk, safe=',')})", prefer="return=minimal")


def _remote_keys(table: str, column: str, where: str = "") -> set[str]:
    keys: set[str] = set()
    offset = 0
    while True:
        page = _request("GET", f"{table}?select={column}{where}&order={column}&limit=1000&offset={offset}")
        keys.update(str(r[column]) for r in page or [])
        if not page or len(page) < 1000:
            return keys
        offset += 1000


# ---------------------------------------------------------------- reading the files


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _jsonl(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """(key, row) for each line; the key is a hash of the line, stable across rewrites of the file."""
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append((hashlib.sha1(line.encode("utf-8")).hexdigest(), row))
    return out


def _ts(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).isoformat()


def accounts(product_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for row in (_json(product_dir / "accounts.json", {}) or {}).values():
        if isinstance(row, dict) and row.get("user_id") and row.get("email"):
            data = {k: row.get(k) for k in ACCOUNT_FIELDS if row.get(k) is not None}
            out[str(row["user_id"])] = {
                "user_id": str(row["user_id"]), "email": str(row["email"]).lower(),
                "created_at": _ts(row.get("created_at")), "data": data,
            }
    return out


def captures(product_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    rows = _json(product_dir / "library.json", [])
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("session_id"):
            out[str(row["session_id"])] = {
                "session_id": str(row["session_id"]), "user_id": row.get("user_id"), "status": row.get("status"),
                "created_at": _ts(row.get("created_at")), "deleted": bool(row.get("deleted")), "data": row,
            }
    return out


def subscriptions(product_dir: Path) -> dict[str, dict[str, Any]]:
    data = _json(product_dir / "billing.json", {}) or {}
    out = {
        str(uid): {"user_id": str(uid), "data": row}
        for uid, row in (data.get("users") or {}).items() if isinstance(row, dict)
    }
    out["_meta"] = {"user_id": "_meta", "data": {"founding_used": int(data.get("founding_used") or 0)}}
    return out


def visits(product_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for uid, row in (_json(product_dir / "activity.json", {}) or {}).items():
        for visit in (row or {}).get("visits") or [] if isinstance(row, dict) else []:
            start, end = _ts(visit.get("start")), _ts(visit.get("end"))
            if start and end:
                out[f"{uid}|{start}"] = {"user_id": str(uid), "started_at": start, "ended_at": end}
    return out


def events(product_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for key, row in _jsonl(product_dir / "events.jsonl"):
        ts = _ts(row.get("ts"))
        if ts and row.get("kind"):
            rest = {k: v for k, v in row.items() if k not in ("ts", "kind", "user_id", "session_id")}
            out[key] = {"key": key, "ts": ts, "kind": str(row["kind"]), "user_id": row.get("user_id"),
                        "session_id": row.get("session_id") or None, "data": rest}
    return out


def questions(product_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for key, row in _jsonl(product_dir / "questions.jsonl"):
        ts = _ts(row.get("ts"))
        if ts and row.get("user_id") and row.get("question"):
            out[key] = {"key": key, "ts": ts, "user_id": str(row["user_id"]),
                        "session_id": row.get("session_id") or None, "question": str(row["question"])}
    return out


def audit(product_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for key, row in _jsonl(product_dir / "admin_audit.jsonl"):
        ts = _ts(row.get("ts"))
        if ts and row.get("action"):
            rest = {k: v for k, v in row.items() if k not in ("ts", "admin", "action")}
            out[key] = {"key": key, "ts": ts, "admin": str(row.get("admin") or ""), "action": str(row["action"]), "data": rest}
    return out


def site_events(product_dir: Path) -> dict[str, dict[str, Any]]:
    """Playground visits recorded by this server (cerebel.tech writes its own straight to Supabase)."""
    out = {}
    for key, row in _jsonl(product_dir / "site_events.jsonl"):
        ts = _ts(row.get("ts"))
        if ts and row.get("event") and row.get("visitor"):
            clean = {k: row.get(k) for k in SITE_EVENT_FIELDS}
            out[key] = {**{k: (v if v is not None else "") for k, v in clean.items() if k != "user_id"},
                        "ts": ts, "user_id": row.get("user_id"), "key": key}
    return out


# table, its key column(s), how rows are keyed in Supabase for deletes, the reader
TABLES: list[tuple[str, str, Callable[[Path], dict[str, dict[str, Any]]]]] = [
    ("accounts", "user_id", accounts),
    ("captures", "session_id", captures),
    ("subscriptions", "user_id", subscriptions),
    ("events", "key", events),
    ("questions", "key", questions),
    ("admin_audit", "key", audit),
    ("site_events", "key", site_events),
    ("visits", "user_id,started_at", visits),
]


def _digest(row: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- one cycle


class Mirror:
    def __init__(self, product_dir: Path) -> None:
        self.product_dir = product_dir
        self.sent: dict[str, dict[str, str]] = {}  # table -> key -> digest of what Supabase holds

    def _remote_state(self, table: str, column: str) -> dict[str, str]:
        """Keys Supabase holds, with an empty digest so every current row is sent once."""
        if table == "visits":
            rows = []
            offset = 0
            while True:
                page = _request("GET", f"visits?select=user_id,started_at&order=user_id,started_at&limit=1000&offset={offset}")
                rows.extend(page or [])
                if not page or len(page) < 1000:
                    break
                offset += 1000
            return {f"{r['user_id']}|{_ts(r['started_at'])}": "" for r in rows}
        where = "&key=not.is.null" if table == "site_events" else ""
        return {k: "" for k in _remote_keys(table, column, where)}

    def sync_table(self, table: str, column: str, reader: Callable[[Path], dict[str, dict[str, Any]]]) -> tuple[int, int]:
        current = reader(self.product_dir)
        if table not in self.sent:
            self.sent[table] = self._remote_state(table, column)
        sent = self.sent[table]
        changed = {k: r for k, r in current.items() if sent.get(k) != _digest(r)}
        gone = [k for k in sent if k not in current]
        if changed:
            stamp = datetime.now(timezone.utc).isoformat()
            rows = [{**r, "mirrored_at": stamp} if table in ("accounts", "captures", "subscriptions") else r
                    for r in changed.values()]
            _upsert(table, rows, column)
        if gone:
            if table == "visits":
                for k in gone:
                    uid, start = k.split("|", 1)
                    _request("DELETE", f"visits?user_id=eq.{urllib.parse.quote(uid)}&started_at=eq.{urllib.parse.quote(start)}",
                             prefer="return=minimal")
            else:
                _delete(table, column, gone)
        for k in gone:
            sent.pop(k, None)
        for k, r in changed.items():
            sent[k] = _digest(r)
        return len(changed), len(gone)

    def cycle(self) -> dict[str, tuple[int, int]]:
        report = {}
        for table, column, reader in TABLES:
            try:
                report[table] = self.sync_table(table, column, reader)
            except MirrorError as exc:
                # Keep going with the other tables; this one is retried next cycle from what it last sent.
                LOG.warning("mirror %s: %s", table, exc)
                report[table] = (-1, -1)
        try:
            self.push_presence()
        except MirrorError as exc:
            LOG.warning("mirror presence: %s", exc)
        return report

    def push_presence(self) -> None:
        """Who is on the Playground right now, from this server's memory."""
        from motion_agent.app import site_traffic

        rows = [
            {
                "visitor": r["visitor"], "site": r.get("site") or "playground", "path": r.get("path") or "/",
                "ref": r.get("ref") or "", "utm_source": r.get("utm_source") or "", "country": r.get("country") or "",
                "tz": r.get("tz") or "", "device": r.get("device") or "", "browser": r.get("browser") or "",
                "user_id": r.get("user_id"), "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            }
            for r in site_traffic.online()
        ]
        if rows:
            _upsert("presence", rows, "visitor")


_started = False
_start_lock = threading.Lock()


def start(product_dir: Path) -> bool:
    """Run the mirror in a daemon thread of this process. Safe to call more than once."""
    global _started
    if not enabled():
        return False
    with _start_lock:
        if _started:
            return True
        _started = True
    mirror = Mirror(product_dir)

    def loop() -> None:
        while True:
            try:
                report = mirror.cycle()
                moved = {t: c for t, c in report.items() if c != (0, 0)}
                if moved:
                    LOG.info("mirror: %s", moved)
            except Exception:  # noqa: BLE001 - the mirror must never take the product server down
                LOG.exception("mirror cycle failed")
            time.sleep(MIRROR_EVERY_S)

    threading.Thread(target=loop, name="supabase-mirror", daemon=True).start()
    return True


def main() -> int:
    import argparse

    from motion_agent.app.config import load_settings

    parser = argparse.ArgumentParser(description="Mirror the product data files to Supabase.")
    parser.add_argument("--once", action="store_true", help="run one full cycle and print the row counts")
    args = parser.parse_args()
    if not args.once:
        parser.error("use --once; the server runs the mirror itself")
    if not enabled():
        print("CEREBEL_SUPABASE_URL and CEREBEL_SUPABASE_SERVICE_KEY are not set")
        return 2
    logging.basicConfig(level=logging.INFO)
    product_dir = load_settings().product_dir
    mirror = Mirror(product_dir)
    report = mirror.cycle()
    bad = [t for t, c in report.items() if c == (-1, -1)]
    for table, column, reader in TABLES:
        local = len(reader(product_dir))
        remote = len(mirror._remote_state(table, column))  # noqa: SLF001
        print(f"{table:14} files {local:6}  supabase {remote:6}  {'OK' if local == remote else 'MISMATCH'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
