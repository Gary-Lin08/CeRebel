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
PROCESSORS: list[dict[str, str]] = [
    {"name": "DigitalOcean", "role": "web server and storage of videos and results (US East)", "country": "United States"},
    {"name": "Modal", "role": "GPU processing of uploaded videos", "country": "United States"},
    {"name": "Together AI", "role": "AI coach; receives your questions and computed numbers, not video frames", "country": "United States"},
    {"name": "Stripe", "role": "payments", "country": "United States"},
    {"name": "Cloudflare", "role": "DNS and network delivery", "country": "United States"},
    {"name": "SpaceMail", "role": "account and analysis-ready emails", "country": "United States"},
]


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
    names = ", ".join(p["name"] for p in PROCESSORS if p["name"] in {"DigitalOcean", "Modal"})
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
        "processors": PROCESSORS,
        "privacy_url": "/privacy",
    }
