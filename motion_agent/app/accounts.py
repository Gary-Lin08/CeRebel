"""Local accounts for the product beta. Tokens live on disk under data/product/."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


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


def register(product_dir: Path, email: str, password: str) -> dict[str, Any]:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        raise ValueError("Enter a valid email.")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
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
        if not row or _hash_password(password or "", row.get("salt") or "") != row.get("password_hash"):
            raise ValueError("Email or password is incorrect.")
        token = _issue_token(tokens_path, row["user_id"], email)
    return {"ok": True, "token": token, "user": {"user_id": row["user_id"], "email": email}}


def _issue_token(tokens_path: Path, user_id: str, email: str) -> str:
    tokens = _read(tokens_path)
    token = secrets.token_urlsafe(32)
    tokens[token] = {"user_id": user_id, "email": email, "created_at": _now()}
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
    if not row:
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
