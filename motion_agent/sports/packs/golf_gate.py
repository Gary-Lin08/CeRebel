"""Is this really a golf swing? Deterministic checks on the rebuilt 3D body.

``segment_golf_swings`` always finds a "swing": with no clear match it falls back
to the fastest wrist frame, and its confidence never drops below 0.5. Tennis
strokes, layups and plain arm waves therefore all came back as golf. This gate
runs after segmentation and asks for what only a golf swing has: two hands
together on a grip, hands low at setup and at the ball, and a body that stays
planted. Only joint positions already computed by the pipeline are used — no
new model, and the video frames are never looked at.

Thresholds were set on real captures (see ``motion_agent/tests/test_golf_gate.py``):
two golf swings in ``kinetic-lens/retargeting_neck_v2/outputs`` (old and new body
model each), the live demo swing 5032CEE6, and the kinetic-lens layup sample,
which the old detector called golf at 0.71. Each limit sits well outside the
range the golf swings reached.
"""

from __future__ import annotations

from typing import Any

import numpy as np

UP = 1  # OpenSim ground frame: Y is up.

# A full swing (takeaway to finish) takes well over a second; the shortest golf
# capture we have is 2.5 s.
MIN_CLIP_S = 1.0
# Fastest wrist (0.1 s average). Golf swings: 6.3–7.1 m/s. Arm waves and
# fidgeting stay near 1 m/s.
MIN_PEAK_WRIST_SPEED_M_S = 2.5
# Wrist-to-wrist distance from takeaway to impact (80th percentile), in shoulder
# widths. Golf: 0.53–0.62 (both hands on the grip). Layup: 2.86.
MAX_GRIP_SPREAD_SHOULDERS = 1.1
GRIP_SPREAD_PERCENTILE = 80
# Hip-centre rise and fall from takeaway to impact. Golf: 0.012–0.049 m. Layup: 0.51 m.
MAX_PELVIS_RISE_M = 0.20
# Horizontal travel of the stiller ankle from takeaway to impact. Golf: ≤ 0.014 m
# (the trail ankle can move 0.18 m, so only the stiller foot is held to this).
# Layup: 1.76 m.
MAX_PLANTED_FOOT_TRAVEL_M = 0.25
# Hands (wrist midpoint) above the hip centres, in torso lengths.
# At setup (median over the address phase) golf reads −0.54 to −0.16; at the
# lowest point within ±0.1 s of impact golf reads −0.45 to 0.0. A static setup is
# not required: one real capture starts mid-takeaway.
MAX_ADDRESS_HANDS_ABOVE_HIPS = 0.10
MAX_IMPACT_HANDS_ABOVE_HIPS = 0.35
IMPACT_WINDOW_S = 0.10

# "Looks like" cues — only used to describe a clip that already failed the gate,
# and only when the evidence is plain.
RUN_OR_JUMP_PELVIS_RANGE_M = 0.25
RUN_OR_JUMP_FOOT_TRAVEL_M = 0.60
OVERHEAD_WRIST_ABOVE_SHOULDERS = 0.50  # torso lengths, at the fastest moment
BAT_ADDRESS_HANDS_ABOVE_HIPS = 0.60  # torso lengths: hands start up by the shoulder

# Order matters: the first failing check is the reason we give.
CHECK_ORDER = (
    "clip_length",
    "swing_speed",
    "two_hands",
    "stays_grounded",
    "stance_planted",
    "address_hands_low",
    "impact_hands_low",
)
NO_SWING_CHECKS = ("clip_length", "swing_speed")

COMING_SOON = "We analyze golf today; tennis and baseball are coming soon."
RETRY_HINT = "Try a side-on clip that shows your whole swing, from setup to finish."
NO_SWING_MESSAGE = f"We couldn't find a full swing in this clip. {RETRY_HINT}"
TOO_SHORT_MESSAGE = f"This clip is too short to hold a full swing. {RETRY_HINT}"

