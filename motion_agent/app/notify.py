"""Email the user when an analysis finishes. SMTP settings come from the environment.

    CEREBEL_SMTP_HOST, CEREBEL_SMTP_PORT (465 = SSL, otherwise STARTTLS),
    CEREBEL_SMTP_USER, CEREBEL_SMTP_PASSWORD, CEREBEL_MAIL_FROM

With no CEREBEL_SMTP_HOST set, nothing is sent. Failures are logged, never raised:
a missing email must not break the analysis.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

LOG = logging.getLogger("motion_agent.notify")


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else default


def enabled() -> bool:
    return bool(_env("CEREBEL_SMTP_HOST") and _env("CEREBEL_MAIL_FROM"))


def build_message(to: str, status: str, session_id: str, detail: str = "") -> EmailMessage:
    base = _env("CEREBEL_PUBLIC_URL", "http://localhost:8092").rstrip("/")
    link = f"{base}/?session={session_id}"
    msg = EmailMessage()
    msg["From"] = _env("CEREBEL_MAIL_FROM")
    msg["To"] = to
    if status == "ready":
        msg["Subject"] = "Your swing analysis is ready"
        body = (
            "Your CeRebel swing analysis is ready.\n\n"
            f"See your 3D swing and the one fix that matters most:\n{link}\n\n"
            "Numbers are estimates from a single phone video (beta).\n"
        )
    else:
        msg["Subject"] = "We couldn't analyse your last clip"
        body = (
            "We couldn't finish the analysis of your last clip"
            + (f": {detail}" if detail else ".")
            + "\n\nIt did not use one of your monthly analyses. Tips for a clip that works:\n"
            "- Whole body and club in frame, phone still at hip height\n"
            "- One swing per clip, slow-motion if your phone has it\n\n"
            f"Try again: {base}/\n"
        )
    msg.set_content(body + "\n— CeRebel\n")
    return msg


def _send(msg: EmailMessage) -> None:
    host = _env("CEREBEL_SMTP_HOST")
    port = int(_env("CEREBEL_SMTP_PORT", "465"))
    user = _env("CEREBEL_SMTP_USER")
    password = os.environ.get("CEREBEL_SMTP_PASSWORD") or ""
    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.starttls(context=context)
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except Exception:  # noqa: BLE001
        LOG.exception("analysis email failed to=%s", msg.get("To"))


def notify_analysis(to: str | None, status: str, session_id: str, detail: str = "") -> bool:
    """Send in the background. Returns True when an email was queued."""
    if not to or not enabled():
        return False
    msg = build_message(to, status, session_id, detail)
    threading.Thread(target=_send, args=(msg,), name=f"mail-{session_id[:8]}", daemon=True).start()
    return True
