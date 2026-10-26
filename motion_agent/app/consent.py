"""Video-processing consent and data-retention settings shared by the API, UI and retention job.

The consent text names every processor that touches an upload, says where they are, and
states the retention periods. Change VIDEO_CONSENT_VERSION whenever the text, the processor
list or the purpose changes, so every account is asked again before its next upload.
"""

from __future__ import annotations

import os
from typing import Any

# Bump on any change to the text below, the processors, or what is done with the video.
VIDEO_CONSENT_VERSION = "2026-10-24-us"

DEFAULT_RAW_VIDEO_RETENTION_DAYS = 30
DEFAULT_INACTIVE_DELETE_DAYS = 730

# Every service that receives upload-derived or account data. All in the United States.
# The single source for the consent dialog, the consent text and the Privacy Policy table
# (section 6). `kind` is what privacy_guard checks the server's configuration against:
# hosting = web server and storage, gpu = video reconstruction, llm = AI coach and its
# knowledge-base search, payments, network, email. `role` is the consent dialog's wording,
# `policy_role` the Privacy Policy's.
PROCESSORS: list[dict[str, str]] = [
    {"name": "DigitalOcean", "kind": "hosting", "country": "United States",
     "role": "web server and storage of videos and results (US East)",
     "policy_role": "Web server and storage of accounts, videos and results (US East data center)"},
    {"name": "Modal", "kind": "gpu", "country": "United States",
     "role": "GPU processing of uploaded videos",
     "policy_role": "GPU processing of each uploaded video in a temporary workspace that is discarded when the job ends"},
    {"name": "Together AI", "kind": "llm", "country": "United States",
     "role": "AI coach; receives your questions and computed numbers, not video frames",
     "policy_role": "AI coach: receives your question and the computed numbers, not your video or its frames"},
    {"name": "Stripe", "kind": "payments", "country": "United States",
     "role": "payments",
     "policy_role": "Payments and subscriptions"},
    {"name": "Cloudflare", "kind": "network", "country": "United States",
     "role": "DNS and network delivery",
     "policy_role": "DNS and network delivery between your device and our server"},
    {"name": "SpaceMail", "kind": "email", "country": "United States",
     "role": "account and analysis-ready emails",
     "policy_role": 'Account and "analysis ready" emails'},
]


def processors_of(kind: str) -> list[dict[str, str]]:
    return [p for p in PROCESSORS if p.get("kind") == kind]


def _env_days(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip()) if raw is not None and str(raw).strip() else default
    except ValueError:
        return default
    return value if value > 0 else default


def raw_video_retention_days() -> int:
    """Days after processing before the original upload and its frames are deleted."""
    return _env_days("CEREBEL_RAW_VIDEO_RETENTION_DAYS", DEFAULT_RAW_VIDEO_RETENTION_DAYS)


def inactive_delete_days() -> int:
    """Days after the last interaction before an account and all its data are deleted."""
    return _env_days("CEREBEL_INACTIVE_DELETE_DAYS", DEFAULT_INACTIVE_DELETE_DAYS)


def period_label(days: int) -> str:
    """730 -> '24 months', 30 -> '30 days'."""
    months = days / 30.4167
    if days >= 60 and abs(months - round(months)) < 0.05:
        n = int(round(months))
        return f"{n} month{'s' if n != 1 else ''}"
    return f"{days} day{'s' if days != 1 else ''}"


def video_consent_text() -> str:
    raw_days = raw_video_retention_days()
    inactive = period_label(inactive_delete_days())
    names = ", ".join(p["name"] for p in processors_of("hosting") + processors_of("gpu"))
    return (
        "I agree that CeRebel may process the videos I upload to build a 3D model of my body "
        "(skeleton and body shape), and to compute joint angles and estimated joint loads for "
        f"swing coaching. Processing and storage happen only in the United States, by {names} "
        "on CeRebel's behalf. The AI coach (Together AI, US) receives my questions and the computed "
        "numbers, not my video. "
        f"My original video and its frames are deleted {raw_days} days after processing; the 3D "
        f"model and measurements are kept until I delete them or my account, and are deleted "
        f"automatically {inactive} after my last activity. CeRebel does not use my video or "
        "body data to identify me, does not build face templates, does not sell my data, and "
        "does not give medical diagnoses. I can withdraw this consent at any time by deleting my "
        "videos or my account."
    )


def processor_rows_html() -> str:
    """The Privacy Policy's section 6 table rows."""
    from html import escape

    return "\n".join(
        f"<tr><td>{escape(p['name'])}</td><td>{escape(p['policy_role'], quote=False)}</td>"
        f"<td>{escape(p['country'])}</td></tr>"
        for p in PROCESSORS
    )


def public_video_consent(accepted_version: str | None = None, accepted_at: str | None = None,
                         accepted_retention_days: int | None = None) -> dict[str, Any]:
    """What the browser needs to show the consent and decide whether to ask."""
    from motion_agent.app.accounts import video_consent_is_current

    return {
        "version": VIDEO_CONSENT_VERSION,
        "required": not video_consent_is_current(accepted_version, accepted_retention_days),
        "accepted_version": accepted_version,
        "accepted_at": accepted_at,
        "text": video_consent_text(),
        "raw_video_retention_days": raw_video_retention_days(),
        "inactive_delete_days": inactive_delete_days(),
        "processors": [{k: p[k] for k in ("name", "role", "country")} for p in PROCESSORS],
        "privacy_url": "/privacy",
    }
