"""Compare two golf swings phase by phase, for the progress view.

Each swing goes through the same ``build_sport_analysis`` the analysis page uses, and
every value is read at the same phase moment as in ``_golf_report``, so the numbers here
are the numbers the player already saw for that swing.

All values are single-camera estimates. A change only counts when it is larger than the
metric's noise floor, and metrics that depend on camera direction or frame rate are not
judged when the two clips differ there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from motion_agent.motion.camera_view import estimate_view, unknown_view
from motion_agent.motion.golf_phases import PHASE_ORDER, segment_golf_swings
from motion_agent.motion.sequence import MotionSequence
from motion_agent.motion.sport_analysis import _at, _phase_span, build_sport_analysis


@dataclass(frozen=True)
class MetricSpec:
    id: str
    label: str
    event: str  # phase moment the value is read at (for seeking both videos)
    unit: str
    better: str | None  # "higher" | "lower" | None = shown, not judged
    finding: str | None  # finding whose rule this value drives
    noise_floor: float
    view_sensitive: bool = False
    rate_sensitive: bool = False


# Noise floors are provisional until measured from back-to-back swings of the same player.
NOISE_FLOOR_SOURCE = "provisional"
METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("separation_top", "Shoulders past hips at the top", "top", "°", "higher",
               "separation", 5.0, view_sensitive=True),
    MetricSpec("trail_knee_loss", "Back-knee bend lost by the top", "top", "°", "lower",
               "trail-knee", 5.0),
    MetricSpec("lead_elbow_top", "Front-arm bend at the top", "top", "°", "lower",
               "lead-arm", 5.0),
    MetricSpec("pelvis_drift_after_impact", "Hip drift after impact", "impact", "cm", "lower",
               "early-extension", 2.0, view_sensitive=True),
    MetricSpec("trail_hand_peak", "Trail-hand peak speed", "impact", "m/s", None,
               None, 0.5, rate_sensitive=True),
    MetricSpec("tempo", "Tempo (back : down)", "top", ":1", None,
               None, 0.3, rate_sensitive=True),
)
METRIC_BY_ID = {spec.id: spec for spec in METRICS}
METRIC_BY_FINDING = {spec.finding: spec for spec in METRICS if spec.finding}

# Goal lines mirror the fault rules in sport_analysis._golf_report.
SEPARATION_MIN_DEG = 20.0
TRAIL_KNEE_LOSS_MAX_DEG = 8.0
LEAD_ELBOW_RISE_MAX_DEG = 15.0
LEAD_ELBOW_TOP_MAX_DEG = 45.0
DRIFT_MAX_CM = 3.5
DRIFT_WINDOW_S = 0.28

CAMERA_ANGLE_TOLERANCE_DEG = 20.0
FPS_RATIO_LIMIT = 1.5

# Shared phase axis for the overlay: each event lands on the same spot for both swings.
OVERLAY_MARKS = (("address", 0.2), ("top", 0.55), ("impact", 0.8), ("finish", 1.0))
OVERLAY_POINTS = 61
OVERLAY_SERIES = (
    ("separation", "Shoulders past hips"),
    ("trail-knee", "Back-knee bend"),
    ("lead-elbow", "Front-arm bend"),
    ("pelvis-drift", "Hip drift from address"),
    ("trail-hand", "Trail-hand speed"),
)

STATUS_LABELS = {
    "resolved": "Fixed",
    "borderline": "Close",
    "improving": "Getting better",
    "unchanged": "No clear change",
    "worse": "Slipped",
    "not_comparable": "Can't compare",
}

DISCLOSURE = (
    "Estimated from a single phone camera. Changes smaller than the margin shown can be noise."
)


def _fmt_value(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "°":
        return f"{value:.0f}°"
    if unit == "cm":
        return f"{value:.1f}" if abs(value) < 10 else f"{value:.0f}"
    if unit == ":1":
        return f"{value:.1f}:1"
    return f"{value:.1f}"


def format_pair(spec: MetricSpec, before: float | None, after: float | None) -> str:
    left = _fmt_value(before, spec.unit)
    right = _fmt_value(after, spec.unit)
    if spec.unit in ("°", ":1"):
        return f"{left} → {right}"
    return f"{left} → {right} {spec.unit}"


def format_single(spec: MetricSpec, value: float | None) -> str:
    text = _fmt_value(value, spec.unit)
    return text if spec.unit in ("°", ":1") or value is None else f"{text} {spec.unit}"


# ---------------------------------------------------------------- one swing


def swing_snapshot(
    seq: MotionSequence,
    swing_index: int = 0,
    *,
    handedness: str = "right",
    pose3d: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the comparison needs from one swing of one capture."""
    segmented = segment_golf_swings(seq, handedness=handedness, max_swings=5)
    swing_count = 0 if segmented.get("error") else len(segmented.get("swings") or [])
    index = max(0, min(int(swing_index), max(swing_count - 1, 0)))
    analysis = build_sport_analysis(seq, "golf", handedness=handedness, swing_index=index)
    if analysis.get("error"):
        return {
            "ok": False,
            "swing_index": index,
            "swing_count": swing_count,
            "fps": float(seq.fps),
            "alignment": "none",
            "missing": list(PHASE_ORDER),
            "values": {},
            "goals": {},
            "fired": [],
            "findings": [],
            "series": [],
            "phases": [],
            "events": {},
            "view": unknown_view("no_analysis"),
        }

    series = analysis.get("series") or []
    traces = {trace["id"]: trace for trace in series}
    phases = analysis.get("phases") or []
    phase_ids = {p.get("id") for p in phases}
    t0 = float(series[0]["times"][0]) if series else float(analysis["start_time"])
    t1 = float(series[0]["times"][-1]) if series else float(analysis["end_time"])
    impact_t = analysis.get("impact_time")

    address = _phase_span(phases, "address")
    top = _phase_span(phases, "top", "transition", "backswing")
    follow = _phase_span(phases, "follow_through", "follow", "finish")
    events: dict[str, float | None] = {
        "address": round(address[1], 4) if address else None,
        "top": round(top[1], 4) if top else None,
        "impact": None if impact_t is None else round(float(impact_t), 4),
        "finish": round(follow[1], 4) if follow else None,
    }
    missing = [pid for pid in PHASE_ORDER if pid not in phase_ids]
    if events["top"] is None or events["impact"] is None or not (t0 < events["top"] < events["impact"]):
        alignment = "none"
    elif missing:
        alignment = "partial"
    else:
        alignment = "good"

    values: dict[str, float | None] = {spec.id: None for spec in METRICS}
    goals: dict[str, dict[str, Any]] = {}
    address = address or (t0, t0)
    if top is not None and impact_t is not None:
        impact_t = float(impact_t)
        sep = _at(traces.get("separation"), top[1])
        if sep:
            values["separation_top"] = abs(sep[1])
        goals["separation_top"] = {"min": SEPARATION_MIN_DEG, "label": f"{SEPARATION_MIN_DEG:.0f}°+"}

        knee_a = _at(traces.get("trail-knee"), address[1])
        knee_b = _at(traces.get("trail-knee"), top[1])
        if knee_a and knee_b:
            # A knee that bends further has lost nothing.
            values["trail_knee_loss"] = max(0.0, knee_a[1] - knee_b[1])
        goals["trail_knee_loss"] = {"max": TRAIL_KNEE_LOSS_MAX_DEG, "label": f"under {TRAIL_KNEE_LOSS_MAX_DEG:.0f}°"}

        elbow_0 = _at(traces.get("lead-elbow"), address[0])
        elbow_1 = _at(traces.get("lead-elbow"), top[1])
        if elbow_0 and elbow_1:
            values["lead_elbow_top"] = elbow_1[1]
            cap = min(elbow_0[1] + LEAD_ELBOW_RISE_MAX_DEG, LEAD_ELBOW_TOP_MAX_DEG)
            goals["lead_elbow_top"] = {"max": round(cap, 1), "label": f"under {cap:.0f}°"}

        drift_0 = _at(traces.get("pelvis-drift"), impact_t)
        drift_1 = _at(traces.get("pelvis-drift"), min(t1, impact_t + DRIFT_WINDOW_S))
        if drift_0 and drift_1:
            values["pelvis_drift_after_impact"] = max(0.0, drift_1[1] - drift_0[1])
        goals["pelvis_drift_after_impact"] = {"max": DRIFT_MAX_CM, "label": f"under {DRIFT_MAX_CM:.1f} cm"}

        back_s = top[1] - address[1]
        down_s = impact_t - top[1]
        if back_s > 0.05 and down_s > 0.02:
            values["tempo"] = back_s / down_s

    hand = traces.get("trail-hand")
    hand_peak_t = None
    if hand and hand.get("values"):
        hand_peak_t, peak = max(zip(hand["times"], hand["values"]), key=lambda item: item[1])
        values["trail_hand_peak"] = float(peak)
    events_for_metric = dict(events)
    if hand_peak_t is not None:
        events_for_metric["hand_peak"] = round(float(hand_peak_t), 4)

    real = [note for note in analysis.get("findings") or [] if not str(note.get("id", "")).startswith("read-")]
    view = unknown_view("no_landmarks")
    if pose3d is not None:
        view = estimate_view(pose3d, address[0], address[1])

    return {
        "ok": True,
        "swing_index": index,
        "swing_count": max(swing_count, 1),
        "fps": float(seq.fps),
        "handedness": analysis.get("handedness"),
        "start_time": round(t0, 4),
        "end_time": round(t1, 4),
        "phases": phases,
        "events": events,
        "metric_times": events_for_metric,
        "alignment": alignment,
        "missing": missing,
        "values": {k: (None if v is None else round(float(v), 3)) for k, v in values.items()},
        "goals": goals,
        "fired": [note["id"] for note in real],
        "findings": [
            {"id": note["id"], "headline": note.get("headline") or note.get("title"), "title": note.get("title")}
            for note in real
        ],
        "series": series,
        "view": view,
    }


