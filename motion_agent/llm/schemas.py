"""JSON schemas and descriptions for VLM tool calling."""

from __future__ import annotations

from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_motion_state",
            "description": (
                "Returns the reconstructed physical state of the human at a specified "
                "timestamp. Use when precise body configuration, joint motion, "
                "center-of-mass state, or foot contact is required. Numerical values "
                "come from the deterministic OpenSim/GEM-X backend. Do NOT infer "
                "numerical measurements from RGB when this tool can provide them. "
                "Angles are continuously unwrapped; for aerial flips prefer "
                "root_euler_continuous_deg / root_cumulative_rotation_deg (WORLD) for "
                "extent, and intrinsic_somersault_* (+backflip / −frontflip) for "
                "front vs back — not pelvis_tilt (stalls near ±180°) and not world "
                "Euler sign alone. "
                "Units: metres, radians/degrees, seconds from video start."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timestamp": {
                        "type": "number",
                        "description": "Time in seconds from video start.",
                    },
                    "temporal_window": {
                        "type": "number",
                        "description": "Optional window width (seconds) for local context.",
                    },
                    "joints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional SMPL-X joint names or aliases (e.g. right_knee, knee_r).",
                    },
                },
                "required": ["timestamp"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_motion_events",
            "description": (
                "Find temporally meaningful kinematic/contact events in an interval "
                "(knee extrema, contact on/off, jump/landing candidates, COM peaks). "
                "Deterministic heuristics — not a learned classifier."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "segment_golf_swings",
            "description": (
                "GOLF-ONLY timeline tool. After you have reasoned about HOW MANY "
                "distinct full swings are in the clip, call this to emit ordered "
                "phases (address, takeaway, backswing, transition, downswing, "
                "impact, follow-through) with timestamps. The chat UI renders "
                "clickable phase cards. ALWAYS pass swing_count from your "
                "reasoning (1 for a single-swing clip). Do not split one swing's "
                "backswing vs downswing into two swings. Prefer this over "
                "get_motion_events for golf sequencing. Deterministic wrist-speed "
                "heuristics — not club/ball tracking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "swing_count": {
                        "type": "integer",
                        "description": (
                            "Exact number of distinct full swings to segment, from "
                            "your prior reasoning (required for best results). "
                            "Use 1 unless the video clearly shows multiple separate swings."
                        ),
                    },
                    "start_time": {
                        "type": "number",
                        "description": "Optional window start (seconds). Default 0.",
                    },
                    "end_time": {
                        "type": "number",
                        "description": "Optional window end (seconds). Default full clip.",
                    },
                    "handedness": {
                        "type": "string",
                        "enum": ["auto", "right", "left"],
                        "description": (
                            "Player handedness for lead/trail labeling. Default and "
                            "auto are right-handed (lead=left, trail=right). Pass "
                            "left only when the user explicitly says the player is "
                            "left-handed — do not infer from wrist speed."
                        ),
                    },
                    "max_swings": {
                        "type": "integer",
                        "description": "Hard cap on swings (1–12). Prefer swing_count instead.",
                    },
                },
                "required": ["swing_count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_sport_analysis",
            "description": (
                "GOLF and BASEBALL figure pack. Returns the phase timeline plus "
                "downsampled labeled traces (knee flex, hip–shoulder separation, "
                "pelvis turn or stride, hand speed) and landmark readings. The chat "
                "UI draws these as the motion board. Cite only these numbers. Call "
                "it when the server has not already injected a sport_motion_board "
                "result. Deterministic reconstruction — not club or ball tracking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sport": {
                        "type": "string",
                        "enum": ["golf", "baseball"],
                        "description": "Which movement the figures are for.",
                    },
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                    "handedness": {
                        "type": "string",
                        "enum": ["auto", "right", "left"],
                        "description": "Default and auto are right-handed (lead=left, trail=right).",
                    },
                    "swing_index": {
                        "type": "integer",
                        "description": "Golf only: which segmented swing to measure (0-based).",
                    },
                },
                "required": ["sport"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_joint_trajectory",
            "description": (
                "Return joint trajectory statistics and downsampled time series. "
                "Use for questions about how a joint changes over time. "
                "DOF angles are continuously unwrapped. For pelvis / flips, inspect "
                "root_euler_*_continuous_deg and root_cumulative_rotation_deg (WORLD "
                "extent) plus intrinsic_somersault_* / flip_direction_hint "
                "(front vs back). Do not trust pelvis_tilt alone through ±180°."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "joint": {"type": "string"},
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                    "quantities": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "position",
                                "velocity",
                                "acceleration",
                                "angle",
                                "angular_velocity",
                                "angular_acceleration",
                            ],
                        },
                    },
                    "include_plot": {
                        "type": "boolean",
                        "description": "If true, also return a PNG plot of the primary angle series.",
                    },
                },
                "required": ["joint", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_com_trajectory",
            "description": (
                "Center-of-mass position/velocity/acceleration over an interval, "
                "including peak vertical velocity/acceleration times."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                    "include_plot": {
                        "type": "boolean",
                        "description": "If true, return a PNG of COM height and vertical velocity.",
                    },
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_contact_state",
            "description": (
                "Left/right foot contact intervals and transitions. Contacts are "
                "estimated (not force-plate truth)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dynamics_summary",
            "description": (
                "High-level OpenSim inverse-dynamics summary: largest moments, "
                "positive/negative power, peak GRF. Use for mechanical loading, "
                "propulsion, and absorption questions. Do not invent roles when "
                "magnitudes are small. Values are estimated GRF-based dynamics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_joint_dynamics",
            "description": (
                "Per-joint moment / reaction force / power time series from OpenSim. "
                "Check returned sign conventions before anatomical interpretation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "joint": {"type": "string"},
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                    "quantities": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["moment", "reaction_force", "power"],
                        },
                    },
                },
                "required": ["joint", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ground_reaction_forces",
            "description": "Estimated left/right/total ground reaction forces over an interval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_joint_power",
            "description": (
                "Mechanical power P = tau * omega for lower-limb joints. "
                "Positive ≈ generation along flexion DOF; negative ≈ absorption. "
                "Confirm convention in the response before claiming causation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                    "joints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rgb_frame",
            "description": (
                "Retrieve the RGB frame at a timestamp for visual appearance, scene "
                "context, object interaction, occlusion, or visual confirmation. "
                "Do NOT use as the authoritative source for numerical joint angles."
            ),
            "parameters": {
                "type": "object",
                "properties": {"timestamp": {"type": "number"}},
                "required": ["timestamp"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rgb_clip",
            "description": (
                "Retrieve a short RGB clip (≤8 s) as frames for temporal visual context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "number"},
                    "end_time": {"type": "number"},
                    "target_fps": {"type": "number"},
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_motion",
            "description": (
                "Compare two temporal segments of the current video for kinematic "
                "and dynamics differences."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "segment_a": {
                        "type": "object",
                        "properties": {
                            "start_time": {"type": "number"},
                            "end_time": {"type": "number"},
                        },
                        "required": ["start_time", "end_time"],
                    },
                    "segment_b": {
                        "type": "object",
                        "properties": {
                            "start_time": {"type": "number"},
                            "end_time": {"type": "number"},
                        },
                        "required": ["start_time", "end_time"],
                    },
                    "joints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["segment_a", "segment_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_motion",
            "description": (
                "Deterministic heuristic search (jumps, deep squats, hand above head). "
                "Not semantic RAG. Use when the user asks to find times of a motion pattern."
            ),
            "parameters": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_force_visualization",
            "description": (
                "Render a sagittal OpenSim skeleton overlay at a timestamp with estimated "
                "GRF arrows, joint-moment disks, COM, and contact markers. Use to spatially "
                "ground dynamics together with RGB. Do NOT read numerical angles/forces from "
                "the image; query dynamics tools for numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "number"},
                    "include_com_trail": {"type": "boolean"},
                },
                "required": ["timestamp"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Local RAG over on-disk sport knowledge .txt packs (snowboarding/FIS, "
                "golf Rules/Handicap/Equipment/Biomechanics packs, shared pipeline "
                "notes, and any other corpus folders). Returns only the top-k relevant "
                "passages — whole files are NOT uploaded into the model context. MUST "
                "use for competition rules, judging/glossary definitions, points/"
                "quotas, handicapping, equipment conformity, and uncertain technique "
                "wording. Prefer filtering with sources= to the active sport using "
                "directory prefixes or globs (e.g. fis/, snowboard_*.txt, golf/, "
                "golf/Rules/, golf/Biomechanics/, golf_*.txt). In golf/snowboarding "
                "modes the server defaults sources to that sport's corpus when omitted. "
                "Cite source filenames from the hits. Do not use this for numeric joint "
                "angles/forces (use motion tools)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of passages to return (1–12, default 4).",
                    },
                    "min_score": {
                        "type": "number",
                        "description": "Minimum cosine similarity (0–1). Default 0.15.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional filter: corpus-relative paths, directory "
                            "prefixes (golf/, golf/Rules/), or globs (golf_*.txt). "
                            "From list_knowledge_sources."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_knowledge_sources",
            "description": (
                "List local knowledge-base .txt sources and chunk counts. "
                "Use before search_knowledge_base when you need to know what docs exist."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# Tools that return pixels from the user's own video. Plots and the force
# render are drawn from measurements and stay available.
RGB_TOOL_NAMES = frozenset({"get_rgb_frame", "get_rgb_clip"})


def openai_tools(*, include_rgb: bool = True) -> list[dict[str, Any]]:
    """Tool schemas for the model; include_rgb=False when the video is not shared."""
    if include_rgb:
        return TOOL_DEFINITIONS
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] not in RGB_TOOL_NAMES]
