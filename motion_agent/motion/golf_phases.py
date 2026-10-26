"""Deterministic golf swing / phase segmentation from OpenSim kinematics.

Uses DETR-style bipartite (Hungarian) matching so each swing hypothesis is
assigned **at most one** impact peak — no duplicate swings / repeated cards.

Pipeline:
1. Build non-overlapping *activity segments* (swing hypotheses) from wrist speed.
2. Collect impact *peak candidates*.
3. Match segments ↔ peaks with ``linear_sum_assignment`` (one-to-one).
4. Partition time exclusively between matched swings, then emit unique phases.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

from motion_agent.motion.sequence import MotionSequence, velocity_of
from motion_agent.motion.synchronization import slice_indices
from motion_agent.storage.errors import tool_error

PHASE_ORDER = (
    "address",
    "takeaway",
    "backswing",
    "transition",
    "downswing",
    "impact",
    "follow_through",
)

PHASE_LABELS = {
    "address": "Address",
    "takeaway": "Takeaway",
    "backswing": "Backswing",
    "transition": "Transition",
    "downswing": "Downswing",
    "impact": "Impact",
    "follow_through": "Follow-through",
}

# Nominal minimum dwell per phase (seconds). Scaled down for short swings so
# address/takeaway/backswing are not merged away. Blocks micro noise peaks.
PHASE_MIN_DURATION_S: dict[str, float] = {
    "address": 0.10,
    "takeaway": 0.10,
    "backswing": 0.16,
    "transition": 0.06,
    "downswing": 0.14,
    "impact": 0.04,
    "follow_through": 0.16,
}

# Cost for matching a peak to the null / no-object slot (DETR-style ∅).
_NO_OBJECT_COST = 1.35

# Takeaway → top is under ~1.5 s even for a slow full swing. A longer span means
# setup motion (walking in, waggles) tripped the onset rule before the real takeaway.
MAX_BACKSWING_S = 2.0
# The backswing moves the hands at a good share of strike speed. A smaller hump just
# before the top is a twitch while paused there, not the takeaway.
BACKSWING_MIN_SHARE = 0.25


def _scaled_phase_mins(swing_duration_s: float) -> dict[str, float]:
    """Scale nominal mins so all seven phases can fit in the swing window."""
    base = dict(PHASE_MIN_DURATION_S)
    total = float(sum(base.values()))
    budget = max(float(swing_duration_s) * 0.92, 0.35)
    if budget >= total:
        return base
    scale = budget / total
    # Keep impact from collapsing to nothing.
    out = {k: max(0.03 if k != "impact" else 0.035, v * scale) for k, v in base.items()}
    return out


def _filter_peaks_for_swing_count(
    peaks: list[dict[str, Any]],
    *,
    swing_count: int | None,
    max_swings: int,
    dt: float,
) -> list[dict[str, Any]]:
    """Keep only peaks that should become swings (LLM count or conservative auto)."""
    if not peaks:
        return []
    ranked = sorted(peaks, key=lambda p: float(p["score"]), reverse=True)
    best = ranked[0]
    best_speed = max(float(p["speed"]) for p in ranked)
    min_sep = max(3, int(1.6 / max(dt, 1e-3)))

    if swing_count is not None:
        n = max(1, min(int(swing_count), max_swings, len(ranked)))
        chosen = ranked[:n]
        chosen.sort(key=lambda p: int(p["index"]))
        return chosen

    # Auto: secondary peaks must be nearly as strong as the best impact.
    # A practice waggle / noise blip at ~30% peak speed must not become a swing.
    kept = [best]
    for p in ranked[1:]:
        if len(kept) >= max_swings:
            break
        if float(p["speed"]) < 0.58 * best_speed:
            continue
        if float(p["score"]) < 0.55 * float(best["score"]):
            continue
        if any(abs(int(p["index"]) - int(k["index"])) < min_sep for k in kept):
            continue
        kept.append(p)
    kept.sort(key=lambda p: int(p["index"]))
    return kept


def _moving_average(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1 or x.size < 3:
        return x.astype(float)
    win = min(win, max(3, x.size // 3))
    if win % 2 == 0:
        win += 1
    pad = win // 2
    kernel = np.ones(win, dtype=float) / win
    xp = np.pad(x.astype(float), (pad, pad), mode="edge")
    return np.convolve(xp, kernel, mode="valid")


def _speed_mag(pos: np.ndarray, dt: float) -> np.ndarray:
    vel = velocity_of(pos, dt)
    return np.linalg.norm(vel, axis=-1)


def _resolve_handedness(handedness: str) -> tuple[str, str, str]:
    """Map handedness → (hand, lead_side, trail_side).

    Wrist-speed auto-inference was systematically unreliable (mesh L/R noise and
    similar peak speeds), so ``auto`` defaults to right-handed. Pass
    ``handedness="left"`` only when the user or operator asserts a lefty.
    """
    key = (handedness or "right").strip().lower()
    if key in ("auto", "right", ""):
        return "right", "left", "right"
    if key == "left":
        return "left", "right", "left"
    raise ValueError(f"handedness must be auto|right|left, got {handedness!r}")


def _local_maxima(series: np.ndarray, order: int) -> list[int]:
    out: list[int] = []
    if series.size < 2 * order + 1:
        return out
    for i in range(order, series.size - order):
        window = series[i - order : i + order + 1]
        if series[i] == np.max(window) and series[i] >= series[i - 1] and series[i] >= series[i + 1]:
            out.append(i)
    return out


def _local_minima(series: np.ndarray, order: int) -> list[int]:
    out: list[int] = []
    if series.size < 2 * order + 1:
        return out
    for i in range(order, series.size - order):
        window = series[i - order : i + order + 1]
        if series[i] == np.min(window) and series[i] <= series[i - 1] and series[i] <= series[i + 1]:
            out.append(i)
    return out


def _activity_segments(
    speed: np.ndarray,
    ts: np.ndarray,
    active_thr: float,
    dt: float,
    *,
    min_duration_s: float = 0.9,
    merge_gap_s: float = 0.45,
) -> list[dict[str, Any]]:
    """Connected high-activity regions — one hypothesized swing each."""
    active = speed >= active_thr
    if not np.any(active):
        # Fallback: whole clip as one segment centered on global max.
        peak = int(np.argmax(speed))
        return [
            {
                "i0": 0,
                "i1": len(speed) - 1,
                "peak_hint": peak,
                "mass": float(np.sum(speed)),
            }
        ]

    raw: list[tuple[int, int]] = []
    i = 0
    n = len(active)
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < n and active[j]:
            j += 1
        raw.append((i, j - 1))
        i = j

    merge_gap = max(1, int(merge_gap_s / dt))
    merged: list[tuple[int, int]] = []
    for a0, a1 in raw:
        if not merged:
            merged.append((a0, a1))
            continue
        b0, b1 = merged[-1]
        if a0 - b1 <= merge_gap:
            merged[-1] = (b0, a1)
        else:
            merged.append((a0, a1))

    min_len = max(3, int(min_duration_s / dt))
    segments: list[dict[str, Any]] = []
    for a0, a1 in merged:
        if a1 - a0 + 1 < min_len:
            # Short blip: keep only if it contains a strong local peak relative to thr.
            if float(np.max(speed[a0 : a1 + 1])) < active_thr * 1.8:
                continue
        peak_hint = int(a0 + np.argmax(speed[a0 : a1 + 1]))
        # Expand slightly into quiet borders for address / follow-through room.
        pad = max(1, int(0.25 / dt))
        i0 = max(0, a0 - pad)
        i1 = min(len(speed) - 1, a1 + pad)
        segments.append(
            {
                "i0": i0,
                "i1": i1,
                "core0": a0,
                "core1": a1,
                "peak_hint": peak_hint,
                "mass": float(np.sum(speed[a0 : a1 + 1])),
            }
        )

    if not segments:
        peak = int(np.argmax(speed))
        return [
            {
                "i0": 0,
                "i1": len(speed) - 1,
                "core0": 0,
                "core1": len(speed) - 1,
                "peak_hint": peak,
                "mass": float(np.sum(speed)),
            }
        ]
    return segments


def _peak_candidates(
    speed: np.ndarray,
    peak_floor: float,
    order: int,
) -> list[dict[str, Any]]:
    idxs = [
        i
        for i in _local_maxima(speed, order)
        if speed[i] >= peak_floor and order < i < len(speed) - order
    ]
    if not idxs:
        idxs = [int(np.argmax(speed))]
    # Score = normalized height × local prominence.
    mx = float(np.max(speed)) + 1e-6
    cands: list[dict[str, Any]] = []
    for i in idxs:
        left = float(np.min(speed[max(0, i - order * 3) : i + 1]))
        right = float(np.min(speed[i : min(len(speed), i + order * 3 + 1)]))
        prominence = float(speed[i]) - max(left, right)
        score = float(speed[i]) / mx + max(0.0, prominence) / mx
        cands.append({"index": i, "score": score, "speed": float(speed[i])})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands


def _match_segments_to_peaks(
    segments: list[dict[str, Any]],
    peaks: list[dict[str, Any]],
    ts: np.ndarray,
    *,
    max_swings: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """DETR-style Hungarian one-to-one matching: each segment ↔ ≤1 peak.

    Cost favors high-scoring peaks inside the segment core; peaks far outside
    pay a large temporal penalty. Extra peaks can match a padded no-object row
    so they are discarded instead of duplicating a swing.
    """
    if not segments or not peaks:
        return []

    # Cap segments by mass so we never emit more swings than requested.
    segs = sorted(segments, key=lambda s: s["mass"], reverse=True)[:max_swings]
    segs = sorted(segs, key=lambda s: s["i0"])

    n_seg = len(segs)
    n_peak = len(peaks)
    # Rows: segments + n_peak no-object pads (so every peak can be unmatched).
    n_rows = n_seg + n_peak
    cost = np.full((n_rows, n_peak), _NO_OBJECT_COST, dtype=float)

    duration = float(max(ts[-1] - ts[0], 1e-3))
    for si, seg in enumerate(segs):
        core0, core1 = int(seg.get("core0", seg["i0"])), int(seg.get("core1", seg["i1"]))
        center = 0.5 * (float(ts[seg["i0"]]) + float(ts[seg["i1"]]))
        for pj, peak in enumerate(peaks):
            pi = int(peak["index"])
            t = float(ts[pi])
            # Base: prefer higher score (lower cost).
            c = 1.0 - float(peak["score"])
            if core0 <= pi <= core1:
                c -= 0.55  # strong bonus for peak inside activity core
            elif seg["i0"] <= pi <= seg["i1"]:
                c -= 0.25
            else:
                # Outside segment: temporal distance penalty (normalized).
                if pi < seg["i0"]:
                    dist = float(ts[seg["i0"]]) - t
                else:
                    dist = t - float(ts[seg["i1"]])
                c += 0.9 + 2.5 * (dist / duration)
            # Soft preference for nearer-to-center peaks inside a segment.
            c += 0.15 * abs(t - center) / duration
            cost[si, pj] = c

    # No-object rows already filled with _NO_OBJECT_COST.

    row_ind, col_ind = linear_sum_assignment(cost)
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used_peaks: set[int] = set()
    used_segs: set[int] = set()
    for r, c in zip(row_ind, col_ind):
        if r >= n_seg:
            continue  # peak matched to ∅
        if cost[r, c] >= _NO_OBJECT_COST * 0.98:
            # Assignment no better than discarding — skip weak matches.
            continue
        if r in used_segs or c in used_peaks:
            continue
        used_segs.add(r)
        used_peaks.add(c)
        matched.append((segs[r], peaks[c]))

    # Sort by time for stable swing_index.
    matched.sort(key=lambda sp: int(sp[1]["index"]))
    return matched


def _exclusive_windows(
    matched: list[tuple[dict[str, Any], dict[str, Any]]],
    n: int,
) -> list[tuple[int, int, int]]:
    """Return (i0, impact_i, i1) with non-overlapping exclusive intervals."""
    if not matched:
        return []
    # Single swing: give the whole analysis window so early phases have room.
    if len(matched) == 1:
        impact_i = int(matched[0][1]["index"])
        return [(0, impact_i, n - 1)]

    impacts = [int(p["index"]) for _, p in matched]
    windows: list[tuple[int, int, int]] = []
    for k, (seg, peak) in enumerate(matched):
        impact_i = int(peak["index"])
        if k == 0:
            i0 = 0
        else:
            i0 = (impacts[k - 1] + impact_i) // 2 + 1
        if k == len(matched) - 1:
            i1 = n - 1
        else:
            i1 = (impact_i + impacts[k + 1]) // 2
        i0 = max(i0, int(seg["i0"]))
        i1 = min(i1, int(seg["i1"]))
        i0 = min(i0, impact_i)
        i1 = max(i1, impact_i)
        windows.append((i0, impact_i, i1))

    repaired: list[tuple[int, int, int]] = []
    prev_end = -1
    for i0, impact_i, i1 in windows:
        i0 = max(i0, prev_end + 1)
        if i0 > impact_i:
            i0 = impact_i
        if i1 < impact_i:
            i1 = impact_i
        if repaired and i0 <= repaired[-1][2]:
            p0, pimp, _ = repaired[-1]
            repaired[-1] = (p0, pimp, i0 - 1 if i0 > p0 else p0)
        repaired.append((i0, impact_i, i1))
        prev_end = repaired[-1][2]
    return repaired


def _phase(
    phase_id: str,
    start_time: float,
    end_time: float,
    *,
    key_time: float | None = None,
    confidence: float = 0.6,
    cues: list[str] | None = None,
) -> dict[str, Any]:
    t0 = float(min(start_time, end_time))
    t1 = float(max(start_time, end_time))
    if t1 - t0 < 1e-3:
        t1 = t0 + 0.04
    kt = float(key_time) if key_time is not None else 0.5 * (t0 + t1)
    kt = min(max(kt, t0), t1)
    return {
        "id": phase_id,
        "label": PHASE_LABELS[phase_id],
        "start_time": round(t0, 4),
        "end_time": round(t1, 4),
        "key_time": round(kt, 4),
        "confidence": round(float(confidence), 3),
        "cues": list(cues or []),
    }


def _phase_duration(p: dict[str, Any]) -> float:
    return float(p["end_time"]) - float(p["start_time"])


def _clamp_key(p: dict[str, Any]) -> None:
    p["key_time"] = round(
        min(max(float(p["key_time"]), float(p["start_time"])), float(p["end_time"])),
        4,
    )
    p["start_time"] = round(float(p["start_time"]), 4)
    p["end_time"] = round(float(p["end_time"]), 4)


def _merge_phase_into(
    phases: list[dict[str, Any]],
    idx: int,
    into: int,
) -> list[dict[str, Any]]:
    """Absorb phases[idx] into phases[into]; keep the survivor's id/label."""
    if idx == into or idx < 0 or into < 0 or idx >= len(phases) or into >= len(phases):
        return phases
    a, b = phases[idx], phases[into]
    survivor = b
    donor = a
    survivor["start_time"] = min(float(survivor["start_time"]), float(donor["start_time"]))
    survivor["end_time"] = max(float(survivor["end_time"]), float(donor["end_time"]))
    # Prefer impact key time if either side is impact.
    if survivor["id"] == "impact" or donor["id"] == "impact":
        if survivor["id"] != "impact" and donor["id"] == "impact":
            survivor["key_time"] = donor["key_time"]
        # else keep survivor key
    else:
        survivor["key_time"] = 0.5 * (
            float(survivor["start_time"]) + float(survivor["end_time"])
        )
    cues = list(dict.fromkeys([*(survivor.get("cues") or []), *(donor.get("cues") or []), "merged short phase"]))
    survivor["cues"] = cues[:4]
    _clamp_key(survivor)
    out = [p for i, p in enumerate(phases) if i != idx]
    return out