def goal_met(spec: MetricSpec, snapshot: dict[str, Any]) -> bool | None:
    """Whether this swing sits on the good side of the metric's goal line (None = no goal)."""
    value = (snapshot.get("values") or {}).get(spec.id)
    goal = (snapshot.get("goals") or {}).get(spec.id)
    if value is None or not goal:
        return None
    if "min" in goal:
        return value >= float(goal["min"])
    return value < float(goal["max"])


# ---------------------------------------------------------------- two swings


def _verdict(spec: MetricSpec, before: float | None, after: float | None, comparable: bool) -> str:
    if before is None or after is None or not comparable:
        return "not_comparable"
    if spec.better is None:
        return "info"
    delta = after - before
    if abs(delta) < spec.noise_floor:
        return "no_clear_change"
    improved = delta > 0 if spec.better == "higher" else delta < 0
    return "improved" if improved else "worse"


def _issue_status(verdict: str, still_fires: bool) -> str:
    if verdict == "not_comparable":
        return "not_comparable"
    if not still_fires:
        return "resolved" if verdict == "improved" else "borderline"
    return {"improved": "improving", "worse": "worse"}.get(verdict, "unchanged")


def _issue_message(status: str, spec: MetricSpec, display: str, goal_label: str | None, reason: str | None) -> str:
    goal = f" (goal {goal_label})" if goal_label else ""
    if status == "resolved":
        return f"Fixed. {spec.label}: {display}{goal}."
    if status == "borderline":
        return f"Right at the goal line: {display}{goal}. Film again to confirm."
    if status == "improving":
        return f"Getting better: {display}{goal}. Keep at it."
    if status == "worse":
        return f"This slipped: {display}{goal}."
    if status == "unchanged":
        return f"About the same: {display}. Within the margin, so keep working on it."
    if reason == "camera_angle_differs":
        return "Filmed from a different angle, so this one can't be compared fairly. Film from the same spot next time."
    if reason == "frame_rate_differs":
        return "Filmed at a different frame rate, so this one can't be compared fairly."
    return "We couldn't line up both swings well enough to compare this."


