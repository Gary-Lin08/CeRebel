"""Skeleton / mesh rendering entry points.

Full GEM-X SOMA mesh rendering lives in the existing Viser service. This module
exposes a replaceable adapter so the agent can request a spatial view without
coupling to a particular mesh backend.
"""

from __future__ import annotations

from typing import Any

from motion_agent.motion.sequence import MotionSequence
from motion_agent.rendering.dynamics_renderer import render_force_visualization


def render_skeleton(seq: MotionSequence, timestamp: float, **kwargs: Any) -> dict[str, Any]:
    """Render the reconstructed body at a timestamp (OpenSim stick figure)."""
    return render_force_visualization(seq, timestamp, **kwargs)
