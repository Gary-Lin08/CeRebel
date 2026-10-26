"""Measured motion figures for golf and baseball analysis boards.

Numbers come from OpenSim coordinates and joint positions. The chat UI draws
the labeled traces; the same payload is what the model is allowed to cite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from motion_agent.motion.golf_phases import segment_golf_swings
from motion_agent.motion.sequence import MotionSequence, velocity_of
from motion_agent.motion.synchronization import slice_indices
from motion_agent.storage.errors import tool_error

MAX_SAMPLES = 80

_SPORTS = ("golf", "baseball")


def build_sport_analysis(
    seq: MotionSequence,
    sport: str = "golf",
    start_time: float | None = None,
    end_time: float | None = None,
    handedness: str = "right",
    swing_index: int = 0,
) -> dict[str, Any]:
    """Return phases, labeled series, and landmark readings for one movement."""
    key = (sport or "golf").strip().lower()
    if key not in _SPORTS:
        return tool_error("INVALID_ARGUMENT", f"sport must be one of {_SPORTS}")
    hand = (handedness or "right").strip().lower()
    if hand not in ("right", "left", "auto"):
        return tool_error("INVALID_ARGUMENT", "handedness must be right|left|auto")
    if hand == "auto":
        hand = "right"
    lead, trail = ("left", "right") if hand == "right" else ("right", "left")

    # Do not invent a pitch board on a golf capture (product trust).
    if key == "baseball":
        try:
            from motion_agent.app.product_session import session_looks_like_golf
            from motion_agent.storage.motion_store import default_data_dir

            data_dir = default_data_dir()
            session_dir = Path(data_dir) / "sessions" / str(seq.video_id)
            if session_dir.is_dir():
                is_golf, conf, _ = session_looks_like_golf(
                    session_dir, video_id=seq.video_id, data_dir=Path(data_dir)
                )
                if is_golf:
                    return tool_error(
                        "SPORT_MISMATCH",
                        "This capture looks like a golf swing, not a baseball pitch "
                        f"(confidence {conf:.2f}).",
                    )
        except Exception:
            pass

    if key == "golf":
        window, phases, impact_time, phase_note = _golf_window(
            seq, start_time, end_time, hand, swing_index
        )
        title = "Swing analysis"
        trail_label = "Trail-hand path"
        stage = (
            "Golf swing replay. Labeled traces are measured from the reconstructed "
            "body over the swing window; the phase bar matches those times."
        )
        series_spec = _golf_series_spec(lead, trail)
        disclosure_motion = (
            "Traces are measured from the reconstructed body in this capture, "
            "not club or ball tracking."
        )
    else:
        window, phases, impact_time, phase_note = _baseball_window(
            seq, start_time, end_time, lead, trail
        )
        title = "Pitch analysis"
        trail_label = "Throwing-hand path"
        stage = (
            "Baseball pitch replay. Labeled traces are measured from the reconstructed "
            "body over the pitching window; the phase bar matches those times."
        )
        series_spec = _baseball_series_spec(lead, trail)
        disclosure_motion = (
            "Traces are measured from the reconstructed body in this capture. "
            "The ball is not tracked; the throwing-hand path is the hand, not the ball."
        )

    if window is None:
        return tool_error("ANALYSIS_FAILED", phase_note or "Could not locate a movement window")

    start_s, end_s = window
    built: list[dict[str, Any]] = []
    for spec in series_spec:
        series = _build_series(seq, spec, start_s, end_s)
        if series is not None:
            built.append(series)
    if len(built) < 2:
        return tool_error(
            "ANALYSIS_FAILED",
            "Not enough reconstructed joint series to draw the analysis figures.",
        )

    landmarks = _landmarks(built, impact_time)
    findings, highlights = _compose_report(
        key, built, phases, impact_time, lead, trail
    )
    try:
        from motion_agent.knowledge.fault_index import enrich_findings

        findings = enrich_findings(key, findings)
    except Exception:
        pass

    return {
        "error": False,
        "ui_hint": "sport_motion_board",
        "tool": "build_sport_analysis",
        "sport": key,
        "title": title,
        "badge": "Motion analysis",
        "stage_label": stage,
        "trail_label": trail_label,
        "handedness": hand,
        "lead_side": lead,
        "trail_side": trail,
        "start_time": round(start_s, 4),
        "end_time": round(end_s, 4),
        "impact_time": None if impact_time is None else round(float(impact_time), 4),
        "phases": phases,
        "series": built,
        "landmarks": landmarks,
        "findings": findings,
        "highlights": highlights,
        "disclosure": {
            "badge": "Motion analysis",
            "motion": disclosure_motion,
            "coaching": (
                "Findings must be read from these traces. "
                "Estimated dynamics are not force-plate measurements."
            ),
        },
        "note_for_model": (
            "These series samples and landmarks are the only numerical measurements "
            "you may cite for this movement. Do not invent angles, distances, speeds, "
            "or times. Phase labels and times are the timeline. "
            f"{phase_note} "
            "Write 2–4 coached findings grounded in these figures and end your answer "
            "with a ```cerebel-analysis JSON fence the UI uses to label the board: "
            '{"highlights":["..."],"findings":[{"id":"","title":"","detail":"",'
            '"series":"<series id>","start_time":0,"end_time":0,'
            '"metric":{"value":"","caption":""},"why":"","cue":"","drill":"","check":""}]}'
            ". series must be one of the ids above. metric.value must be copied from "
            "a landmark or a sample, not estimated from appearance."
        ),
    }


def _golf_series_spec(lead: str, trail: str) -> list[dict[str, Any]]:
    side = {"left": "l", "right": "r"}
    return [
        {
            "id": "trail-knee",
            "label": "Trail knee flex",
            "unit": "°",
            "kind": "coord",
            "coord": f"knee_angle_{side[trail]}",
        },
        {
            "id": "lead-elbow",
            "label": "Lead elbow flex",
            "unit": "°",
            "kind": "coord",
            "coord": f"elbow_flex_{side[lead]}",
        },
        {
            "id": "separation",
            "label": "Hip–shoulder separation",
            "unit": "°",
            "kind": "coord",
            "coord": "lumbar_rotation",
            "zero_at_start": True,
        },
        {
            "id": "pelvis-turn",
            "label": "Pelvis turn",
            "unit": "°",
            "kind": "coord",
            "coord": "pelvis_rotation",
            "zero_at_start": True,
        },
        {
            "id": "pelvis-drift",
            "label": "Pelvis drift from address",
            "unit": "cm",
            "kind": "drift",
            "joint": "pelvis",
        },
        {
            "id": "trail-hand",
            "label": "Trail-hand speed",
            "unit": "m/s",
            "kind": "speed",
            "joint": f"{trail}_wrist",
        },
    ]


def _baseball_series_spec(lead: str, trail: str) -> list[dict[str, Any]]:
    side = {"left": "l", "right": "r"}
    return [
        {
            "id": "stride",
            "label": "Stride length",
            "unit": "cm",
            "kind": "stride",
            "lead": f"{lead}_ankle",
            "trail": f"{trail}_ankle",
        },
        {
            "id": "back-knee",
            "label": "Back knee flex",
            "unit": "°",
            "kind": "coord",
            "coord": f"knee_angle_{side[trail]}",
        },
        {
            "id": "separation",
            "label": "Hip–shoulder separation",
            "unit": "°",
            "kind": "coord",
            "coord": "lumbar_rotation",
            "zero_at_start": True,
        },
        {
            "id": "front-knee",
            "label": "Front knee flex",
            "unit": "°",
            "kind": "coord",
            "coord": f"knee_angle_{side[lead]}",
        },
        {
            "id": "throwing-hand",
            "label": "Throwing-hand speed",
            "unit": "m/s",
            "kind": "speed",
            "joint": f"{trail}_wrist",
        },
    ]


def _golf_window(
    seq: MotionSequence,
    start_time: float | None,
    end_time: float | None,
    handedness: str,
    swing_index: int,
) -> tuple[tuple[float, float] | None, list[dict[str, Any]], float | None, str]:
    args: dict[str, Any] = {"handedness": handedness, "max_swings": 5}
    if start_time is not None:
        args["start_time"] = float(start_time)
    if end_time is not None:
        args["end_time"] = float(end_time)
    segmented = segment_golf_swings(seq, **args)
    if segmented.get("error") or not segmented.get("swings"):
        start_s, end_s = _clip_bounds(seq, start_time, end_time)
        return (
            (start_s, end_s),
            _even_phases(start_s, end_s, [("movement", "Movement")]),
            None,
            "Golf phase segmentation was unavailable; traces cover the requested window.",
        )
    swings = segmented["swings"]
    idx = max(0, min(int(swing_index), len(swings) - 1))
    swing = swings[idx]
    start_s = float(swing["start_time"])
    end_s = float(swing["end_time"])
    phases = [
        {
            "id": p["id"],
            "label": p.get("label") or p["id"],
            "start_time": round(float(p["start_time"]), 4),
            "end_time": round(float(p["end_time"]), 4),
        }
        for p in swing.get("phases") or []
    ]
    impact = swing.get("impact_time")
    note = (
        f"Window is swing {idx + 1} of {len(swings)} "
        f"({start_s:.2f}–{end_s:.2f} s)."
    )
    return (start_s, end_s), phases, None if impact is None else float(impact), note


def _baseball_window(
    seq: MotionSequence,
    start_time: float | None,
    end_time: float | None,
    lead: str,
    trail: str,
) -> tuple[tuple[float, float] | None, list[dict[str, Any]], float | None, str]:
    start_s, end_s = _clip_bounds(seq, start_time, end_time)
    hand = f"{trail}_wrist"
    if hand not in seq.joint_positions:
        return None, [], None, f"Missing {hand} position"
    sl = slice_indices(seq.timestamps, start_s, end_s)
    times = seq.timestamps[sl]
    if times.size < 8:
        return None, [], None, "Window is too short to segment a pitch"
    dt = float(seq.metadata.get("dt") or (1.0 / max(seq.fps, 1.0)))
    speed = np.linalg.norm(velocity_of(seq.joint_positions[hand], dt)[sl], axis=-1)
    release_i = int(np.argmax(speed))
    release_t = float(times[release_i])

    landing_t = _landing_time(seq, times, release_i, lead, dt)
    span = max(end_s - start_s, 0.4)
    set_end = start_s + 0.12 * span
    if landing_t is None:
        landing_t = max(start_s, release_t - 0.18 * span)
    lift_end = max(set_end, landing_t - 0.28 * span)
    stride_end = landing_t
    land_end = min(end_s, landing_t + 0.08 * span)
    release_end = min(end_s, max(land_end, release_t + 0.06 * span))

    raw = [
        ("set", "Set", start_s, set_end),
        ("lift", "Leg lift", set_end, lift_end),
        ("stride", "Stride", lift_end, stride_end),
        ("landing", "Landing", stride_end, land_end),
        ("release", "Release", land_end, release_end),
        ("follow", "Follow-through", release_end, end_s),
    ]
    phases = []
    for pid, label, a, b in raw:
        if b - a < 0.02:
            continue
        phases.append(
            {
                "id": pid,
                "label": label,
                "start_time": round(float(a), 4),
                "end_time": round(float(b), 4),
            }
        )
    if phases:
        phases[0]["start_time"] = round(start_s, 4)
        phases[-1]["end_time"] = round(end_s, 4)
    note = (
        f"Pitch window {start_s:.2f}–{end_s:.2f} s. "
        f"Release is the throwing-hand speed peak at {release_t:.2f} s "
        f"({trail} wrist). Landing is estimated from the lead ({lead}) foot."
    )
    return (start_s, end_s), phases, release_t, note


def _landing_time(
    seq: MotionSequence,
    times: np.ndarray,
    release_i: int,
    lead: str,
    dt: float,
) -> float | None:
    ankle = f"{lead}_ankle"
    if ankle not in seq.joint_positions or release_i < 3:
        return None
    # Align the full-series velocity index with the windowed times.
    full_times = seq.timestamps
    i0 = int(np.searchsorted(full_times, float(times[0]), side="left"))
    speed = np.linalg.norm(velocity_of(seq.joint_positions[ankle], dt), axis=-1)
    local = speed[i0 : i0 + times.size]
    if local.size != times.size:
        return None
    before = local[: release_i + 1]
    if before.size < 4:
        return None
    # Landing: last strong deceleration of the lead foot before release.
    order = max(2, before.size // 12)
    peak = int(np.argmax(before))
    if peak >= before.size - 2:
        return float(times[max(0, release_i - 2)])
    after = before[peak:]
    quiet = float(np.percentile(before, 30))
    for j, value in enumerate(after):
        if value <= max(quiet * 1.15, float(before[peak]) * 0.45):
            return float(times[peak + j])
    return float(times[peak])


def _clip_bounds(
    seq: MotionSequence,
    start_time: float | None,
    end_time: float | None,
) -> tuple[float, float]:
    start_s = 0.0 if start_time is None else max(0.0, float(start_time))
    end_s = float(seq.duration_s) if end_time is None else min(float(seq.duration_s), float(end_time))
    if end_s <= start_s:
        end_s = min(float(seq.duration_s), start_s + 0.5)
    return start_s, end_s


def _even_phases(start_s: float, end_s: float, labels: list[tuple[str, str]]) -> list[dict[str, Any]]:
    n = len(labels)
    span = end_s - start_s
    out = []
    for i, (pid, label) in enumerate(labels):
        a = start_s + span * i / n
        b = start_s + span * (i + 1) / n
        out.append(
            {
                "id": pid,
                "label": label,
                "start_time": round(a, 4),
                "end_time": round(b, 4),
            }
        )
    return out


def _build_series(
    seq: MotionSequence,
    spec: dict[str, Any],
    start_s: float,
    end_s: float,
) -> dict[str, Any] | None:
    sl = slice_indices(seq.timestamps, start_s, end_s)
    times = np.asarray(seq.timestamps[sl], dtype=np.float64)
    if times.size < 4:
        return None
    kind = spec["kind"]
    if kind == "coord":
        raw = seq.opensim_coordinates.get(spec["coord"])
        if raw is None:
            return None
        values = np.degrees(np.asarray(raw[sl], dtype=np.float64))
        if spec.get("zero_at_start"):
            values = values - float(values[0])
    elif kind == "speed":
        joint = spec["joint"]
        if joint not in seq.joint_positions:
            return None
        dt = float(seq.metadata.get("dt") or (1.0 / max(seq.fps, 1.0)))
        vel = velocity_of(seq.joint_positions[joint], dt)
        values = np.linalg.norm(vel[sl], axis=-1)
    elif kind == "drift":
        joint = spec["joint"]
        if joint not in seq.joint_positions:
            return None
        pos = np.asarray(seq.joint_positions[joint][sl], dtype=np.float64)
        horizontal = pos[:, [0, 2]] - pos[0, [0, 2]]
        values = np.linalg.norm(horizontal, axis=-1) * 100.0
    elif kind == "stride":
        lead = spec["lead"]
        trail = spec["trail"]
        if lead not in seq.joint_positions or trail not in seq.joint_positions:
            return None
        a = np.asarray(seq.joint_positions[lead][sl], dtype=np.float64)
        b = np.asarray(seq.joint_positions[trail][sl], dtype=np.float64)
        dist = np.linalg.norm((a - b)[:, [0, 2]], axis=-1) * 100.0
        values = dist
    else:
        return None
    if not np.any(np.isfinite(values)):
        return None
    times_d, values_d = _downsample(times, values, MAX_SAMPLES)
    return {
        "id": spec["id"],
        "label": spec["label"],
        "unit": spec["unit"],
        "times": [round(float(t), 4) for t in times_d],
        "values": [round(float(v), 3) for v in values_d],
    }


def _downsample(times: np.ndarray, values: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    if times.size <= n:
        return times, values
    idx = np.linspace(0, times.size - 1, n).astype(int)
    return times[idx], values[idx]


def _landmarks(series: list[dict[str, Any]], impact_time: float | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for trace in series:
        times = trace["times"]
        values = trace["values"]
        if not times:
            continue
        pairs = list(zip(times, values))
        peak = max(pairs, key=lambda p: p[1])
        trough = min(pairs, key=lambda p: p[1])
        out.append(
            {
                "id": f"{trace['id']}-peak",
                "series": trace["id"],
                "label": f"{trace['label']} peak",
                "time": peak[0],
                "value": peak[1],
                "unit": trace["unit"],
            }
        )
        out.append(
            {
                "id": f"{trace['id']}-min",
                "series": trace["id"],
                "label": f"{trace['label']} minimum",
                "time": trough[0],
                "value": trough[1],
                "unit": trace["unit"],
            }
        )
        if impact_time is not None:
            nearest = min(pairs, key=lambda p: abs(p[0] - impact_time))
            out.append(
                {
                    "id": f"{trace['id']}-event",
                    "series": trace["id"],
                    "label": f"{trace['label']} at release/impact",
                    "time": nearest[0],
                    "value": nearest[1],
                    "unit": trace["unit"],
                }
            )
    return out


def _fmt(value: float, unit: str) -> str:
    if unit == "°":
        return f"{value:.0f}°" if abs(value) >= 10 else f"{value:.1f}°"
    if unit == "cm":
        return f"{value:.1f} cm"
    if unit == "m/s":
        return f"{value:.2f} m/s"
    return f"{value:.2f} {unit}"


def _by_id(series: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {trace["id"]: trace for trace in series}


def _phase_span(phases: list[dict[str, Any]], *ids: str) -> tuple[float, float] | None:
    for phase in phases:
        if phase.get("id") in ids:
            return float(phase["start_time"]), float(phase["end_time"])
    return None


def _at(trace: dict[str, Any] | None, time_s: float) -> tuple[float, float] | None:
    if not trace or not trace.get("times"):
        return None
    pair = min(zip(trace["times"], trace["values"]), key=lambda item: abs(item[0] - time_s))
    return float(pair[0]), float(pair[1])


def _finding(
    *,
    fid: str,
    title: str,
    detail: str,
    series: str,
    start: float,
    end: float,
    metric_value: str,
    caption: str,
    why: str,
    cue: str,
    drill: str,
    check: str,
    accent: str,
    group: dict[str, str] | None = None,
    goal: dict[str, Any] | None = None,
    strength: bool = False,
    headline: str | None = None,
    plain: str | None = None,
    severity: float = 0.0,
) -> dict[str, Any]:
    return {
        "id": fid,
        "title": title,
        "headline": headline or title,
        "plain": plain or detail,
        "detail": detail,
        "series": series,
        "start_time": round(start, 4),
        "end_time": round(end, 4),
        "metric": {"value": metric_value, "caption": caption},
        "why": why,
        "cue": cue,
        "drill": drill,
        "check": check,
        "accent": accent,
        "group": group,
        "goal": goal,
        "strength": strength,
        "severity": float(severity),
        "focus": None,
    }


def _compose_report(
    sport: str,
    series: list[dict[str, Any]],
    phases: list[dict[str, Any]],
    impact_time: float | None,
    lead: str,
    trail: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    traces = _by_id(series)
    if not traces:
        return [], []
    t0 = float(series[0]["times"][0])
    t1 = float(series[0]["times"][-1])
    if sport == "baseball":
        return _baseball_report(traces, phases, impact_time, lead, trail, t0, t1)
    return _golf_report(traces, phases, impact_time, lead, trail, t0, t1)


def _golf_report(
    traces: dict[str, dict[str, Any]],
    phases: list[dict[str, Any]],
    impact_time: float | None,
    lead: str,
    trail: str,
    t0: float,
    t1: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    address = _phase_span(phases, "address") or (t0, t0)
    top = _phase_span(phases, "top", "transition", "backswing") or (t0, (t0 + t1) / 2)
    down = _phase_span(phases, "downswing", "impact") 
    impact_t = impact_time if impact_time is not None else (down[1] if down else (t0 + t1) * 0.55)
    follow = _phase_span(phases, "follow_through", "follow", "finish") or (impact_t, t1)

    findings: list[dict[str, Any]] = []
    highlights: list[str] = []
    trail_name = "Right" if trail == "right" else "Left"
    lead_name = "Left" if lead == "left" else "Right"

    knee = traces.get("trail-knee")
    a = _at(knee, address[1])
    b = _at(knee, top[1])
    if knee and a and b:
        drop = a[1] - b[1]
        if drop >= 8:
            findings.append(_finding(
                fid="trail-knee",
                title="Trail knee straightens",
                headline="Your back knee locks too early",
                plain=(
                    f"On the way to the top, your back ({trail_name.lower()}) knee straightens "
                    f"instead of staying soft ({a[1]:.0f}° → {b[1]:.0f}°). "
                    "That stands you up and steals hip turn."
                ),
                detail=(
                    f"{trail_name}-knee flexion drops from about {a[1]:.0f}° to {b[1]:.0f}° "
                    "in the backswing, limiting hip turn."
                ),
                series="trail-knee",
                start=address[1],
                end=top[1],
                metric_value=f"{a[1]:.0f}° → {b[1]:.0f}°",
                caption="back-knee bend, address → top",
                why="When the back knee locks, the hips stop turning and the arms have to invent the swing.",
                cue="Keep a soft back knee to the top — like sitting slightly into that hip.",
                drill="Feet-together backswings. Make ten slow backswings keeping the trail knee flexed, then one full swing.",
                check=f"Trail knee flex at the top stays near {a[1]:.0f}° instead of falling to {b[1]:.0f}°.",
                accent="#c4a6ff",
                severity=40 + min(drop, 30),
                group={
                    "id": "hip-rotators",
                    "label": f"{trail_name} piriformis · glute medius",
                    "role": "Holds the trail hip deep so the pelvis can turn.",
                    "color": "#c4a6ff",
                },
            ))
        else:
            highlights.append(
                f"Trail knee stays loaded: {a[1]:.0f}° at address, {b[1]:.0f}° at the top."
            )

    elbow = traces.get("lead-elbow")
    e0 = _at(elbow, address[0])
    e1 = _at(elbow, top[1])
    if elbow and e0 and e1:
        if e1[1] - e0[1] >= 15 or e1[1] >= 45:
            findings.append(_finding(
                fid="lead-arm",
                title="Lead arm bends",
                headline="Your front arm collapses at the top",
                plain=(
                    f"At the top of the backswing your front ({lead_name.lower()}) arm bends a lot "
                    f"(about {e1[1]:.0f}°, vs ~{e0[1]:.0f}° at address). "
                    "That shortens the swing arc, so the club has less room to build speed."
                ),
                detail=(
                    f"{lead_name}-elbow flexion reaches about {e1[1]:.0f}° at the top, "
                    f"from {e0[1]:.0f}° at address, shortening the arc."
                ),
                series="lead-elbow",
                start=top[0],
                end=max(top[1], top[0] + 0.04),
                metric_value=f"{e1[1]:.0f}°",
                caption="front-arm bend at the top",
                why="A bent lead arm narrows the swing radius, so the clubhead has less time to build speed.",
                cue="At the top, keep the front arm long — stretch it away from your head, not chicken-winged in.",
                drill="Pump drill. Pause at the top with the lead arm extended, pump down halfway twice, then swing through. Eight reps.",
                check=f"Lead elbow flex at the top stays under {max(e0[1] + 10, 30):.0f}° instead of {e1[1]:.0f}°.",
                accent="#8fd0ff",
                severity=35 + min(max(e1[1] - 30, 0), 40),
                goal={"value": round(e0[1], 1), "label": "address flex"},
            ))
        else:
            highlights.append(
                f"Lead arm stays long: elbow flex is {e1[1]:.0f}° at the top."
            )

    sep = traces.get("separation")
    s1 = _at(sep, top[1])
    if sep and s1:
        if abs(s1[1]) < 20:
            findings.append(_finding(
                fid="separation",
                title="Hips and shoulders turn together",
                headline="Hips and chest are turning as one block",
                plain=(
                    f"At the top there’s almost no twist between your hips and chest "
                    f"(only about {abs(s1[1]):.0f}° of separation). "
                    "Without that stretch, the arms have to create all the speed."
                ),
                detail=(
                    f"At the top, hip–shoulder separation is only about {s1[1]:.0f}° "
                    "from address."
                ),
                series="separation",
                start=top[0],
                end=max(top[1], top[0] + 0.04),
                metric_value=f"{s1[1]:.0f}°",
                caption="hip–shoulder twist at the top",
                why="The stretch between a turned pelvis and a closed chest is what stores the downswing. With little of it, the arms have to make the speed.",
                cue="Turn the belt buckle first; keep the chest facing the side a beat longer.",
                drill="Hip-lead half swings. Start the pelvis, hold the shoulders for one beat, then swing. Ten reps.",
                check=f"Separation at the top grows past {abs(s1[1]):.0f}°.",
                accent="#ffb84d",
                severity=45 + max(0, 20 - abs(s1[1])),
            ))
        else:
            highlights.append(
                f"Separation at the top is about {s1[1]:.0f}° from address."
            )

    drift = traces.get("pelvis-drift")
    d0 = _at(drift, impact_t)
    d1 = _at(drift, min(t1, impact_t + 0.28))
    if drift and d0 and d1 and (d1[1] - d0[1]) >= 3.5:
        drift_cm = d1[1] - d0[1]
        findings.append(_finding(
            fid="early-extension",
            title="Early extension",
            headline="You're standing up through the ball",
            plain=(
                f"Right after impact your hips thrust toward the ball "
                f"(about {drift_cm:.0f} cm of pelvis drift). "
                "That pops you out of posture and sends the club up and out instead of through the shot."
            ),
            detail=(
                f"The pelvis moves about {drift_cm:.0f} cm farther from address "
                "between impact and the follow-through, losing spine angle."
            ),
            series="pelvis-drift",
            start=impact_t,
            end=min(t1, follow[1]),
            metric_value=f"{drift_cm:.0f} cm",
            caption="hips drifting toward the ball after impact",
            why="Standing up through impact sends the club out and up instead of down the target line.",
            cue="Through the ball, keep the same spine tilt you had at address — trail hip stays back.",
            drill="Chair drill. Set a chair just behind the hips and make ten swings without touching it.",
            check=f"Pelvis drift from impact to follow stays near {d0[1]:.0f} cm instead of adding {drift_cm:.0f} cm.",
            accent="#ff8fbf",
            severity=55 + min(drift_cm * 2.5, 35),
            group={
                "id": "hip-extensors",
                "label": "Glute max · hamstrings",
                "role": "Keeps the pelvis from thrusting toward the ball.",
                "color": "#ff8fbf",
            },
            goal={"value": round(d0[1], 1), "label": "drift at impact"},
        ))
    elif drift and d1:
        highlights.append(f"Pelvis drift from address stays about {d1[1]:.0f} cm through the finish.")

    hand = traces.get("trail-hand")
    if hand and hand["values"]:
        peak = max(zip(hand["times"], hand["values"]), key=lambda item: item[1])
        highlights.append(f"Trail-hand speed peaks at {peak[1]:.2f} m/s at {peak[0]:.2f} s.")

    if len(findings) < 2:
        findings.extend(_fallback_readings(traces, t0, t1, lead, trail, accents=("#c4a6ff", "#8fd0ff", "#ffb84d")))
    # Biggest fix first — not whatever rule fired first in code order.
    findings.sort(key=lambda note: float(note.get("severity") or 0.0), reverse=True)
    return _stamp_focus(findings[:4], lead, trail), highlights[:4]


def _baseball_report(
    traces: dict[str, dict[str, Any]],
    phases: list[dict[str, Any]],
    impact_time: float | None,
    lead: str,
    trail: str,
    t0: float,
    t1: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    stride = _phase_span(phases, "stride") or (t0, (t0 + t1) * 0.5)
    landing = _phase_span(phases, "landing") or (stride[1], min(t1, stride[1] + 0.12))
    release = _phase_span(phases, "release")
    release_t = impact_time if impact_time is not None else (release[0] if release else landing[1])
    release_end = release[1] if release else min(t1, release_t + 0.15)
    findings: list[dict[str, Any]] = []
    highlights: list[str] = []
    trail_name = "Right" if trail == "right" else "Left"
    lead_name = "Left" if lead == "left" else "Right"

    back = traces.get("back-knee")
    b0 = _at(back, stride[0])
    b1 = _at(back, landing[0])
    if back and b0 and b1:
        if b1[1] > 25 and (b0[1] - b1[1]) < 8:
            findings.append(_finding(
                fid="back-leg",
                title="Back leg stays bent",
                detail=(
                    f"As you stride, your back knee is about {b1[1]:.0f}° "
                    f"and is still bent when the front foot lands"
                    + (f" (it was {b0[1]:.0f}° as the stride began)." if abs(b0[1] - b1[1]) >= 1 else ".")
                ),
                series="back-knee",
                start=stride[0],
                end=landing[1],
                metric_value=f"{b1[1]:.0f}°",
                caption="back knee flex at landing",
                why="The back leg is what pushes you off the rubber. If it is still folded at landing, the push has not finished, and the hips arrive with less speed to pass on.",
                cue="Push the rubber away. Feel the back leg straighten before the front foot touches down.",
                drill="Rocker drill. Set your feet in the landing position, rock back onto the back leg, then drive off it and throw at half effort. Ten reps.",
                check=f"Back knee flex is falling in the frames before landing instead of holding near {b1[1]:.0f}°.",
                accent="#c4a6ff",
                group={
                    "id": "drive-leg",
                    "label": f"{trail_name} glute max · quadriceps",
                    "role": "Finishes the push off the rubber before the front foot lands.",
                    "color": "#c4a6ff",
                },
            ))
        else:
            highlights.append(
                f"Back leg drives: knee flex goes from {b0[1]:.0f}° to {b1[1]:.0f}° by landing."
            )

    sep = traces.get("separation")
    s1 = _at(sep, landing[0])
    if sep and s1:
        if abs(s1[1]) < 25:
            findings.append(_finding(
                fid="separation",
                title="Hips and shoulders turn together",
                detail=f"At landing your hips are about {s1[1]:.0f}° ahead of your shoulders.",
                series="separation",
                start=landing[0],
                end=landing[1],
                metric_value=f"{s1[1]:.0f}°",
                caption="hips ahead of shoulders at landing",
                why="The stretch between open hips and closed shoulders is what whips the trunk around. With little of it, the arm has to make up the speed.",
                cue="Let the belt buckle turn to the plate while the chest still faces the side.",
                drill="Hip-lead throws. From a narrow stance, start the hips, hold the shoulders closed for one beat, then throw at half effort. Ten reps.",
                check=f"Separation at landing grows beyond {abs(s1[1]):.0f}°.",
                accent="#ffb84d",
            ))
        else:
            highlights.append(f"Hip–shoulder separation at landing is about {s1[1]:.0f}°.")

    front = traces.get("front-knee")
    f0 = _at(front, landing[0])
    f1 = _at(front, release_t)
    if front and f0 and f1:
        if f1[1] - f0[1] >= 8:
            findings.append(_finding(
                fid="front-knee",
                title="Front knee gives after landing",
                detail=(
                    f"Your front knee lands at {f0[1]:.0f}° and keeps bending to about "
                    f"{f1[1]:.0f}° before release."
                ),
                series="front-knee",
                start=landing[0],
                end=release_end,
                metric_value=f"{f0[1]:.0f}° → {f1[1]:.0f}°",
                caption="front knee flex, landing to release",
                why="The front leg is the brake. When it keeps bending, the energy from the stride goes down into the knee instead of up through the trunk to the ball.",
                cue="Land and stop. Feel the front thigh catch you, then push back against the ground as you throw.",
                drill="Stride-and-brace. Stride out, land, and hold the front knee still for two counts before throwing at half effort. Ten reps.",
                check=f"Front knee flex stays near {f0[1]:.0f}° from landing to release instead of reaching {f1[1]:.0f}°.",
                accent="#7dffb3",
                group={
                    "id": "lead-quad",
                    "label": f"{lead_name} quadriceps",
                    "role": "Braces the front leg so the stride stops and the trunk can whip.",
                    "color": "#7dffb3",
                },
                goal={"value": round(f0[1], 1), "label": "landing flex"},
            ))
        else:
            highlights.append(
                f"Front knee braces: {f0[1]:.0f}° at landing, {f1[1]:.0f}° at release."
            )

    stride_trace = traces.get("stride")
    if stride_trace and stride_trace["values"]:
        peak = max(zip(stride_trace["times"], stride_trace["values"]), key=lambda item: item[1])
        start_v = float(stride_trace["values"][0])
        highlights.insert(
            0,
            f"Stride length reaches {peak[1]:.0f} cm (from {start_v:.0f} cm at the start of the window).",
        )

    if len(findings) < 2:
        findings.extend(_fallback_readings(traces, t0, t1, lead, trail, accents=("#c4a6ff", "#ffb84d", "#7dffb3")))
    return _stamp_focus(findings[:4], lead, trail), highlights[:4]


def _body_side(side: str) -> str:
    return "l" if side == "left" else "r"


def _focus_for(kind: str, side: str | None, yaw: float, label: str) -> dict[str, Any]:
    """OpenSim body the close-up should frame. Names match rajagopal-bones.glb."""
    if kind == "knee" and side:
        s = _body_side(side)
        return {
            "anchor": f"tibia_{s}",
            "bones": [f"femur_{s}", f"tibia_{s}", f"patella_{s}"],
            "view_yaw": yaw,
            "label": label,
        }
    if kind == "arm" and side:
        s = _body_side(side)
        return {
            "anchor": f"ulna_{s}",
            "bones": [f"humerus_{s}", f"ulna_{s}", f"radius_{s}"],
            "view_yaw": yaw,
            "label": label,
        }
    if kind == "hand" and side:
        s = _body_side(side)
        return {
            "anchor": f"hand_{s}",
            "bones": [f"ulna_{s}", f"radius_{s}", f"hand_{s}"],
            "view_yaw": yaw,
            "label": label,
        }
    if kind == "ankle" and side:
        s = _body_side(side)
        return {
            "anchor": f"talus_{s}",
            "bones": [f"tibia_{s}", f"talus_{s}", f"calcn_{s}"],
            "view_yaw": yaw,
            "label": label,
        }
    if kind == "trunk":
        return {
            "anchor": "torso",
            "bones": ["pelvis", "torso"],
            "view_yaw": yaw,
            "label": label,
        }
    return {
        "anchor": "pelvis",
        "bones": ["pelvis"],
        "view_yaw": yaw,
        "label": label,
    }


def _stamp_focus(
    findings: list[dict[str, Any]],
    lead: str,
    trail: str,
) -> list[dict[str, Any]]:
    """Point each fix at the limb the website close-up would frame, and say how to mark it on the video."""
    catalog = {
        "trail-knee": _focus_for("knee", trail, -32, "Trail knee"),
        "lead-arm": _focus_for("arm", lead, 36, "Lead arm"),
        "separation": _focus_for("trunk", None, 0, "Hips and chest"),
        "early-extension": _focus_for("pelvis", None, 24, "Pelvis"),
        "back-leg": _focus_for("knee", trail, -32, "Back leg"),
        "front-knee": _focus_for("knee", lead, 32, "Front knee"),
    }
    series_focus = {
        "trail-knee": catalog["trail-knee"],
        "lead-elbow": catalog["lead-arm"],
        "separation": catalog["separation"],
        "pelvis-turn": catalog["separation"],
        "pelvis-drift": catalog["early-extension"],
        "back-knee": catalog["back-leg"],
        "front-knee": catalog["front-knee"],
        "stride": _focus_for("ankle", lead, 28, "Front foot"),
        "trail-hand": _focus_for("hand", trail, -40, "Trail hand"),
        "throwing-hand": _focus_for("hand", trail, -40, "Throwing hand"),
    }
    from motion_agent.motion.swing_overlay import overlay_for_finding

    for note in findings:
        note["focus"] = catalog.get(note["id"]) or series_focus.get(note.get("series") or "") or _focus_for(
            "pelvis", None, 0, "Body"
        )
        note["overlay"] = overlay_for_finding(note["id"], note.get("series"), lead, trail)
    return findings


def _fallback_readings(
    traces: dict[str, dict[str, Any]],
    t0: float,
    t1: float,
    lead: str,
    trail: str,
    accents: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Keep the plan as deep as the site when a clip does not trip a fault rule."""
    out = []
    for trace, accent in zip(traces.values(), accents):
        if not trace["values"]:
            continue
        peak = max(zip(trace["times"], trace["values"]), key=lambda item: abs(item[1]))
        out.append(_finding(
            fid=f"read-{trace['id']}",
            title=trace["label"],
            detail=(
                f"{trace['label']} moves to {_fmt(peak[1], trace['unit'])} at {peak[0]:.2f} s "
                "inside this window."
            ),
            series=trace["id"],
            start=t0,
            end=t1,
            metric_value=_fmt(peak[1], trace["unit"]),
            caption=f"{trace['label']} extreme",
            why="This is the largest change on this trace. Read the shape against the phase bar before treating it as a fault.",
            cue="Replay the highlighted stretch and watch whether the change happens before or after the event marker.",
            drill="Film the same movement again from the side and compare this trace on the next capture.",
            check=f"The next capture should show whether {_fmt(peak[1], trace['unit'])} was the pattern or a one-off.",
            accent=accent,
        ))
        if len(out) >= 3:
            break
    return out
