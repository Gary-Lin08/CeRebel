"""Plans, monthly analysis quota and Stripe subscriptions for the consumer beta.

Everything Stripe-specific is read from the environment on the server:

    STRIPE_SECRET_KEY        sk_live_... / sk_test_...   (required for checkout)
    STRIPE_WEBHOOK_SECRET    whsec_...                    (required for the webhook)
    STRIPE_PRICE_ANNUAL      price id of the $99.99/year plan
    STRIPE_PRICE_MONTHLY     price id of the monthly plan (optional; hidden when unset)
    STRIPE_FOUNDING_COUPON   coupon id that turns the first year into $69 (optional)
    CEREBEL_FOUNDING_LIMIT   how many founding-member checkouts are offered (default 200)
    CEREBEL_PUBLIC_URL       e.g. https://app.cerebel.ai — used for checkout return URLs
    CEREBEL_FREE_MONTHLY     free analyses per calendar month (default 3)
    CEREBEL_PRO_MONTHLY      fair-use cap for Pro per month (default 60)

Only stdlib is used so the server needs no new dependency. The secret key never
leaves the server; the browser only ever receives a Stripe-hosted checkout URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

# Captures in these states never count against the quota: the upload failed,
# the swing could not be read, or the user deleted it before it finished.
_NOT_COUNTED = {"error", "no_swing", "deleted", "rejected"}
_ACTIVE_STATUSES = {"active", "trialing"}


class BillingError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def free_monthly_limit() -> int:
    return _env_int("CEREBEL_FREE_MONTHLY", 3)


def pro_monthly_limit() -> int:
    return _env_int("CEREBEL_PRO_MONTHLY", 60)


def founding_limit() -> int:
    return _env_int("CEREBEL_FOUNDING_LIMIT", 200)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- storage


def _path(product_dir: Path) -> Path:
    product_dir.mkdir(parents=True, exist_ok=True)
    return product_dir / "billing.json"


def _read(product_dir: Path) -> dict[str, Any]:
    path = _path(product_dir)
    if not path.is_file():
        return {"users": {}, "events": [], "founding_used": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"users": {}, "events": [], "founding_used": 0}
    data.setdefault("users", {})
    data.setdefault("events", [])
    data.setdefault("founding_used", 0)
    return data


def _write(product_dir: Path, data: dict[str, Any]) -> None:
    path = _path(product_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def subscription_for(product_dir: Path, user_id: str) -> dict[str, Any]:
    return dict(_read(product_dir)["users"].get(user_id) or {})


def is_pro(product_dir: Path, user_id: str) -> bool:
    row = subscription_for(product_dir, user_id)
    if row.get("status") not in _ACTIVE_STATUSES:
        return False
    end = row.get("current_period_end")
    if isinstance(end, (int, float)) and end > 0:
        # One day of grace so a renewal webhook arriving late never locks a payer out.
        return time.time() < end + 86_400
    return True


# ---------------------------------------------------------------- quota


def _month_key(iso: str | None) -> str:
    return str(iso or "")[:7]


def analyses_this_month(captures: list[dict[str, Any]], now: datetime | None = None) -> int:
    month = (now or _now()).strftime("%Y-%m")
    return sum(
        1
        for row in captures
        if _month_key(row.get("created_at")) == month
        and str(row.get("status") or "processing") not in _NOT_COUNTED
        and not row.get("deleted")
    )


def quota(product_dir: Path, user_id: str, captures: list[dict[str, Any]]) -> dict[str, Any]:
    pro = is_pro(product_dir, user_id)
    limit = pro_monthly_limit() if pro else free_monthly_limit()
    used = analyses_this_month(captures)
    return {
        "plan": "pro" if pro else "free",
        "monthly_limit": limit,
        "used_this_month": used,
        "remaining": max(0, limit - used),
        "resets": "the 1st of each month (UTC)",
    }


# ---------------------------------------------------------------- plans shown to the browser


def public_plans(product_dir: Path) -> dict[str, Any]:
    data = _read(product_dir)
    founding_left = max(0, founding_limit() - int(data.get("founding_used") or 0))
    plans = []
    if _env("STRIPE_PRICE_ANNUAL"):
        if _env("STRIPE_FOUNDING_COUPON") and founding_left > 0:
            plans.append({
                "id": "founding",
                "label": "Founding member",
                "price": "$69 first year",
                "detail": f"then $99.99/year · {founding_left} spots left",
            })
        plans.append({"id": "annual", "label": "Pro annual", "price": "$99.99/year", "detail": "about $8.33/month"})
    if _env("STRIPE_PRICE_MONTHLY"):
        plans.append({"id": "monthly", "label": "Pro monthly", "price": "$14.99/month", "detail": "cancel anytime"})
    return {
        "checkout_enabled": bool(_env("STRIPE_SECRET_KEY") and plans),
        "plans": plans,
        "free_monthly": free_monthly_limit(),
        "pro_monthly": pro_monthly_limit(),
    }


# ---------------------------------------------------------------- Stripe API (stdlib)


def _stripe_request(method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    key = _env("STRIPE_SECRET_KEY")
    if not key:
        raise BillingError("Payments are not switched on yet.", 503)
    body = urllib.parse.urlencode(params or {}, doseq=True).encode() if params else None
    req = urllib.request.Request(f"https://api.stripe.com/v1/{path}", data=body, method=method)
    auth = base64.b64encode(f"{key}:".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message")
        except Exception:  # noqa: BLE001
            detail = None
        raise BillingError(detail or "Payment provider error.", 502) from exc
    except urllib.error.URLError as exc:
        raise BillingError("Payment provider unreachable.", 502) from exc


def _public_url() -> str:
    return _env("CEREBEL_PUBLIC_URL", "http://localhost:8092").rstrip("/")


def create_checkout(product_dir: Path, user: dict[str, Any], plan: str) -> dict[str, Any]:
    if is_pro(product_dir, user["user_id"]):
        raise BillingError("You already have Pro. Use Manage subscription to change plans.", 409)
    price = _env("STRIPE_PRICE_MONTHLY") if plan == "monthly" else _env("STRIPE_PRICE_ANNUAL")
    if plan not in {"annual", "monthly", "founding"} or not price:
        raise BillingError("That plan is not available.")
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": 1,
        "client_reference_id": user["user_id"],
        "metadata[user_id]": user["user_id"],
        "metadata[plan]": plan,
        "subscription_data[metadata][user_id]": user["user_id"],
        "subscription_data[metadata][plan]": plan,
        "success_url": f"{_public_url()}/?checkout=success",
        "cancel_url": f"{_public_url()}/?checkout=cancel",
    }
    coach = str(user.get("coach_code") or "")
    if coach:
        params["metadata[coach_code]"] = coach
        params["subscription_data[metadata][coach_code]"] = coach
    existing = subscription_for(product_dir, user["user_id"]).get("customer")
    if existing:
        params["customer"] = existing
    elif user.get("email"):
        params["customer_email"] = user["email"]
    if plan == "founding":
        data = _read(product_dir)
        coupon = _env("STRIPE_FOUNDING_COUPON")
        if not coupon or int(data.get("founding_used") or 0) >= founding_limit():
            raise BillingError("Founding spots are gone. Pro annual is still available.", 409)
        params["discounts[0][coupon]"] = coupon
    else:
        params["allow_promotion_codes"] = "true"
    session = _stripe_request("POST", "checkout/sessions", params)
    return {"ok": True, "url": session.get("url"), "id": session.get("id")}


def create_portal(product_dir: Path, user: dict[str, Any]) -> dict[str, Any]:
    customer = subscription_for(product_dir, user["user_id"]).get("customer")
    if not customer:
        raise BillingError("No subscription found for this account.", 404)
    session = _stripe_request(
        "POST", "billing_portal/sessions", {"customer": customer, "return_url": f"{_public_url()}/"}
    )
    return {"ok": True, "url": session.get("url")}


# ---------------------------------------------------------------- webhook


def verify_signature(payload: bytes, header: str | None, secret: str, tolerance_s: int = 300) -> bool:
    """Check Stripe's `Stripe-Signature` header (t=…, v1=…) against the raw body."""
    if not header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts.get("t", ["0"])[0])
    except ValueError:
        return False
    if abs(time.time() - timestamp) > tolerance_s:
        return False
    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


