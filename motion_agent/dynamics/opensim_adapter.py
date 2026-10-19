"""Thin OpenSim-facing aliases kept as a stable dynamics boundary."""

from __future__ import annotations

from motion_agent.dynamics.joint_dynamics import (  # noqa: F401
    POWER_CONVENTION,
    get_dynamics_summary,
    get_ground_reaction_forces,
    get_joint_dynamics,
    get_joint_power,
)
from motion_agent.motion.adapter import adapt_session as load_opensim_session  # noqa: F401
