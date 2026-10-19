"""Tool registry: schemas + dispatch in one place for the VLM adapter."""

from __future__ import annotations

from typing import Any, Callable

from motion_agent.llm.schemas import openai_tools
from motion_agent.motion.sequence import MotionSequence
from motion_agent.tools.motion_tools import build_tool_map, execute_tool

__all__ = ["openai_tools", "build_tool_map", "execute_tool", "tool_names"]


def tool_names() -> list[str]:
    return [t["function"]["name"] for t in openai_tools()]


def bound_tools(seq: MotionSequence) -> dict[str, Callable[..., dict[str, Any]]]:
    return build_tool_map(seq)