def _enforce_phase_minima(
    phases: list[dict[str, Any]],
    mins: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Iteratively merge phases shorter than mins into neighbors."""
    if not phases:
        return []
    min_map = mins or PHASE_MIN_DURATION_S
    cur = [dict(p) for p in phases]
    # Prefer merging ephemeral phases into these anchors when possible.
    # Keep address/takeaway/backswing over transition — transition is the
    # shortest intentional phase and the usual casualty of noise merges.
    prefer_keep = {
        "impact": 0,
        "downswing": 1,
        "backswing": 2,
        "follow_through": 3,
        "takeaway": 4,
        "address": 5,
        "transition": 6,
    }

    for _ in range(len(PHASE_ORDER) * 3):
        short_i = None
        for i, p in enumerate(cur):
            need = min_map.get(p["id"], 0.08)
            if _phase_duration(p) + 1e-9 < need:
                short_i = i
                break
        if short_i is None:
            break
        candidates: list[int] = []
        if short_i > 0:
            candidates.append(short_i - 1)
        if short_i + 1 < len(cur):
            candidates.append(short_i + 1)
        if not candidates:
            break
        into = min(
            candidates,
            key=lambda j: (
                prefer_keep.get(cur[j]["id"], 9),
                -_phase_duration(cur[j]),
            ),
        )
        cur = _merge_phase_into(cur, short_i, into)

    by_id = {p["id"]: p for p in cur}
    ordered = [by_id[pid] for pid in PHASE_ORDER if pid in by_id]
    return ordered


def _snap_contiguous(
    phases: list[dict[str, Any]],
    mins: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Make phases abut end-to-end without gaps/overlaps."""
    if not phases:
        return []
    min_map = mins or PHASE_MIN_DURATION_S
    ordered = [dict(p) for p in phases]
    for i in range(len(ordered) - 1):
        cur, nxt = ordered[i], ordered[i + 1]
        mid = 0.5 * (float(cur["end_time"]) + float(nxt["start_time"]))
        if cur["id"] == "impact":
            mid = max(mid, float(cur["key_time"]) + 0.01)
        if nxt["id"] == "impact":
            mid = min(mid, float(nxt["key_time"]) - 0.01)
        cur_min = min_map.get(cur["id"], 0.05)
        nxt_min = min_map.get(nxt["id"], 0.05)
        earliest = float(cur["start_time"]) + cur_min
        latest = float(nxt["end_time"]) - nxt_min
        if earliest <= latest:
            mid = min(max(mid, earliest), latest)
        else:
            span = float(nxt["end_time"]) - float(cur["start_time"])
            mid = float(cur["start_time"]) + span * (
                cur_min / max(cur_min + nxt_min, 1e-6)
            )
        cur["end_time"] = float(mid)
        nxt["start_time"] = float(mid)
        _clamp_key(cur)
        _clamp_key(nxt)
    return ordered


def _finalize_phases(
    phases: list[dict[str, Any]],
    mins: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Dedupe → min-duration merge → contiguous snap → re-check minima."""
    min_map = mins or PHASE_MIN_DURATION_S
    by_id: dict[str, dict[str, Any]] = {}
    for p in phases:
        if p["id"] not in by_id:
            by_id[p["id"]] = dict(p)
    ordered = [by_id[pid] for pid in PHASE_ORDER if pid in by_id]
    if not ordered:
        return []
    ordered = _snap_contiguous(ordered, min_map)
    ordered = _enforce_phase_minima(ordered, min_map)
    ordered = _snap_contiguous(ordered, min_map)
    keep: list[dict[str, Any]] = []
    for p in ordered:
        need = min_map.get(p["id"], 0.05) * 0.5
        if _phase_duration(p) >= need:
            keep.append(p)
        elif keep:
            keep[-1]["end_time"] = max(float(keep[-1]["end_time"]), float(p["end_time"]))
            _clamp_key(keep[-1])
    return keep


def _place_boundary(
    t_hint: float,
    t_lo: float,
    t_hi: float,
    *,
    min_before: float,
    min_after: float,
) -> float:
    """Clamp a boundary so both adjacent phases keep their minimum duration."""
    lo = t_lo + min_before
    hi = t_hi - min_after
    if lo > hi:
        # Not enough span — split remaining time by min weights.
        span = max(t_hi - t_lo, 1e-6)
        return t_lo + span * (min_before / max(min_before + min_after, 1e-6))
    return min(max(t_hint, lo), hi)


def _backswing_onset(
    ts: np.ndarray,
    speed: np.ndarray,
    swing_i0: int,
    top_i: int,
    impact_i: int,
    quiet: float,
) -> int | None:
    """First moving frame of the backswing, found walking back from the top.

    The backswing is the hand-speed hump just before the top. Walking back from its
    peak to where the hands are still again gives the takeaway, however much setup
    motion comes before it.
    """
    lo = max(swing_i0, int(np.searchsorted(ts, float(ts[top_i]) - MAX_BACKSWING_S)))
    if top_i - lo < 3:
        return None
    peak_i = lo + int(np.argmax(speed[lo:top_i]))
    if speed[peak_i] < BACKSWING_MIN_SHARE * float(speed[impact_i]):
        return None
    still = max(quiet * 1.15, 0.2 * float(speed[peak_i]))
    for i in range(peak_i, swing_i0 - 1, -1):
        if speed[i] <= still:
            # Settle back to the stillest frame before the hump, not past quiet.
            while i > swing_i0 and quiet * 1.15 < speed[i - 1] < speed[i]:
                i -= 1
            return min(i + 1, peak_i)
    return None


def _build_phases(
    ts: np.ndarray,
    speed: np.ndarray,
    impact_i: int,
    swing_i0: int,
    swing_i1: int,
    quiet: float,
) -> list[dict[str, Any]]:
    t_start = float(ts[swing_i0])
    t_end = float(ts[swing_i1])
    t_impact = float(ts[impact_i])
    dt = float(ts[1] - ts[0]) if len(ts) > 1 else 0.033
    swing_dur = max(t_end - t_start, dt)
    pm = _scaled_phase_mins(swing_dur)

    order = max(2, int(0.08 / max(dt, 1e-3)))
    min_bs = pm["backswing"]
    min_ds = pm["downswing"]
    min_tr = pm["transition"]
    min_ad = pm["address"]
    min_tk = pm["takeaway"]
    min_im = pm["impact"]
    min_ft = pm["follow_through"]

    valley_lo_t = t_start + min_ad + min_tk + 0.5 * min_bs
    valley_hi_t = t_impact - (min_ds + min_tr + 0.5 * min_im)
    mins = []
    for i in _local_minima(speed, order):
        if not (swing_i0 <= i < impact_i - 1):
            continue
        ti = float(ts[i])
        if ti < valley_lo_t or ti > valley_hi_t:
            continue
        mins.append(i)
    if mins:
        top_i = mins[-1]
    else:
        lo = swing_i0
        hi = max(lo + 1, impact_i - 1)
        for j in range(swing_i0, impact_i):
            if float(ts[j]) >= valley_lo_t:
                lo = j
                break
        for j in range(impact_i - 1, swing_i0, -1):
            if float(ts[j]) <= valley_hi_t:
                hi = j
                break
        if hi <= lo:
            lo, hi = swing_i0, max(swing_i0 + 1, impact_i - 1)
        top_i = int(lo + np.argmin(speed[lo : hi + 1]))

    t_top = float(ts[top_i])

    take_i = swing_i0
    take_earliest_t = t_start + min_ad
    for i in range(swing_i0, max(swing_i0 + 1, top_i)):
        if float(ts[i]) < take_earliest_t:
            continue
        if speed[i] > quiet * 1.15:
            take_i = i
            break
    if t_top - float(ts[take_i]) > MAX_BACKSWING_S:
        onset = _backswing_onset(ts, speed, swing_i0, top_i, impact_i, quiet)
        if onset is not None and float(ts[onset]) >= take_earliest_t:
            take_i = onset
    t_take = _place_boundary(
        float(ts[take_i]),
        t_start,
        t_top,
        min_before=min_ad,
        min_after=min_tk + 0.5 * min_bs,
    )

    t_mid_hint = t_take + 0.45 * max(t_top - t_take, 0.0)
    t_mid = _place_boundary(
        t_mid_hint,
        t_take,
        t_top,
        min_before=min_tk,
        min_after=min_bs,
    )

    t_trans_hint = t_top + max(min_tr, 2.0 * dt)
    t_trans = _place_boundary(
        t_trans_hint,
        t_top,
        t_impact,
        min_before=min_tr,
        min_after=min_ds + 0.5 * min_im,
    )

    half = max(0.5 * min_im, 1.5 * dt)
    t_imp_a = _place_boundary(
        t_impact - half,
        t_trans,
        t_end,
        min_before=min_ds,
        min_after=min_im + min_ft * 0.5,
    )
    t_imp_b = _place_boundary(
        t_impact + half,
        t_imp_a,
        t_end,
        min_before=min_im,
        min_after=min_ft,
    )
    if not (t_imp_a <= t_impact <= t_imp_b):
        t_imp_a = min(t_imp_a, t_impact - 0.5 * min_im)
        t_imp_b = max(t_imp_b, t_impact + 0.5 * min_im)
        t_imp_a = max(t_imp_a, t_trans)
        t_imp_b = min(t_imp_b, t_end)

    follow_end_t = t_end
    settle_after = t_imp_b + min_ft
    for i in range(impact_i + 1, swing_i1 + 1):
        ti = float(ts[i])
        if ti < settle_after:
            continue
        if speed[i] < quiet * 1.25 and i > impact_i + max(2, order):
            if i + order < len(speed) and np.mean(speed[i : i + order]) < quiet * 1.4:
                follow_end_t = min(t_end, ti)
                break
    follow_end_t = max(follow_end_t, t_imp_b + min_ft)
    follow_end_t = min(follow_end_t, t_end)

    t_top = _place_boundary(
        t_top,
        t_mid,
        t_trans if t_trans > t_mid else t_impact,
        min_before=min_bs,
        min_after=min_tr,
    )

    phases = [
        _phase("address", t_start, t_take, key_time=t_start, confidence=0.55, cues=["low wrist speed"]),
        _phase("takeaway", t_take, t_mid, confidence=0.6, cues=["motion onset"]),
        _phase("backswing", t_mid, t_top, key_time=t_top, confidence=0.65, cues=["pre-impact speed valley"]),
        _phase("transition", t_top, t_trans, key_time=t_top, confidence=0.6, cues=["direction change"]),
        _phase("downswing", t_trans, t_imp_a, confidence=0.7, cues=["rising wrist speed"]),
        _phase("impact", t_imp_a, t_imp_b, key_time=t_impact, confidence=0.75, cues=["peak wrist speed"]),
        _phase(
            "follow_through",
            t_imp_b,
            follow_end_t,
            key_time=0.5 * (t_imp_b + follow_end_t),
            confidence=0.6,
            cues=[],
        ),
    ]
    return _finalize_phases(phases, pm)

def segment_golf_swings(
    seq: MotionSequence,
    start_time: float | None = None,
    end_time: float | None = None,
    handedness: str = "right",
    max_swings: int = 5,
    swing_count: int | None = None,
) -> dict[str, Any]:
    """Segment golf swings and phases with one-to-one Hungarian matching.

    Prefer passing ``swing_count`` from LLM reasoning (how many distinct full
    swings are in the clip). Auto mode keeps only peaks nearly as strong as the
    global max so weak blips are not promoted to swings.

    Handedness defaults to right-handed (lead=left, trail=right). ``auto`` is
    treated the same; pass ``left`` only when explicitly known.
    """
    try:
        t0 = 0.0 if start_time is None else float(start_time)
        t1 = float(seq.duration_s) if end_time is None else float(end_time)
        max_swings = int(max_swings)
        if swing_count is not None:
            swing_count = int(swing_count)
    except (TypeError, ValueError):
        return tool_error(
            "INVALID_ARGUMENT",
            "start_time/end_time/max_swings/swing_count invalid",
        )
    if t1 < t0:
        t0, t1 = t1, t0
    t0 = max(0.0, t0)
    t1 = min(float(seq.duration_s), t1)
    if max_swings < 1:
        max_swings = 1
    max_swings = min(max_swings, 12)
    if swing_count is not None and swing_count < 1:
        return tool_error("INVALID_ARGUMENT", "swing_count must be >= 1")

    try:
        hand, lead, trail = _resolve_handedness(handedness)
    except ValueError as exc:
        return tool_error("INVALID_ARGUMENT", str(exc))

    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop - sl.start < 8:
        return tool_error(
            "INSUFFICIENT_WINDOW",
            "Need a wider temporal window to segment golf swings",
            start_time=t0,
            end_time=t1,
        )

    dt = float(seq.metadata.get("dt") or (1.0 / max(seq.fps, 1e-6)))
    ts = seq.timestamps[sl]

    lead_pos = seq.joint_positions.get(f"{lead}_wrist")
    trail_pos = seq.joint_positions.get(f"{trail}_wrist")
    if lead_pos is None and trail_pos is None:
        return tool_error(
            "MISSING_KINEMATICS",
            "No wrist positions available for swing segmentation",
        )

    speeds = []
    if lead_pos is not None:
        speeds.append(_speed_mag(lead_pos[sl], dt))
    if trail_pos is not None:
        speeds.append(_speed_mag(trail_pos[sl], dt))
    raw_speed = np.mean(np.stack(speeds, axis=0), axis=0)
    smooth_win = max(5, int(0.12 / dt))
    if smooth_win % 2 == 0:
        smooth_win += 1
    speed = _moving_average(raw_speed, smooth_win)

    quiet = float(np.percentile(speed, 30))
    active_thr = max(float(np.percentile(speed, 55)), quiet * 1.6, 0.25)
    peak_floor = max(float(np.percentile(speed, 78)), active_thr * 1.25, 0.4)
    order = max(3, int(0.12 / dt))

    duration_s = float(ts[-1] - ts[0])
    # Conservative duration prior: ~2.5s+ per full swing including address.
    duration_cap = max(1, int(duration_s / 2.5) + (1 if duration_s >= 4.0 else 0))
    max_swings = min(max_swings, duration_cap)
    if swing_count is not None:
        max_swings = min(max_swings, int(swing_count))

    segments = _activity_segments(
        speed,
        ts,
        active_thr,
        dt,
        min_duration_s=0.9,
        merge_gap_s=0.55,
    )
    peaks_raw = _peak_candidates(speed, peak_floor, order)
    peaks = _filter_peaks_for_swing_count(
        peaks_raw,
        swing_count=swing_count,
        max_swings=max_swings,
        dt=dt,
    )
    # Match only among filtered peaks (one-to-one with activity segments).
    matched = _match_segments_to_peaks(segments, peaks, ts, max_swings=len(peaks) or 1)

    # If LLM asked for N but matching under-produced, seed from top peaks alone.
    if swing_count is not None and len(matched) < int(swing_count) and peaks:
        matched = []
        for peak in peaks[: int(swing_count)]:
            pi = int(peak["index"])
            matched.append(
                (
                    {
                        "i0": 0,
                        "i1": len(speed) - 1,
                        "core0": max(0, pi - int(0.8 / dt)),
                        "core1": min(len(speed) - 1, pi + int(0.5 / dt)),
                        "mass": float(peak["speed"]),
                    },
                    peak,
                )
            )
        matched.sort(key=lambda sp: int(sp[1]["index"]))

    if not matched:
        peak_i = int(np.argmax(speed))
        matched = [
            (
                {
                    "i0": 0,
                    "i1": len(speed) - 1,
                    "core0": 0,
                    "core1": len(speed) - 1,
                    "mass": float(np.sum(speed)),
                },
                {"index": peak_i, "score": 1.0, "speed": float(speed[peak_i])},
            )
        ]

    windows = _exclusive_windows(matched, len(ts))
    swings: list[dict[str, Any]] = []
    for si, (i0, impact_i, i1) in enumerate(windows):
        phases = _build_phases(ts, speed, impact_i, i0, i1, quiet)
        seen_ids: set[str] = set()
        uniq_phases: list[dict[str, Any]] = []
        for p in phases:
            if p["id"] in seen_ids:
                continue
            seen_ids.add(p["id"])
            uniq_phases.append(p)
        peak_speed = float(speed[impact_i])
        swings.append(
            {
                "swing_index": si,
                "start_time": round(float(ts[i0]), 4),
                "end_time": round(float(ts[i1]), 4),
                "impact_time": round(float(ts[impact_i]), 4),
                "confidence": round(
                    min(0.92, 0.5 + peak_speed / max(peak_floor * 2, 1e-6) * 0.35),
                    3,
                ),
                "phases": uniq_phases,
                "supporting_measurements": {
                    "peak_wrist_speed_m_s": round(peak_speed, 4),
                    "peak_wrist_speed_time": round(float(ts[impact_i]), 4),
                    "quiet_speed_m_s": round(quiet, 4),
                    "lead_wrist": f"{lead}_wrist",
                    "trail_wrist": f"{trail}_wrist",
                    "match": "hungarian_segment_peak",
                },
            }
        )

    return {
        "error": False,
        "ui_hint": "golf_phase_cards",
        "sport": "golf",
        "tool": "segment_golf_swings",
        "handedness": hand,
        "lead_side": lead,
        "trail_side": trail,
        "start_time": round(float(ts[0]), 4),
        "end_time": round(float(ts[-1]), 4),
        "swing_count": len(swings),
        "swings": swings,
        "phase_order": list(PHASE_ORDER),
        "phase_min_duration_s": dict(PHASE_MIN_DURATION_S),
        "matching": {
            "algorithm": "hungarian",
            "activity_segments": len(segments),
            "peak_candidates": len(peaks_raw),
            "peaks_kept": len(peaks),
            "matched": len(matched),
            "swing_count_arg": swing_count,
            "count_mode": "llm" if swing_count is not None else "auto_conservative",
        },
        "note": (
            "Count swings first (prefer swing_count from reasoning). Peaks are "
            "filtered so weak blips cannot become extra swings; matched swings get "
            "exclusive windows and phase mins scaled to fit address→follow-through. "
            "Not club/ball tracking."
        ),
    }
