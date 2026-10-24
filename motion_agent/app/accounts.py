"""Local accounts for the product beta. Tokens live on disk under data/product/."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
# Login tokens expire. CEREBEL_TOKEN_TTL_DAYS changes the lifetime (default 30 days).
TOKEN_TTL = timedelta(days=float(os.environ.get("CEREBEL_TOKEN_TTL_DAYS") or 30))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(product_dir: Path) -> tuple[Path, Path]:
    product_dir.mkdir(parents=True, exist_ok=True)
    return product_dir / "accounts.json", product_dir / "tokens.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return digest.hex()


TERMS_VERSION = "2026-10-24"


def _clean_coach_code(code: str | None) -> str | None:
    code = "".join(ch for ch in str(code or "").strip().upper() if ch.isalnum() or ch in "-_")
    return code[:32] or None


def register(
    product_dir: Path,
    email: str,
    password: str,
    *,
    accepted_terms: bool = True,
    coach_code: str | None = None,
) -> dict[str, Any]:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        raise ValueError("Enter a valid email.")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    if not accepted_terms:
        raise ValueError("Please accept the Terms and Privacy Policy, including video processing.")
    with _LOCK:
        accounts_path, tokens_path = _paths(product_dir)
        accounts = _read(accounts_path)
        if email in accounts:
            raise ValueError("An account with that email already exists.")
        salt = secrets.token_hex(16)
        user_id = secrets.token_hex(8)
        accounts[email] = {
            "user_id": user_id,
            "email": email,
            "salt": salt,
            "password_hash": _hash_password(password, salt),
            "created_at": _now(),
            "terms_version": TERMS_VERSION,
            "terms_accepted_at": _now(),
            "coach_code": _clean_coach_code(coach_code),
            "last_active_at": _now(),
            # Separate, explicit consent to video processing is recorded before the first upload.
            "video_consent_version": None,
            "video_consent_at": None,
        }
        _write(accounts_path, accounts)
        token = _issue_token(tokens_path, user_id, email)
    return {"ok": True, "token": token, "user": {"user_id": user_id, "email": email}}


def login(product_dir: Path, email: str, password: str) -> dict[str, Any]:
    email = (email or "").strip().lower()
    with _LOCK:
        accounts_path, tokens_path = _paths(product_dir)
        accounts = _read(accounts_path)
        row = accounts.get(email)
        expected_hash = str((row or {}).get("password_hash") or "")
        given_hash = _hash_password(password or "", (row or {}).get("salt") or "")
        if not row or not hmac.compare_digest(given_hash, expected_hash):
            raise ValueError("Email or password is incorrect.")
        row["last_login_at"] = row["last_active_at"] = _now()
        _write(accounts_path, accounts)
        token = _issue_token(tokens_path, row["user_id"], email)
    return {"ok": True, "token": token, "user": {"user_id": row["user_id"], "email": email}}


def _token_expired(row: Any, now: datetime) -> bool:
    """Tokens issued before expiry existed fall back to created_at + TOKEN_TTL."""
    if not isinstance(row, dict):
        return True
    try:
        if row.get("expires_at"):
            expires = datetime.fromisoformat(str(row["expires_at"]))
        else:
            expires = datetime.fromisoformat(str(row.get("created_at"))) + TOKEN_TTL
    except (TypeError, ValueError):
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return now >= expires


def _issue_token(tokens_path: Path, user_id: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    # Drop expired tokens while we are rewriting the file anyway.
    tokens = {tok: row for tok, row in _read(tokens_path).items() if not _token_expired(row, now)}
    token = secrets.token_urlsafe(32)
    tokens[token] = {
        "user_id": user_id,
        "email": email,
        "created_at": now.isoformat(),
        "expires_at": (now + TOKEN_TTL).isoformat(),
    }
    _write(tokens_path, tokens)
    return token


def user_from_token(product_dir: Path, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    if not token:
        return None
    _, tokens_path = _paths(product_dir)
    row = _read(tokens_path).get(token)
    if not row or _token_expired(row, datetime.now(timezone.utc)):
        return None
    return {"user_id": row.get("user_id"), "email": row.get("email"), "token": token}


def delete_account(product_dir: Path, user_id: str) -> None:
    """Remove the account and every login token for that user."""
    with _LOCK:
        accounts_path, tokens_path = _paths(product_dir)
        accounts = _read(accounts_path)
        accounts = {email: row for email, row in accounts.items() if row.get("user_id") != user_id}
        _write(accounts_path, accounts)
        tokens = _read(tokens_path)
        tokens = {tok: row for tok, row in tokens.items() if row.get("user_id") != user_id}
        _write(tokens_path, tokens)


def logout(product_dir: Path, token: str | None) -> None:
    if not token:
        return
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    with _LOCK:
        _, tokens_path = _paths(product_dir)
        tokens = _read(tokens_path)
        tokens.pop(token, None)
        _write(tokens_path, tokens)


def profile(product_dir: Path, user_id: str) -> dict[str, Any]:
    """Non-secret account fields (no password hash or salt)."""
    accounts_path, _ = _paths(product_dir)
    for row in _read(accounts_path).values():
        if row.get("user_id") == user_id:
            return {
                "user_id": user_id,
                "email": row.get("email"),
                "coach_code": row.get("coach_code"),
                "created_at": row.get("created_at"),
                "terms_version": row.get("terms_version"),
                "video_consent_version": row.get("video_consent_version"),
                "video_consent_at": row.get("video_consent_at"),
                "video_consent_retention_days": row.get("video_consent_retention_days"),
                "last_active_at": row.get("last_active_at"),
            }
    return {}


# ---------------------------------------------------------------- video consent and activity


def video_consent_is_current(version: str | None, retention_days: int | None = None) -> bool:
    """Consent counts only for the current text and a raw-video retention no longer than agreed."""
    from motion_agent.app.consent import VIDEO_CONSENT_VERSION, raw_video_retention_days

    if not version or version != VIDEO_CONSENT_VERSION:
        return False
    if retention_days is None:
        return True
    try:
        return int(retention_days) >= raw_video_retention_days()
    except (TypeError, ValueError):
        return False


def has_video_consent(product_dir: Path, user_id: str) -> bool:
    row = profile(product_dir, user_id)
    return bool(row) and video_consent_is_current(
        row.get("video_consent_version"), row.get("video_consent_retention_days")
    )


def video_consent_status(product_dir: Path, user_id: str) -> dict[str, Any]:
    from motion_agent.app.consent import public_video_consent

    row = profile(product_dir, user_id)
    return public_video_consent(
        row.get("video_consent_version"),
        row.get("video_consent_at"),
        row.get("video_consent_retention_days"),
    )


def record_video_consent(product_dir: Path, user_id: str, version: str) -> dict[str, Any]:
    """Store the user's explicit agreement to the current video-processing text."""
    from motion_agent.app.consent import VIDEO_CONSENT_VERSION, raw_video_retention_days

    if version != VIDEO_CONSENT_VERSION:
        raise ValueError("The consent text has changed. Reload the page and review it again.")
    with _LOCK:
        accounts_path, _ = _paths(product_dir)
        accounts = _read(accounts_path)
        row = next((r for r in accounts.values() if r.get("user_id") == user_id), None)
        if row is None:
            raise LookupError("Account not found.")
        now = _now()
        row["video_consent_version"] = VIDEO_CONSENT_VERSION
        row["video_consent_at"] = now
        row["video_consent_retention_days"] = raw_video_retention_days()
        row["last_active_at"] = now
        _write(accounts_path, accounts)
    return {"version": VIDEO_CONSENT_VERSION, "accepted_at": now}


