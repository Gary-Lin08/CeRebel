#!/usr/bin/env python3
"""Check the coach's language-model settings in .env.product without printing the key.

    python3 deploy/check_llm.py            # list matching models on the provider and try one call
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env.product"


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV.is_file():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.split("#", 1)[0].strip().strip("'\"")
    return out


def main() -> int:
    env = {**load_env(), **{k: v for k, v in os.environ.items() if k.startswith("MOTION_AGENT_")}}
    base = env.get("MOTION_AGENT_BASE_URL", "").rstrip("/")
    key = env.get("MOTION_AGENT_API_KEY", "")
    if not base or not key:
        print("Set MOTION_AGENT_BASE_URL and MOTION_AGENT_API_KEY in .env.product first.")
        return 1
    req = urllib.request.Request(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    rows = data if isinstance(data, list) else data.get("data", [])
    ids = sorted(str(r.get("id")) for r in rows)
    print("Qwen chat models:", [i for i in ids if "qwen" in i.lower() and "embed" not in i.lower()][:20])
    print("Embedding models:", [i for i in ids if "embed" in i.lower() or "bge" in i.lower() or "e5" in i.lower()][:20])
    model = env.get("MOTION_AGENT_MODEL")
    if not model:
        print("\nPick one chat model id above and set MOTION_AGENT_MODEL, then run again.")
        return 0
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "Reply with: ok"}], "max_tokens": 5}).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        reply = json.loads(resp.read().decode())
    print(f"\nTest call to {model}:", reply["choices"][0]["message"]["content"], "| usage:", reply.get("usage"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
