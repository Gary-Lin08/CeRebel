"""Glasses link protocol, HUD cards, recordings and the end-to-end bridge (mock API + mock glasses)."""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cerebel import CerebelClient
from cerebel.glasses import (
    ConsoleHudSink,
    FolderWatcher,
    FrameDecoder,
    GlassesBridge,
    GlassesLink,
    LinkHudSink,
    MockGlasses,
    encode_frame,
    fix_card,
    hud,
    prepare_clip,
    progress_card,
)
from cerebel.glasses.protocol import MAX_FRAME_BYTES, ProtocolError
from cerebel.testing import MockCerebelServer


class ProtocolTest(unittest.TestCase):
    def test_frame_round_trip_in_any_chunking(self) -> None:
        frames = encode_frame({"type": "hello", "fw": "1.0"}) + encode_frame(hud(status="挥杆分析", reply="保持后膝微屈"))
        decoder = FrameDecoder()
        out = []
        for i in range(len(frames)):
            out.extend(decoder.feed(frames[i : i + 1]))
        self.assertEqual([m["type"] for m in out], ["hello", "hud"])
        self.assertEqual(out[1]["reply"], "保持后膝微屈")

    def test_length_prefix_is_big_endian(self) -> None:
        frame = encode_frame({"type": "ping"})
        (length,) = struct.unpack(">I", frame[:4])
        self.assertEqual(length, len(frame) - 4)

    def test_oversized_frame_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            FrameDecoder().feed(struct.pack(">I", MAX_FRAME_BYTES + 1))

    def test_hud_fields_are_clipped(self) -> None:
        card = hud(status="x" * 100, reply="y" * 500, you="z" * 200)
        self.assertEqual(len(card["status"]), 40)
        self.assertEqual(len(card["reply"]), 160)
        self.assertTrue(card["reply"].endswith("…"))
        with self.assertRaises(ProtocolError):
            hud(mode="party")


class CardTest(unittest.TestCase):
    def test_fix_card(self) -> None:
        card = fix_card({"headline": "Your back knee locks too early", "cue": "Keep a soft back knee",
                         "metric": {"value": "66° → 0°"}}, lang="zh")
        self.assertEqual(card["mode"], "assist")
        self.assertEqual(card["status"], "要改 66° → 0°")
        self.assertIn("soft back knee", card["reply"])

    def test_progress_card_with_queue(self) -> None:
        card = progress_card({"stage": "queued", "percent": 22, "jobs_ahead": 2, "eta_minutes": 4}, lang="en")
        self.assertEqual(card["status"], "In line 22%")
        self.assertEqual(card["reply"], "2 ahead · ~4 min")


class LinkTest(unittest.TestCase):
    def test_hello_hud_and_keys(self) -> None:
        keys: list[str] = []
        hello = threading.Event()
        with MockGlasses(fw="test-fw") as glasses:
            link = GlassesLink(glasses.host, glasses.port, ping_interval=0, on_key=keys.append,
                               on_hello=lambda m: hello.set())
            with link:
                self.assertTrue(hello.wait(2))
                self.assertEqual(link.hello["fw"], "test-fw")
                link.send_hud(mode="info", status="ready", reply="hi")
                self.assertEqual(glasses.wait_for(1)[0]["status"], "ready")
                glasses.press("rec_stop")
                deadline = time.time() + 2
                while not keys and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(keys, ["rec_stop"])


class ClipTest(unittest.TestCase):
    def test_container_passes_through(self) -> None:
        self.assertEqual(prepare_clip("a/b/swing.mp4"), Path("a/b/swing.mp4"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_raw_h264_is_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "factory_1.h264"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x480:rate=30",
                 "-t", "2", "-c:v", "libx264", "-f", "h264", str(raw)],
                check=True,
            )
            mp4 = prepare_clip(raw, fps=30)
            self.assertEqual(mp4.suffix, ".mp4")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate",
                 "-of", "csv=p=0", str(mp4)], capture_output=True, text=True, check=True,
            )
            num, den = probe.stdout.strip().split("/")
            self.assertAlmostEqual(int(num) / int(den), 30.0, places=1)

    def test_watcher_reports_settled_new_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "old.mp4").write_bytes(b"1" * 10)
            watcher = FolderWatcher(folder, settle_s=0.05)
            (folder / "new.mp4").write_bytes(b"1" * 10)
            self.assertEqual(watcher.poll(), [])  # first sighting
            time.sleep(0.08)
            self.assertEqual([p.name for p in watcher.poll()], ["new.mp4"])
            self.assertEqual(watcher.poll(), [])


class BridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = MockCerebelServer(stage_seconds=0.05).start()
        self.client = CerebelClient(self.api.url)
        self.client.register("coach@example.com", "password123", accept_terms=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.api.stop()
        self.tmp.cleanup()

    def test_clip_to_fix_on_glasses(self) -> None:
        self.client.accept_video_consent()
        clip = self.folder / "swing.mp4"
        clip.write_bytes(b"\x00" * 4096)
        with MockGlasses() as glasses, GlassesLink(glasses.host, glasses.port, ping_interval=0) as link:
            bridge = GlassesBridge(self.client, LinkHudSink(link), lang="zh", progress_every_s=0, poll_interval=0.01)
            result = bridge.process_clip(clip)
            self.assertEqual(result["main_fix"]["id"], "trail-knee")
            cards = glasses.wait_for(3)
            deadline = time.time() + 2
            while glasses.huds()[-1].get("mode") != "assist" and time.time() < deadline:
                time.sleep(0.01)
            cards = glasses.huds()
            self.assertEqual(cards[0]["status"], "收到录像")
            self.assertTrue(any(c["status"].startswith("排队中") for c in cards))
            self.assertEqual(cards[-1]["mode"], "assist")
            self.assertTrue(cards[-1]["status"].startswith("要改"))

    def test_consent_missing_shows_instruction(self) -> None:
        clip = self.folder / "swing.mp4"
        clip.write_bytes(b"\x00" * 4096)
        shown: list[dict] = []

        class Sink:
            def show(self, card: dict) -> None:
                shown.append(card)

        from cerebel import ConsentRequired

        with self.assertRaises(ConsentRequired):
            GlassesBridge(self.client, Sink()).process_clip(clip)
        self.assertEqual(shown[-1]["mode"], "end")
        self.assertIn("同意", shown[-1]["reply"])

    def test_watch_folder_processes_new_recording(self) -> None:
        self.client.accept_video_consent()
        stop = threading.Event()
        results: list[dict] = []

        def done(result: dict) -> None:
            results.append(result)
            stop.set()

        bridge = GlassesBridge(self.client, ConsoleHudSink(stream=io.StringIO()), poll_interval=0.01, on_result=done)
        worker = threading.Thread(
            target=bridge.watch, args=(self.folder,), kwargs={"interval": 0.02, "settle_s": 0.05, "stop": stop}, daemon=True
        )
        worker.start()
        time.sleep(0.05)
        (self.folder / "take1.mp4").write_bytes(b"\x00" * 4096)
        worker.join(timeout=10)
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