ACTIVITY_WRITE_INTERVAL = timedelta(hours=12)


def touch_activity(product_dir: Path, user_id: str, *, force: bool = False) -> None:
    """Remember the last interaction (used for inactivity deletion). Writes at most twice a day."""
    now = datetime.now(timezone.utc)
    accounts_path, _ = _paths(product_dir)
    row = next((r for r in _read(accounts_path).values() if r.get("user_id") == user_id), None)
    if row is None:
        return
    if not force:
        try:
            last = datetime.fromisoformat(str(row.get("last_active_at")))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < ACTIVITY_WRITE_INTERVAL:
                return
        except (TypeError, ValueError):
            pass
    with _LOCK:
        accounts = _read(accounts_path)
        for item in accounts.values():
            if item.get("user_id") == user_id:
                item["last_active_at"] = now.isoformat()
                _write(accounts_path, accounts)
                return


def list_accounts(product_dir: Path) -> list[dict[str, Any]]:
    """Account rows without password material (for the retention job)."""
    accounts_path, _ = _paths(product_dir)
    secret = {"salt", "password_hash"}
    return [{k: v for k, v in row.items() if k not in secret} for row in _read(accounts_path).values()]


def tokens_for_user(product_dir: Path, user_id: str) -> list[dict[str, Any]]:
    _, tokens_path = _paths(product_dir)
    return [dict(row) for row in _read(tokens_path).values() if isinstance(row, dict) and row.get("user_id") == user_id]
