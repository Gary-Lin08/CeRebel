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

from motion_agent.app.file_lock import locked

_LOCK = threading.Lock()
# Login tokens expire. CEREBEL_TOKEN_TTL_DAYS changes the lifetime (default 30 days).
TOKEN_TTL = timedelta(days=float(os.environ.get("CEREBEL_TOKEN_TTL_DAYS") or 30))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(product_dir: Path) -> tuple[Path, Path]:
    product_dir.mkdir(parents=True, exist_ok=True)
    return product_dir / "accounts.json", product_dir / "tokens.json"


def _locked(product_dir: Path):
    """Around every write of accounts.json or tokens.json (one lock for both files): the retention
    job and privacy_backfill delete accounts from other processes."""
    return locked(product_dir / "accounts.json", _LOCK)


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


# 2026-10-26: the privacy policy now keeps signed-in users' coach question text (questions.py).
TERMS_VERSION = "2026-10-26"


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
    with _locked(product_dir):
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
    with _locked(product_dir):
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
    """Remove the account, every login token and reset link, its visits and its coach questions."""
    from motion_agent.app import activity, questions, site_traffic

    with _locked(product_dir):
        accounts_path, tokens_path = _paths(product_dir)
        accounts = _read(accounts_path)
        accounts = {email: row for email, row in accounts.items() if row.get("user_id") != user_id}
        _write(accounts_path, accounts)
        tokens = _read(tokens_path)
        tokens = {tok: row for tok, row in tokens.items() if row.get("user_id") != user_id}
        _write(tokens_path, tokens)
        _drop_resets(product_dir, user_id)
    activity.forget(product_dir, user_id)
    questions.scrub(product_dir, user_id=user_id)
    site_traffic.forget(product_dir, user_id)


# ---------------------------------------------------------------- passwords

# A reset link works once, for an hour (CEREBEL_RESET_TTL_MINUTES). Only its SHA-256 is on disk.
RESET_TTL = timedelta(minutes=float(os.environ.get("CEREBEL_RESET_TTL_MINUTES") or 60))


def _resets_path(product_dir: Path) -> Path:
    return product_dir / "password_resets.json"


def _reset_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _live_resets(product_dir: Path, now: datetime) -> dict[str, Any]:
    return {k: r for k, r in _read(_resets_path(product_dir)).items() if not _token_expired(r, now)}


def _drop_resets(product_dir: Path, user_id: str) -> None:
    """Caller holds the accounts lock."""
    path = _resets_path(product_dir)
    if not path.is_file():
        return
    resets = {k: r for k, r in _read(path).items() if isinstance(r, dict) and r.get("user_id") != user_id}
    _write(path, resets)


def _set_password(row: dict[str, Any], password: str) -> None:
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    row["salt"] = secrets.token_hex(16)
    row["password_hash"] = _hash_password(password, row["salt"])
    row["password_changed_at"] = row["last_active_at"] = _now()


def create_password_reset(product_dir: Path, email: str, *, ttl: timedelta | None = None) -> str | None:
    """A one-time reset token for this email, or None when no account uses it."""
    email = (email or "").strip().lower()
    now = datetime.now(timezone.utc)
    with _locked(product_dir):
        accounts_path, _ = _paths(product_dir)
        row = _read(accounts_path).get(email)
        if not row:
            return None
        resets = _live_resets(product_dir, now)
        token = secrets.token_urlsafe(32)
        resets[_reset_key(token)] = {
            "user_id": row["user_id"],
            "created_at": now.isoformat(),
            "expires_at": (now + (ttl or RESET_TTL)).isoformat(),
        }
        _write(_resets_path(product_dir), resets)
    return token


def reset_password(product_dir: Path, token: str, new_password: str) -> dict[str, Any]:
    """Set a new password from a reset link. Signs out every device and returns a fresh login."""
    if len(new_password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    now = datetime.now(timezone.utc)
    with _locked(product_dir):
        reset = _live_resets(product_dir, now).get(_reset_key(str(token or "")))
        accounts_path, tokens_path = _paths(product_dir)
        accounts = _read(accounts_path)
        row = next((r for r in accounts.values() if reset and r.get("user_id") == reset.get("user_id")), None)
        if row is None:
            raise ValueError("This reset link has expired or was already used. Ask for a new one.")
        _set_password(row, new_password)
        _write(accounts_path, accounts)
        _drop_resets(product_dir, row["user_id"])
        _revoke_tokens(tokens_path, row["user_id"])
        login_token = _issue_token(tokens_path, row["user_id"], row["email"])
    return {"ok": True, "token": login_token, "user": {"user_id": row["user_id"], "email": row["email"]}}


def change_password(product_dir: Path, user_id: str, current: str, new_password: str) -> dict[str, Any]:
    """Signed-in password change. Other devices are signed out; returns this device's new login."""
    with _locked(product_dir):
        accounts_path, tokens_path = _paths(product_dir)
        accounts = _read(accounts_path)
        row = next((r for r in accounts.values() if r.get("user_id") == user_id), None)
        if row is None:
            raise LookupError("Account not found.")
        given = _hash_password(current or "", row.get("salt") or "")
        if not hmac.compare_digest(given, str(row.get("password_hash") or "")):
            raise ValueError("Your current password is incorrect.")
        _set_password(row, new_password)
        _write(accounts_path, accounts)
        _drop_resets(product_dir, user_id)
        _revoke_tokens(tokens_path, user_id)
        login_token = _issue_token(tokens_path, user_id, row["email"])
    return {"ok": True, "token": login_token, "user": {"user_id": user_id, "email": row["email"]}}


def _revoke_tokens(tokens_path: Path, user_id: str) -> None:
    tokens = {tok: r for tok, r in _read(tokens_path).items() if isinstance(r, dict) and r.get("user_id") != user_id}
    _write(tokens_path, tokens)


def prune_password_resets(product_dir: Path) -> int:
    """Drop expired reset tokens (the retention job calls this)."""
    path = _resets_path(product_dir)
    if not path.is_file():
        return 0
    with _locked(product_dir):
        resets = _read(path)
        live = _live_resets(product_dir, datetime.now(timezone.utc))
        if len(live) != len(resets):
            _write(path, live)
    return len(resets) - len(live)


def logout(product_dir: Path, token: str | None) -> None:
    if not token:
        return
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    with _locked(product_dir):
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
                "last_login_at": row.get("last_login_at"),
                "password_changed_at": row.get("password_changed_at"),
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
    with _locked(product_dir):
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
    with _locked(product_dir):
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