def _period_end(sub: dict[str, Any]) -> int | None:
    end = sub.get("current_period_end")
    if not end:
        items = (sub.get("items") or {}).get("data") or []
        if items:
            end = items[0].get("current_period_end")
    return int(end) if end else None


def handle_webhook(product_dir: Path, payload: bytes, signature: str | None) -> dict[str, Any]:
    secret = _env("STRIPE_WEBHOOK_SECRET")
    if not verify_signature(payload, signature, secret):
        raise BillingError("Invalid signature.", 400)
    event = json.loads(payload.decode("utf-8"))
    event_id = str(event.get("id") or "")
    kind = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    changed: str | None = None
    with _LOCK:
        data = _read(product_dir)
        if event_id and event_id in data["events"]:
            return {"ok": True, "duplicate": True}
        if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
            user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id")
            if user_id:
                row = data["users"].setdefault(user_id, {})
                row.update({
                    "customer": obj.get("customer"),
                    "subscription": obj.get("subscription"),
                    "status": "active",
                    "plan": (obj.get("metadata") or {}).get("plan"),
                    "updated_at": _now().isoformat(),
                })
                if (obj.get("metadata") or {}).get("plan") == "founding":
                    data["founding_used"] = int(data.get("founding_used") or 0) + 1
                changed = user_id
        elif kind in {
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        }:
            user_id = (obj.get("metadata") or {}).get("user_id")
            if not user_id:
                user_id = next(
                    (uid for uid, row in data["users"].items() if row.get("subscription") == obj.get("id")),
                    None,
                )
            if user_id:
                row = data["users"].setdefault(user_id, {})
                row.update({
                    "customer": obj.get("customer") or row.get("customer"),
                    "subscription": obj.get("id"),
                    "status": "canceled" if kind.endswith("deleted") else obj.get("status"),
                    "current_period_end": _period_end(obj),
                    "cancel_at_period_end": bool(obj.get("cancel_at_period_end")),
                    "updated_at": _now().isoformat(),
                })
                changed = user_id
        if event_id:
            data["events"] = (data["events"] + [event_id])[-500:]
        _write(product_dir, data)
    return {"ok": True, "type": kind, "user_id": changed}
