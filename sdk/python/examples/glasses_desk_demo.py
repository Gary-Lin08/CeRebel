"""The whole glasses loop on one computer: mock API + mock glasses + bridge.

A key press on the (mock) glasses means "recording finished"; the bridge then analyses the newest
file in the recordings folder and the fix appears as HUD cards. Swap MockGlasses for the real
glasses' address and MockCerebelServer for the real API to run it for real.

    python glasses_desk_demo.py
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from cerebel import CerebelClient
from cerebel.glasses import GlassesBridge, GlassesLink, LinkHudSink, MockGlasses
from cerebel.testing import MockCerebelServer


def main() -> None:
    recordings = Path(tempfile.mkdtemp(prefix="cerebel-rec-"))
    with MockCerebelServer(stage_seconds=0.5) as api, MockGlasses(echo=False) as glasses:
        client = CerebelClient(api.url)
        client.register("coach@example.com", "password123", accept_terms=True)
        client.accept_video_consent()

        pressed = threading.Event()

        def on_key(action: str) -> None:
            # Runs on the link's reader thread: only note it; the main thread does the work.
            if action == "rec_stop":
                pressed.set()

        with GlassesLink(glasses.host, glasses.port, on_key=on_key) as link:
            bridge = GlassesBridge(client, LinkHudSink(link), lang="zh", poll_interval=0.2, progress_every_s=0)

            # Pretend the wearer filmed a swing: a file lands in the folder, then the key is released.
            (recordings / "take1.mp4").write_bytes(b"\x00" * 4096)
            glasses.press("rec_stop")
            pressed.wait(5)

            newest = max(recordings.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
            result = bridge.process_clip(newest)
            time.sleep(0.2)

        print("Cards the glasses showed:")
        for card in glasses.huds():
            print(f"  [{card['mode']}] {card['status']}  {card['reply']}")
        print("Main fix:", result["main_fix"]["headline"])


if __name__ == "__main__":
    main()