def _warp(times: np.ndarray, anchors_t: list[float], anchors_u: list[float]) -> np.ndarray:
    return np.interp(times, anchors_t, anchors_u)


def _overlay_for(snapshot: dict[str, Any]) -> dict[str, list[float]] | None:
    events = snapshot.get("events") or {}
    start = float(snapshot.get("start_time") or 0.0)
    anchors_t = [start]
    anchors_u = [0.0]
    for name, u in OVERLAY_MARKS:
        t = events.get(name)
        if t is None:
            continue
        if float(t) <= anchors_t[-1] + 1e-6:
            continue
        anchors_t.append(float(t))
        anchors_u.append(u)
    if len(anchors_t) < 3 or anchors_u[-1] < 1.0:
        # Without a finish moment, stretch the window end to the end of the axis.
        end = float(snapshot.get("end_time") or anchors_t[-1])
        if end > anchors_t[-1] + 1e-6 and anchors_u[-1] < 1.0:
            anchors_t.append(end)
            anchors_u.append(1.0)
    if len(anchors_t) < 3:
        return None
    grid = np.linspace(0.0, 1.0, OVERLAY_POINTS)
    traces = {trace["id"]: trace for trace in snapshot.get("series") or []}
    out: dict[str, list[float]] = {}
    top_t = events.get("top")
    for sid, _label in OVERLAY_SERIES:
        trace = traces.get(sid)
        if not trace or len(trace.get("times") or []) < 4:
            continue
        times = np.asarray(trace["times"], dtype=np.float64)
        values = np.asarray(trace["values"], dtype=np.float64)
        keep = (times >= anchors_t[0]) & (times <= anchors_t[-1])
        if keep.sum() < 4:
            continue
        times, values = times[keep], values[keep]
        if sid == "separation" and top_t is not None:
            # Twist direction depends on the lead side; show "shoulders past hips" as positive.
            at_top = _at(trace, float(top_t))
            if at_top and at_top[1] < 0:
                values = -values
        u = _warp(times, anchors_t, anchors_u)
        out[sid] = [round(float(v), 2) for v in np.interp(grid, u, values)]
    return out


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Judge what changed from the earlier swing to the later one."""
    reasons: list[str] = []
    if before.get("alignment") == "none" or after.get("alignment") == "none":
        reasons.append("phases_unclear")

    view_b, view_a = before.get("view") or {}, after.get("view") or {}
    same_angle: bool | None = None
    if view_b.get("known") and view_a.get("known"):
        gap = abs(float(view_b["azimuth_deg"]) - float(view_a["azimuth_deg"]))
        same_angle = gap <= CAMERA_ANGLE_TOLERANCE_DEG
        if not same_angle:
            reasons.append("camera_angle_differs")

    fps_b, fps_a = float(before.get("fps") or 0), float(after.get("fps") or 0)
    rate_differs = bool(fps_b and fps_a and max(fps_b, fps_a) / min(fps_b, fps_a) > FPS_RATIO_LIMIT)
    if rate_differs:
        reasons.append("frame_rate_differs")

    if "phases_unclear" in reasons:
        level = "none"
    elif reasons:
        level = "limited"
    else:
        level = "full"

    messages = {
        "phases_unclear": "We couldn't find a full swing in one of these clips, so there's nothing to line up.",
        "camera_angle_differs": (
            "These were filmed from different angles, so the turn and hip-drift numbers can't be compared fairly."
        ),
        "frame_rate_differs": (
            "These were filmed at different frame rates, so speed and tempo can't be compared fairly."
        ),
    }
    message = " ".join(messages[r] for r in reasons)
    if not message and same_angle is None:
        message = "Film from the same spot each time for a fair comparison."

    metrics: list[dict[str, Any]] = []
    if level != "none":
        vb, va = before.get("values") or {}, after.get("values") or {}
        tb, ta = before.get("metric_times") or {}, after.get("metric_times") or {}
        for spec in METRICS:
            b, a = vb.get(spec.id), va.get(spec.id)
            if b is None and a is None:
                continue
            comparable = not (spec.view_sensitive and same_angle is False) and not (
                spec.rate_sensitive and rate_differs
            )
            event = "hand_peak" if spec.id == "trail_hand_peak" else spec.event
            goal = (after.get("goals") or {}).get(spec.id)
            metrics.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "event": spec.event,
                    "unit": spec.unit,
                    "before": b,
                    "after": a,
                    "delta": None if a is None or b is None else round(a - b, 2),
                    "display": format_pair(spec, b, a),
                    "better": spec.better,
                    "goal": goal,
                    "noise_floor": spec.noise_floor,
                    "noise_floor_source": NOISE_FLOOR_SOURCE,
                    "margin": f"±{format_single(spec, spec.noise_floor)}",
                    "verdict": _verdict(spec, b, a, comparable),
                    "estimate": True,
                    "view_sensitive": spec.view_sensitive,
                    "finding_id": spec.finding,
                    "time_before": tb.get(event),
                    "time_after": ta.get(event),
                }
            )
    by_id = {m["id"]: m for m in metrics}

    main_issue = None
    first = (before.get("findings") or [None])[0]
    if first:
        spec = METRIC_BY_FINDING.get(first["id"])
        metric = by_id.get(spec.id) if spec else None
        if spec is None or metric is None:
            status, display, goal_label = "not_comparable", None, None
        else:
            status = _issue_status(metric["verdict"], first["id"] in (after.get("fired") or []))
            display = metric["display"]
            goal_label = (metric.get("goal") or {}).get("label")
        reason = None
        if status == "not_comparable":
            if spec is not None and spec.view_sensitive and same_angle is False:
                reason = "camera_angle_differs"
            elif spec is not None and spec.rate_sensitive and rate_differs:
                reason = "frame_rate_differs"
            else:
                reason = "phases_unclear"
        main_issue = {
            "finding_id": first["id"],
            "headline": first.get("headline"),
            "metric_id": spec.id if spec else None,
            "label": spec.label if spec else None,
            "before_display": format_single(spec, metric["before"]) if spec and metric else None,
            "after_display": format_single(spec, metric["after"]) if spec and metric else None,
            "display": display,
            "goal": goal_label,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "reason": reason,
            "message": _issue_message(status, spec, display or "", goal_label, reason)
            if spec
            else _issue_message(status, METRICS[0], "", None, reason),
        }

    next_issue = None
    nxt = (after.get("findings") or [None])[0]
    if nxt:
        next_issue = {
            "finding_id": nxt["id"],
            "headline": nxt.get("headline"),
            "same_as_before": bool(first and nxt["id"] == first["id"]),
        }

    overlay = None
    if level != "none":
        ob, oa = _overlay_for(before), _overlay_for(after)
        if ob and oa:
            labels = dict(OVERLAY_SERIES)
            units = {trace["id"]: trace.get("unit") for trace in after.get("series") or []}
            overlay = {
                "axis": "phase_normalized",
                "u": [round(float(u), 4) for u in np.linspace(0.0, 1.0, OVERLAY_POINTS)],
                "marks": [{"id": name, "u": u} for name, u in OVERLAY_MARKS],
                "series": [
                    {
                        "id": sid,
                        "label": labels[sid],
                        "unit": units.get(sid),
                        "before": ob[sid],
                        "after": oa[sid],
                    }
                    for sid, _ in OVERLAY_SERIES
                    if sid in ob and sid in oa
                ],
            }

    return {
        "comparability": {
            "level": level,
            "reasons": reasons,
            "same_camera_angle": same_angle,
            "message": message,
        },
        "alignment": {
            "method": "phase_events",
            "quality": min(
                (before.get("alignment"), after.get("alignment")),
                key=lambda q: {"none": 0, "partial": 1, "good": 2}.get(q or "none", 0),
            ),
            "missing": sorted(set(before.get("missing") or []) | set(after.get("missing") or [])),
        },
        "metrics": metrics,
        "main_issue": main_issue,
        "next_issue": next_issue,
        "overlay": overlay,
        "disclosure": DISCLOSURE,
    }