LOOKS_LIKE_PHRASES = {
    "run_or_jump": "running or jumping",
    "overhead": "an overhead swing or throw",
    "bat_swing": "a baseball or softball swing",
    "one_handed": "a one-handed swing or throw",
}
REASON_PHRASES = {
    "two_hands": "your hands weren't together on a grip",
    "stays_grounded": "your body left the ground during the swing",
    "stance_planted": "both feet moved during the swing",
    "address_hands_low": "your hands didn't start low, at the ball",
    "impact_hands_low": "your hands were high at the fastest point, not down at the ball",
}

_JOINTS = (
    "left_wrist",
    "right_wrist",
    "left_shoulder",
    "right_shoulder",
    "left_hip",
    "right_hip",
    "left_ankle",
    "right_ankle",
)


def _dt(seq: Any) -> float:
    dt = (getattr(seq, "metadata", None) or {}).get("dt")
    if dt:
        return float(dt)
    ts = np.asarray(seq.timestamps, dtype=float)
    if ts.size > 1:
        return float(np.median(np.diff(ts)))
    return 1.0 / max(float(getattr(seq, "fps", 30.0) or 30.0), 1e-6)


def _smoothed_speed(pos: np.ndarray, dt: float) -> np.ndarray:
    speed = np.linalg.norm(np.gradient(pos, dt, axis=0), axis=1)
    win = max(3, int(round(0.1 / max(dt, 1e-6))))
    if win % 2 == 0:
        win += 1
    if speed.size < win:
        return speed
    pad = win // 2
    padded = np.pad(speed, (pad, pad), mode="edge")
    return np.convolve(padded, np.ones(win) / win, mode="valid")


def _check(check_id: str, passed: bool, value: float, limit: float, unit: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "passed": bool(passed),
        "value": round(float(value), 3),
        "limit": limit,
        "unit": unit,
    }


def _body(seq: Any) -> dict[str, np.ndarray] | None:
    jp = getattr(seq, "joint_positions", None) or {}
    out: dict[str, np.ndarray] = {}
    for name in _JOINTS:
        arr = jp.get(name)
        if arr is None:
            return None
        out[name] = np.asarray(arr, dtype=float)
    return out


def _swing_frames(ts: np.ndarray, swing: dict[str, Any]) -> tuple[int, int, int, int]:
    """(start, takeaway, impact, end) frame indices for one segmented swing."""

    def idx(t: Any) -> int:
        return int(np.clip(np.searchsorted(ts, float(t) - 1e-6), 0, ts.size - 1))

    i0 = idx(swing.get("start_time", ts[0]))
    i1 = idx(swing.get("end_time", ts[-1]))
    i_imp = idx(swing.get("impact_time", ts[i1]))
    take = next((p for p in swing.get("phases") or [] if p.get("id") == "takeaway"), None)
    i_take = idx(take["start_time"]) if take else i0
    if not (i0 <= i_take < i_imp):
        i_take = i0
    if i_take >= i_imp:
        i_take, i_imp = i0, max(i0 + 1, i1)
    return i0, i_take, min(i_imp, ts.size - 1), i1


