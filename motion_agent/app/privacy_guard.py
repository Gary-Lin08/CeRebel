"""Check this server's configuration against what the video consent and Privacy Policy promise.

consent.PROCESSORS names every service that touches an upload and says all of them are in the
United States. Whether that is true depends on where this process runs and what it is configured
to call: the web host, the GPU backend, the language-model and embedding endpoints, the mail
server. This module reads that configuration and lists every place it differs from the
disclosure. It never calls any of those services.

    python3 -m motion_agent.app.privacy_guard     # print findings; exit 1 if there are any

CEREBEL_PRIVACY_GATE decides what the web app does with findings:
    warn     (default) log them at start-up and show them on the operator port's /api/ops
    enforce  also refuse new uploads (and coach questions, when the language model is the
             mismatch) until the configuration or the disclosure is fixed
    off      skip the check
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from motion_agent.app.consent import PROCESSORS

# Hosts this code knows how to name. Anything else is reported as unknown rather than guessed.
LLM_HOSTS: dict[str, tuple[str, str]] = {
    "api.together.xyz": ("Together AI", "United States"),
    "api.together.ai": ("Together AI", "United States"),
    "api.fireworks.ai": ("Fireworks AI", "United States"),
    "api.openai.com": ("OpenAI", "United States"),
    "dashscope.aliyuncs.com": ("Alibaba Cloud DashScope", "China (mainland)"),
    "dashscope-intl.aliyuncs.com": ("Alibaba Cloud DashScope International", "Singapore"),
}
SMTP_HOSTS: dict[str, tuple[str, str]] = {
    "mail.spacemail.com": ("SpaceMail", "United States"),
}
# CEREBEL_HOST_PROVIDER values (case-insensitive) -> processor name used in PROCESSORS.
HOST_PROVIDERS: dict[str, str] = {
    "digitalocean": "DigitalOcean",
    "modal": "Modal",
}
# The data folder of the old onethingai GPU server (its egress IP is in Shanghai; see HANDOFF.md).
LEGACY_DATA_DIR_PREFIX = "/root/CerebelServer"

GATE_MODES = ("warn", "enforce", "off")
# What each checked endpoint carries, for the messages.
_WHAT = {"LLM": "coach (language model)", "EMBEDDINGS": "knowledge-base search (embedding)", "EMAIL": "email"}


def gate_mode(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    mode = (env.get("CEREBEL_PRIVACY_GATE") or "warn").strip().lower()
    return mode if mode in GATE_MODES else "warn"


def _disclosed(kind: str) -> dict[str, str]:
    """Disclosed processor name -> country, for one kind."""
    return {p["name"]: p["country"] for p in PROCESSORS if p.get("kind") == kind}


def _host(url: str | None) -> str | None:
    if not url or not str(url).strip():
        return None
    return (urlparse(str(url).strip()).hostname or "").lower() or None


def _finding(kind: str, code: str, message: str) -> dict[str, str]:
    return {"kind": kind, "code": code, "message": message}


def _check_endpoint(kind: str, label: str, host: str | None, known: dict[str, tuple[str, str]],
                    env_name: str) -> list[dict[str, str]]:
    disclosed = _disclosed(kind)
    what = _WHAT.get(label, label.lower())
    if host is None:
        return [_finding(kind, f"{label}_UNSET", f"{env_name} is not set, so it is not known where {what} "
                                                 f"requests go. The Privacy Policy names {', '.join(disclosed) or 'nobody'}.")]
    if host not in known:
        return [_finding(kind, f"{label}_UNKNOWN", f"{env_name} points at {host}, which this check cannot place. "
                                                   f"The Privacy Policy names {', '.join(disclosed) or 'nobody'}.")]
    name, country = known[host]
    if name not in disclosed:
        return [_finding(kind, f"{label}_NOT_DISCLOSED", f"{what.capitalize()} requests go to {name} ({country}, {host}), "
                                                         f"but the Privacy Policy names {', '.join(disclosed) or 'nobody'}.")]
    if country != disclosed[name]:
        return [_finding(kind, f"{label}_COUNTRY", f"{name} at {host} is in {country}; the Privacy Policy says "
                                                   f"{disclosed[name]}.")]
    return []


def findings(env: Mapping[str, str] | None = None, data_dir: Path | str | None = None) -> list[dict[str, str]]:
    """Every difference between the configuration and the disclosure. Empty means consistent."""
    env = os.environ if env is None else env
    out: list[dict[str, str]] = []
    hosting = _disclosed("hosting")

    # 1. Where this web server and its stored videos live.
    data_dir = str(data_dir if data_dir is not None else env.get("CEREBEL_DATA_DIR") or "")
    if data_dir.startswith(LEGACY_DATA_DIR_PREFIX):
        out.append(_finding("hosting", "LEGACY_HOST",
                            f"The data folder is {data_dir}: this is the old onethingai GPU server (egress IP in "
                            f"Shanghai), not {', '.join(hosting)}. Accounts, videos and body data are stored here."))
    declared = (env.get("CEREBEL_HOST_PROVIDER") or "").strip()
    host_name = HOST_PROVIDERS.get(declared.lower(), declared) if declared else ""
    if not declared:
        out.append(_finding("hosting", "HOST_UNDECLARED",
                            "CEREBEL_HOST_PROVIDER is not set, so this check cannot confirm the server runs at "
                            f"{', '.join(hosting)} as the Privacy Policy says."))
    elif host_name not in hosting:
        out.append(_finding("hosting", "HOST_NOT_DISCLOSED",
                            f"CEREBEL_HOST_PROVIDER says this server runs at {declared}, but the Privacy Policy "
                            f"names {', '.join(hosting) or 'nobody'} for hosting."))

    # 2. Where the 3D reconstruction runs.
    gpu = _disclosed("gpu")
    backend = (env.get("CEREBEL_GPU_BACKEND") or "local").strip().lower()
    if backend == "modal":
        if "Modal" not in gpu:
            out.append(_finding("gpu", "GPU_NOT_DISCLOSED",
                                f"Videos are reconstructed on Modal, but the Privacy Policy names {', '.join(gpu) or 'nobody'}."))
    elif not host_name or host_name not in gpu:
        out.append(_finding("gpu", "GPU_LOCAL",
                            f"CEREBEL_GPU_BACKEND={backend}: videos are reconstructed on this server"
                            f"{' (' + declared + ')' if declared else ''}, but the Privacy Policy says GPU processing "
                            f"is done by {', '.join(gpu) or 'nobody'}."))

    # 3. The AI coach and its knowledge-base embeddings receive the user's questions.
    if (env.get("MOTION_AGENT_BACKEND") or "dashscope").strip().lower() not in {"mock", "test"}:
        chat_url = env.get("MOTION_AGENT_BASE_URL")
        out += _check_endpoint("llm", "LLM", _host(chat_url), LLM_HOSTS, "MOTION_AGENT_BASE_URL")
        embed_url = env.get("MOTION_AGENT_KB_EMBED_BASE_URL") or chat_url
        if _host(embed_url) != _host(chat_url):
            out += _check_endpoint("llm", "EMBEDDINGS", _host(embed_url), LLM_HOSTS, "MOTION_AGENT_KB_EMBED_BASE_URL")

    # 4. "Your analysis is ready" emails carry the user's address.
    smtp = (env.get("CEREBEL_SMTP_HOST") or "").strip().lower()
    if smtp:
        out += _check_endpoint("email", "EMAIL", smtp, SMTP_HOSTS, "CEREBEL_SMTP_HOST")
    return out


def blocks_uploads(found: list[dict[str, Any]], env: Mapping[str, str] | None = None) -> bool:
    return gate_mode(env) == "enforce" and bool(found)


def blocks_coach(found: list[dict[str, Any]], env: Mapping[str, str] | None = None) -> bool:
    return gate_mode(env) == "enforce" and any(f.get("kind") == "llm" for f in found)


UPLOADS_PAUSED = "New uploads are paused for maintenance. Nothing was uploaded and it didn't use one of your analyses."
COACH_PAUSED = "The coach is offline for maintenance. Your report and measurements still work."


def report(env: Mapping[str, str] | None = None, data_dir: Path | str | None = None) -> dict[str, Any]:
    found = findings(env, data_dir)
    return {
        "ok": not found,
        "gate": gate_mode(env),
        "uploads_blocked": blocks_uploads(found, env),
        "coach_blocked": blocks_coach(found, env),
        "findings": found,
    }


def main(argv: list[str] | None = None) -> int:
    from motion_agent.app.config import load_settings

    result = report(data_dir=load_settings().data_dir)
    if result["ok"]:
        print("privacy check: configuration matches the Privacy Policy's processor list")
        return 0
    print(f"privacy check: {len(result['findings'])} difference(s) from the Privacy Policy "
          f"(CEREBEL_PRIVACY_GATE={result['gate']}):")
    for item in result["findings"]:
        print(f"  [{item['kind']}] {item['code']}: {item['message']}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
