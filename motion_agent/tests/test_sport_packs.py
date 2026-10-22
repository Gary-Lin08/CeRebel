"""Sport Pack registry + product session wiring."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from motion_agent.sports.registry import catalog, detect_sport, get_pack, resolve_pack_session
from motion_agent.sports.schema import DetectResult


class SportPackRegistryTests(unittest.TestCase):
    def test_catalog_has_live_golf_and_coming_soon(self):
        rows = catalog()
        by_id = {r["id"]: r for r in rows}
        self.assertIn("golf", by_id)
        self.assertEqual(by_id["golf"]["status"], "live")
        self.assertFalse(by_id["golf"]["locked"])
        self.assertIn("baseball", by_id)
        self.assertTrue(by_id["baseball"]["locked"])
        self.assertEqual(by_id["baseball"]["status"], "coming_soon")

    def test_get_pack_aliases(self):
        self.assertEqual(get_pack("golf").id, "golf")
        self.assertEqual(get_pack("driver").id, "golf")
        self.assertEqual(get_pack("general").id, "general")
        self.assertEqual(get_pack("baseball").id, "baseball")
        self.assertEqual(get_pack("baseball").status, "coming_soon")
        self.assertEqual(get_pack("tennis").id, "tennis")
        self.assertEqual(get_pack("tennis").status, "coming_soon")
        self.assertEqual(get_pack("snowboarding").id, "snowboarding")
        self.assertEqual(get_pack("sb").id, "snowboarding")

    def test_catalog_includes_snowboarding_and_kb_modes(self):
        from motion_agent.knowledge.sport_sources import SPORT_KB_SOURCES

        rows = catalog()
        by_id = {r["id"]: r for r in rows}
        self.assertIn("snowboarding", by_id)
        self.assertTrue(by_id["snowboarding"]["locked"])
        self.assertIn("yoga", by_id)
        for mode in SPORT_KB_SOURCES:
            if mode == "general":
                continue
            self.assertIn(mode, by_id, msg=f"missing catalog row for {mode}")

    def test_detect_prefers_golf_when_matched(self):
        session_dir = Path("/tmp/fake_session")
        golf = get_pack("golf")
        with mock.patch.object(
            type(golf),
            "detect",
            return_value=DetectResult(matched=True, confidence=0.9, detail={}),
        ):
            pack, result = detect_sport(session_dir, video_id="s1")
        self.assertEqual(pack.id, "golf")
        self.assertTrue(result.matched)

    def test_detect_falls_back_to_human(self):
        session_dir = Path("/tmp/fake_session")
        golf = get_pack("golf")
        with mock.patch.object(
            type(golf),
            "detect",
            return_value=DetectResult(matched=False, confidence=0.1, message="no swing"),
        ):
            pack, result = detect_sport(session_dir, video_id="s1", preferred="golf")
        self.assertEqual(pack.id, "general")
        self.assertIn("preferred", result.detail)

    def test_resolve_pack_session_modes_honest(self):
        session_dir = Path("/tmp/fake_session")
        golf = get_pack("golf")
        with mock.patch.object(
            type(golf),
            "detect",
            return_value=DetectResult(matched=True, confidence=0.88),
        ), mock.patch.object(type(golf), "build_briefs", return_value=[{"id": "x"}]):
            ps = resolve_pack_session(session_dir, video_id="s1")
        fields = ps.as_session_fields()
        self.assertEqual(fields["sport"], "golf")
        self.assertEqual(fields["available_modes"], ["golf"])
        self.assertNotIn("baseball", fields["available_modes"])
        self.assertEqual(fields["product_title"], "Your swing")
        self.assertEqual(len(fields["coach_briefs"]), 1)


class ProductSessionApiTests(unittest.TestCase):
    def test_sports_catalog_payload(self):
        from motion_agent.app.product_session import sports_catalog_payload

        payload = sports_catalog_payload()
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["live_count"], 1)
        ids = {r["id"] for r in payload["sports"]}
        self.assertIn("golf", ids)
        self.assertIn("tennis", ids)
        baseball = next(r for r in payload["sports"] if r["id"] == "baseball")
        self.assertTrue(baseball["locked"])

    def test_clamp_prompt_mode_consumer(self):
        import tempfile

        from motion_agent.app.product_session import resolve_prompt_mode_for_session
        from motion_agent.sports.schema import PackSession

        fake = PackSession(
            sport="golf",
            label="Golf",
            confidence=0.9,
            product_title="Your swing",
            product_subtitle="x",
            example_questions=[],
            default_prompt_mode="golf",
            available_modes=["golf"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sessions" / "abc").mkdir(parents=True)
            with mock.patch(
                "motion_agent.sports.registry.resolve_pack_session", return_value=fake
            ):
                mode, meta = resolve_prompt_mode_for_session(
                    video_id="abc",
                    data_dir=root,
                    requested="baseball",
                    allow_dev_modes=False,
                )
        self.assertEqual(mode, "golf")
        self.assertTrue(meta["clamped"])

    def test_clamp_blocks_golf_as_baseball_even_in_dev(self):
        import tempfile

        from motion_agent.app.product_session import resolve_prompt_mode_for_session
        from motion_agent.sports.schema import PackSession

        fake = PackSession(
            sport="golf",
            label="Golf",
            confidence=0.9,
            product_title="Your swing",
            product_subtitle="x",
            example_questions=[],
            default_prompt_mode="golf",
            available_modes=["golf"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sessions" / "abc").mkdir(parents=True)
            with mock.patch(
                "motion_agent.sports.registry.resolve_pack_session", return_value=fake
            ):
                mode, meta = resolve_prompt_mode_for_session(
                    video_id="abc",
                    data_dir=root,
                    requested="baseball",
                    allow_dev_modes=True,
                )
        self.assertEqual(mode, "golf")
        self.assertEqual(meta.get("reason"), "SPORT_MISMATCH")


class BaseballPackTests(unittest.TestCase):
    def test_baseball_blocked_by_golf(self):
        from motion_agent.sports.packs.baseball import BaseballPack
        from motion_agent.sports.packs.golf import GolfPack

        session_dir = Path("/tmp/fake_session")
        with mock.patch.object(
            GolfPack,
            "detect",
            return_value=DetectResult(matched=True, confidence=0.91),
        ):
            result = BaseballPack().detect(session_dir, video_id="s1")
        self.assertFalse(result.matched)
        self.assertEqual(result.detail.get("blocked_by"), "golf")

    def test_baseball_stays_disabled_even_with_high_pitch_score(self):
        from motion_agent.sports.packs.baseball import BaseballPack, DETECTOR_ENABLED
        from motion_agent.sports.packs.golf import GolfPack

        self.assertFalse(DETECTOR_ENABLED)
        session_dir = Path("/tmp/fake_session")
        with mock.patch.object(
            GolfPack,
            "detect",
            return_value=DetectResult(matched=False, confidence=0.1),
        ), mock.patch(
            "motion_agent.sports.packs.pitch_score.pitch_likeness",
            return_value={"score": 0.95, "ok": True, "detail": {}},
        ):
            result = BaseballPack().detect(session_dir, video_id="s1")
        self.assertFalse(result.matched)
        self.assertGreaterEqual(result.confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