def _evaluate_swing(
    ts: np.ndarray,
    body: dict[str, np.ndarray],
    swing: dict[str, Any],
    *,
    clip_s: float,
    peak_speed: float,
    peak_i: int,
    shoulder_width: float,
    torso: float,
) -> dict[str, Any]:
    lw, rw = body["left_wrist"], body["right_wrist"]
    hands = 0.5 * (lw + rw)
    shoulders = 0.5 * (body["left_shoulder"] + body["right_shoulder"])
    hips = 0.5 * (body["left_hip"] + body["right_hip"])
    hands_above_hips = (hands[:, UP] - hips[:, UP]) / torso

    i0, i_take, i_imp, _ = _swing_frames(ts, swing)
    swing_win = slice(i_take, i_imp + 1)
    address_win = slice(i0, i_take + 1)
    near_impact = np.abs(ts - ts[i_imp]) <= IMPACT_WINDOW_S + 1e-6

    spread = np.linalg.norm(lw - rw, axis=1)[swing_win] / shoulder_width
    grip_spread = float(np.percentile(spread, GRIP_SPREAD_PERCENTILE))
    pelvis_rise = float(np.ptp(hips[swing_win, UP]))
    travel = []
    for ankle in (body["left_ankle"], body["right_ankle"]):
        moved = ankle[swing_win] - ankle[i_take]
        travel.append(float(np.max(np.linalg.norm(moved[:, [0, 2]], axis=1))))
    address_hands = float(np.median(hands_above_hips[address_win]))
    impact_hands = float(np.min(hands_above_hips[near_impact]))

    checks = [
        _check("clip_length", clip_s >= MIN_CLIP_S, clip_s, MIN_CLIP_S, "s"),
        _check("swing_speed", peak_speed >= MIN_PEAK_WRIST_SPEED_M_S, peak_speed, MIN_PEAK_WRIST_SPEED_M_S, "m/s"),
        _check("two_hands", grip_spread <= MAX_GRIP_SPREAD_SHOULDERS, grip_spread, MAX_GRIP_SPREAD_SHOULDERS, "shoulder widths"),
        _check("stays_grounded", pelvis_rise <= MAX_PELVIS_RISE_M, pelvis_rise, MAX_PELVIS_RISE_M, "m"),
        _check("stance_planted", min(travel) <= MAX_PLANTED_FOOT_TRAVEL_M, min(travel), MAX_PLANTED_FOOT_TRAVEL_M, "m"),
        _check(
            "address_hands_low",
            address_hands <= MAX_ADDRESS_HANDS_ABOVE_HIPS,
            address_hands,
            MAX_ADDRESS_HANDS_ABOVE_HIPS,
            "torso lengths above hips",
        ),
        _check(
            "impact_hands_low",
            impact_hands <= MAX_IMPACT_HANDS_ABOVE_HIPS,
            impact_hands,
            MAX_IMPACT_HANDS_ABOVE_HIPS,
            "torso lengths above hips",
        ),
    ]

    # Cues for describing a clip that failed (never used to pass one).
    near_peak = np.abs(ts - ts[peak_i]) <= IMPACT_WINDOW_S + 1e-6
    top_wrist = np.maximum(lw[:, UP], rw[:, UP])
    overhead = float(np.max((top_wrist[near_peak] - shoulders[near_peak, UP]) / torso))
    cues = {
        "pelvis_range_m": round(float(np.ptp(hips[:, UP])), 3),
        "stiller_foot_travel_m": round(min(travel), 3),
        "busier_foot_travel_m": round(max(travel), 3),
        "wrist_above_shoulders_at_peak": round(overhead, 3),
        "address_hands_above_hips": round(address_hands, 3),
        "grip_spread_shoulders": round(grip_spread, 3),
    }
    return {
        "swing_index": swing.get("swing_index"),
        "swing_confidence": swing.get("confidence"),
        "frames": {"start": i0, "takeaway": i_take, "impact": i_imp},
        "checks": checks,
        "cues": cues,
    }


def _looks_like(checks: dict[str, dict[str, Any]], cues: dict[str, float]) -> str | None:
    if any(not checks[c]["passed"] for c in NO_SWING_CHECKS):
        return None
    if (
        cues["pelvis_range_m"] >= RUN_OR_JUMP_PELVIS_RANGE_M
        or cues["stiller_foot_travel_m"] >= RUN_OR_JUMP_FOOT_TRAVEL_M
    ):
        return "run_or_jump"
    if cues["wrist_above_shoulders_at_peak"] >= OVERHEAD_WRIST_ABOVE_SHOULDERS:
        return "overhead"
    if checks["two_hands"]["passed"] and cues["address_hands_above_hips"] >= BAT_ADDRESS_HANDS_ABOVE_HIPS:
        return "bat_swing"
    if not checks["two_hands"]["passed"]:
        return "one_handed"
    return None


