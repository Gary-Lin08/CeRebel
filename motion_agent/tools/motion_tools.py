"""Unified tool dispatch for the motion agent."""

from __future__ import annotations

from typing import Any, Callable

from motion_agent.dynamics.joint_dynamics import (
    get_dynamics_summary,
    get_ground_reaction_forces,
    get_joint_dynamics,
    get_joint_power,
)
from motion_agent.motion.events import get_motion_events
from motion_agent.motion.golf_phases import segment_golf_swings
from motion_agent.motion.sport_analysis import build_sport_analysis
from motion_agent.motion.kinematics import (
    get_com_trajectory,
    get_contact_state,
    get_joint_trajectory,
    get_motion_state,
)
from motion_agent.motion.sequence import MotionSequence
from motion_agent.storage.errors import tool_error
from motion_agent.rendering.dynamics_renderer import render_force_visualization
from motion_agent.tools.comparison_tools import compare_motion, search_motion
from motion_agent.tools.visual_tools import get_rgb_clip, get_rgb_frame
from motion_agent.knowledge.tool import list_knowledge_sources, search_knowledge_base

ToolFn = Callable[..., dict[str, Any]]


def _wrap(seq: MotionSequence, fn: ToolFn) -> Callable[..., dict[str, Any]]:
    def _inner(**kwargs: Any) -> dict[str, Any]:
        try:
            return fn(seq, **kwargs)
        except TypeError as exc:
            return tool_error("INVALID_ARGUMENT", str(exc))
        except Exception as exc:  # noqa: BLE001 — surface to VLM as structured error
            return tool_error("TOOL_EXECUTION_FAILED", f"{type(exc).__name__}: {exc}")

    return _inner


def build_tool_map(seq: MotionSequence) -> dict[str, Callable[..., dict[str, Any]]]:
    return {
        "get_motion_state": _wrap(seq, get_motion_state),
        "get_motion_events": _wrap(seq, get_motion_events),
        "segment_golf_swings": _wrap(seq, segment_golf_swings),
        "build_sport_analysis": _wrap(seq, build_sport_analysis),
        "get_joint_trajectory": _wrap(seq, get_joint_trajectory),
        "get_com_trajectory": _wrap(seq, get_com_trajectory),
        "get_contact_state": _wrap(seq, get_contact_state),
        "get_dynamics_summary": _wrap(seq, get_dynamics_summary),
        "get_joint_dynamics": _wrap(seq, get_joint_dynamics),
        "get_ground_reaction_forces": _wrap(seq, get_ground_reaction_forces),
        "get_joint_power": _wrap(seq, get_joint_power),
        "get_rgb_frame": _wrap(seq, get_rgb_frame),
        "get_rgb_clip": _wrap(seq, get_rgb_clip),
        "compare_motion": _wrap(seq, compare_motion),
        "search_motion": _wrap(seq, search_motion),
        "render_force_visualization": _wrap(seq, render_force_visualization),
        "search_knowledge_base": _wrap(seq, search_knowledge_base),
        "list_knowledge_sources": _wrap(seq, list_knowledge_sources),
    }


def execute_tool(seq: MotionSequence, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    tools = build_tool_map(seq)
    if name not in tools:
        return tool_error("UNKNOWN_TOOL", f"Unknown tool: {name}")
    return tools[name](**(arguments or {}))
