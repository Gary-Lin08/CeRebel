"""Upload one clip, wait for the analysis, print the main fix and ask the coach a follow-up.

    python analyze_swing.py swing.mp4                     # against the mock API (no account needed)
    CEREBEL_BASE_URL=http://127.0.0.1:8092 CEREBEL_TOKEN=… python analyze_swing.py swing.mp4
"""

from __future__ import annotations

import os
import sys

from cerebel import AnalysisFailed, CerebelClient, ConsentRequired, UploadRejected, fix_summary
from cerebel.testing import MockCerebelServer


def run(client: CerebelClient, video: str) -> int:
    try:
        result = client.analyze(
            video, poll_interval=1.0, on_progress=lambda p: print(f"  {p.get('stage')} {p.get('percent') or ''}")
        )
    except ConsentRequired as exc:
        print("Agree to video processing first:\n\n" + str(exc.consent.get("text")))
        return 2
    except (UploadRejected, AnalysisFailed) as exc:
        print(f"Not analysed: {exc.message}")
        return 1
    fix = result["main_fix"]
    if fix is None:
        print("Nothing big to fix in this swing.")
        return 0
    parts = fix_summary(fix)
    print(f"\n{parts['headline']}  ({parts['metric']})\nFeel: {parts['cue']}\nDrill: {parts['drill']}\n")
    for event in client.ask_stream("Why does that matter for my ball flight?", result["session_id"]):
        if event.get("type") == "done":
            print("Coach:", event.get("answer"))
    return 0


def main() -> int:
    base = os.environ.get("CEREBEL_BASE_URL")
    if base:
        if len(sys.argv) < 2:
            print("usage: analyze_swing.py VIDEO")
            return 2
        upload = os.environ.get("CEREBEL_UPLOAD_BASE_URL")
        return run(CerebelClient(base, os.environ.get("CEREBEL_TOKEN"), upload_base_url=upload), sys.argv[1])
    # No server given: the mock API ignores the file's content, so any existing file will do.
    video = sys.argv[1] if len(sys.argv) > 1 else __file__
    with MockCerebelServer(stage_seconds=1.0) as mock:
        client = CerebelClient(mock.url)
        client.register("demo@example.com", "password123", accept_terms=True)
        client.accept_video_consent()
        return run(client, video)


if __name__ == "__main__":
    sys.exit(main())
