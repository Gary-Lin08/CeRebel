"""Adapter placeholder — CeRebel sessions already expose OpenSim-compatible state.

Future estimator swaps should implement `adapt_estimator_output(...)` here and
emit data consumable by `load_motion_sequence`.
"""

from __future__ import annotations

from pathlib import Path

from motion_agent.motion.sequence import MotionSequence, load_motion_sequence


def adapt_session(session_dir: Path | str) -> MotionSequence:
    """Load a finished session into the canonical MotionSequence representation."""
    return load_motion_sequence(session_dir)
