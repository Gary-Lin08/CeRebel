#!/usr/bin/env python3
"""Measure how much the progress metrics move between two swings that should be the same.

Read-only. For every account, pairs of ready captures uploaded within --window-min of each
other (same practice session: same player, same camera spot, nothing coached in between)
are compared, and the spread of |change| per metric is printed. The 95th percentile is the
smallest change the progress view should call real (swing_compare.METRICS noise_floor).

    CEREBEL_DATA_DIR=/path/to/data python3 -m motion_agent.scripts.compare_noise_floor
    python3 -m motion_agent.scripts.compare_noise_floor --json > floors.json

Prints counts and percentiles only — no account ids, session ids or file names.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

from motion_agent.app.compare import _load_pose3d, ready_captures
from motion_agent.app.config import load_settings
from motion_agent.app.library import all_captures
from motion_agent.motion.adapter import adapt_session
from motion_agent.motion.swing_compare import METRICS, swing_snapshot


def _when(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("created_at")))
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window-min", type=float, default=60.0, help="max minutes between the two uploads")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    settings = load_settings()
    users = {str(r["user_id"]) for r in all_captures(settings.product_dir) if r.get("user_id")}
    deltas: dict[str, list[float]] = defaultdict(list)
    pairs = 0
    snapshots: dict[str, dict] = {}

    def snap(sid: str) -> dict:
        if sid not in snapshots:
            seq = adapt_session(settings.data_dir / "sessions" / sid)
            snapshots[sid] = swing_snapshot(seq, 0, pose3d=_load_pose3d(settings, sid))
        return snapshots[sid]

    for user_id in sorted(users):
        ready = ready_captures(settings, user_id)
        for a, b in zip(ready, ready[1:]):
            ta, tb = _when(a), _when(b)
            if ta is None or tb is None or (tb - ta).total_seconds() > args.window_min * 60:
                continue
            try:
                sa, sb = snap(str(a["session_id"])), snap(str(b["session_id"]))
            except Exception:  # noqa: BLE001 - skip unreadable captures, keep counting the rest
                continue
            if sa.get("alignment") == "none" or sb.get("alignment") == "none":
                continue
            pairs += 1
            for spec in METRICS:
                va, vb = sa["values"].get(spec.id), sb["values"].get(spec.id)
                if va is not None and vb is not None:
                    deltas[spec.id].append(abs(vb - va))

    report = {"pairs": pairs, "window_min": args.window_min, "metrics": {}}
    for spec in METRICS:
        values = np.asarray(deltas.get(spec.id) or [], dtype=float)
        row = {"n": int(values.size), "current_floor": spec.noise_floor, "unit": spec.unit}
        if values.size:
            row.update({f"p{q}": round(float(np.percentile(values, q)), 2) for q in (50, 90, 95)})
        report["metrics"][spec.id] = row

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 0
    print(f"{pairs} back-to-back pairs (uploads within {args.window_min:.0f} min)")
    print(f"{'metric':28} {'n':>4} {'p50':>7} {'p90':>7} {'p95':>7} {'floor now':>10}")
    for mid, row in report["metrics"].items():
        cells = [f"{row.get(k, float('nan')):>7.2f}" for k in ("p50", "p90", "p95")]
        print(f"{mid:28} {row['n']:>4} {' '.join(cells)} {row['current_floor']:>10.2f}")
    if pairs < 20:
        print("Fewer than 20 pairs: keep the provisional floors until more data comes in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
