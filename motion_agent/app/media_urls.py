"""Short-lived signed URLs for /media/<session_id>/... files.

A <video>, <img> or GLTFLoader request cannot send the Authorization header, so a
private capture's media 403s in the browser even for its owner. Instead, every
/media/<session_id>/ URL the server hands to a caller who may view that session gets
``?mt=<expiry>.<signature>``. The /media handler accepts either the header (as before)
or a valid, unexpired signature for that same session id.

The signature covers only the session id and the expiry, so one token works for every
file of that capture. Deleting a capture removes its files and its library row, so an
old link stops working even before it expires.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable

PARAM = "mt"
_SID = r"[A-Za-z0-9_-]+"
_MEDIA_RE = re.compile(r"^/media/(" + _SID + r")/")
_KEY_CACHE: dict[str, bytes] = {}


def ttl_seconds() -> int:
    try:
        return max(60, int(os.environ.get("CEREBEL_MEDIA_URL_TTL_S") or 6 * 3600))
    except ValueError:
        return 6 * 3600


def _key(product_dir: Path) -> bytes:
    """CEREBEL_MEDIA_URL_SECRET, else a random key kept in the product data folder."""
    env = os.environ.get("CEREBEL_MEDIA_URL_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    cache_key = str(product_dir)
    if cache_key in _KEY_CACHE:
        return _KEY_CACHE[cache_key]
    path = Path(product_dir) / "media_url.key"
    try:
        key = path.read_bytes().strip()
    except OSError:
        key = b""
    if len(key) < 32:
        key = secrets.token_hex(32).encode("ascii")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    _KEY_CACHE[cache_key] = key
    return key


def _sig(product_dir: Path, session_id: str, exp: int) -> str:
    msg = f"{session_id}.{exp}".encode("utf-8")
    return hmac.new(_key(product_dir), msg, hashlib.sha256).hexdigest()[:32]


def make_token(product_dir: Path, session_id: str, *, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + ttl_seconds())
    return f"{exp}.{_sig(product_dir, session_id, exp)}"


def token_valid(product_dir: Path, session_id: str, token: str | None, *, now: float | None = None) -> bool:
    if not token or "." not in token or not re.fullmatch(_SID, session_id or ""):
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(sig, _sig(product_dir, session_id, exp))


def sign_url(product_dir: Path, url: str) -> str:
    m = _MEDIA_RE.match(url)
    if not m or f"{PARAM}=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{PARAM}={make_token(product_dir, m.group(1))}"


def sign_payload(product_dir: Path, payload: Any, may_view: Callable[[str], bool]) -> Any:
    """Return a copy of a JSON payload with every viewable /media/<sid>/ URL signed."""
    verdicts: dict[str, bool] = {}

    def _allowed(sid: str) -> bool:
        if sid not in verdicts:
            verdicts[sid] = bool(may_view(sid))
        return verdicts[sid]

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            m = _MEDIA_RE.match(value)
            return sign_url(product_dir, value) if m and _allowed(m.group(1)) else value
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return _walk(payload)