def gate_message(reason: str | None, looks_like: str | None) -> str:
    """Consumer copy for a clip that did not pass. Empty when it passed."""
    if reason is None:
        return ""
    if reason == "clip_length":
        return TOO_SHORT_MESSAGE
    if reason in NO_SWING_CHECKS or reason == "no_swing":
        return NO_SWING_MESSAGE
    if looks_like in LOOKS_LIKE_PHRASES:
        return f"This doesn't look like a golf swing — it looks more like {LOOKS_LIKE_PHRASES[looks_like]}. {COMING_SOON}"
    why = REASON_PHRASES.get(reason)
    lead = f"This doesn't look like a golf swing: {why}." if why else "This doesn't look like a golf swing."
    return f"{lead} {COMING_SOON} If it was a golf swing, {RETRY_HINT[0].lower()}{RETRY_HINT[1:]}"


def _no_swing(note: str) -> dict[str, Any]:
    return {
        "passed": False,
        "reason": "no_swing",
        "looks_like": None,
        "not_golf": False,
        "message": NO_SWING_MESSAGE,
        "note": note,
        "checks": [],
    }


def evaluate_golf_gate(seq: Any, phases_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Pass/fail plus every measured value, for one capture.

    ``seq`` needs ``timestamps`` and ``joint_positions`` (a ``MotionSequence`` or
    anything shaped like one); ``phases_payload`` is the ``segment_golf_swings``
    result. The capture passes when any one segmented swing passes every check.

    ``not_golf`` is True when there was a real swing but it was not a golf one —
    the case where we say what it looked like and that other sports are coming.
    """
    if not phases_payload or phases_payload.get("error") or not phases_payload.get("swings"):
        return _no_swing("no segmented swing")
    body = _body(seq)
    ts = np.asarray(seq.timestamps, dtype=float)
    if body is None or ts.size < 3:
        # Without wrists, shoulders, hips and ankles the gate cannot judge; the
        # segmenter's own confidence stays the only test (old behaviour).
        return {"passed": True, "reason": None, "looks_like": None, "not_golf": False,
                "message": "", "note": "joints missing; gate skipped", "checks": []}

    dt = _dt(seq)
    clip_s = float(ts[-1] - ts[0]) + dt
    speeds = np.stack([_smoothed_speed(body["left_wrist"], dt), _smoothed_speed(body["right_wrist"], dt)])
    peak_i = int(np.argmax(np.max(speeds, axis=0)))
    peak_speed = float(np.max(speeds))
    shoulder_width = float(np.median(np.linalg.norm(body["left_shoulder"] - body["right_shoulder"], axis=1)))
    shoulders = 0.5 * (body["left_shoulder"] + body["right_shoulder"])
    hips = 0.5 * (body["left_hip"] + body["right_hip"])
    torso = float(np.median(np.linalg.norm(shoulders - hips, axis=1)))
    if shoulder_width <= 1e-3 or torso <= 1e-3:
        return _no_swing("degenerate body scale")

    evaluated = [
        _evaluate_swing(
            ts,
            body,
            swing,
            clip_s=clip_s,
            peak_speed=peak_speed,
            peak_i=peak_i,
            shoulder_width=shoulder_width,
            torso=torso,
        )
        for swing in phases_payload["swings"]
    ]

    def failures(ev: dict[str, Any]) -> int:
        return sum(1 for c in ev["checks"] if not c["passed"])

    best = min(evaluated, key=lambda ev: (failures(ev), -float(ev.get("swing_confidence") or 0.0)))
    by_id = {c["id"]: c for c in best["checks"]}
    reason = next((cid for cid in CHECK_ORDER if not by_id[cid]["passed"]), None)
    looks_like = _looks_like(by_id, best["cues"]) if reason else None
    return {
        "passed": reason is None,
        "reason": reason,
        "looks_like": looks_like,
        "not_golf": reason is not None and reason not in NO_SWING_CHECKS,
        "message": gate_message(reason, looks_like),
        "swing_index": best["swing_index"],
        "frames": best["frames"],
        "checks": best["checks"],
        "cues": best["cues"],
    }
