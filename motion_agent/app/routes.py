"""HTTP route helpers for the standalone demo server."""

from __future__ import annotations

import json
from typing import Any

from motion_agent.llm.agent import AgentResult

EXAMPLE_QUESTIONS = [
    "What is the subject's posture at 3.2 seconds?",
    "What changes between 3 and 4 seconds?",
    "What is the person doing with their right hand?",
    "Which joints are generating the most mechanical power during the jump?",
    "Why do you conclude this is a jump rather than simply standing up?",
    "Does the reconstructed SMPL-X motion agree with what we see in the video?",
    "What happens mechanically immediately before the feet leave the ground?",
]


def trace_to_dict(result: AgentResult) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in result.trace:
        item = {
            "kind": step.kind,
            "name": step.name,
            "arguments": step.arguments,
            "result_summary": step.result_summary,
            "content": step.content,
            "interim": step.interim,
        }
        if step.image_base64:
            item["mime_type"] = step.mime_type or "image/png"
            item["image_base64"] = step.image_base64
        out.append(item)
    return out


def sse_pack(event: dict[str, Any]) -> bytes:
    """Encode one Server-Sent Event data frame."""
    payload = json.dumps(event, default=str, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


def chat_payload(result: AgentResult) -> dict[str, Any]:
    return {
        "answer": result.answer,
        "session_id": result.session_id,
        "tool_calls_used": result.tool_calls_used,
        "trace": trace_to_dict(result),
        "error": result.error,
    }
