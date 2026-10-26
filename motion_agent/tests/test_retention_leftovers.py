"""Retention of what sits outside the session folders: GEM-X logs, the legacy usage log, question counters."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

from motion_agent.app import billing, retention
from motion_agent.tests.test_privacy_retention import _age, _TempData


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class GemxLogTests(_TempData):
    def test_logs_beside_the_output_folders_go_after_the_raw_video_limit(self):
        for sid, days in (("OLD1", 40), ("NEW1", 2), ("DEMO1", 900)):
            log = self.gemx / f"{sid}_gemx.log"
            log.write_text("[Preprocess] Done\n")
            _age(log, days)
        (self.gemx / "OLD1").mkdir()
        (self.gemx / "OLD1" / "hpe_results.pt").write_bytes(b"x")
        (self.data / "sessions" / "OLD1").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": "DEMO1"}):
            dry = retention.run(self.settings, dry_run=True)
            self.assertEqual(dry["gemx_logs_deleted"], ["OLD1"])
            self.assertTrue((self.gemx / "OLD1_gemx.log").is_file())
            report = retention.run(self.settings)
        self.assertEqual(report["gemx_logs_deleted"], ["OLD1"])
        self.assertFalse((self.gemx / "OLD1_gemx.log").exists())
        self.assertTrue((self.gemx / "NEW1_gemx.log").is_file())
        self.assertTrue((self.gemx / "DEMO1_gemx.log").is_file())
        self.assertTrue((self.gemx / "OLD1" / "hpe_results.pt").is_file())
        self.assertIn("1 GEM-X logs", retention.summary_line(report))


class LegacyUsageLogTests(_TempData):
    def _write(self, *rows: str) -> None:
        (self.data / "product_events.jsonl").write_text("".join(r + "\n" for r in rows), encoding="utf-8")

    def _row(self, days: float, visitor: str) -> str:
        return json.dumps({"received_at": _ago(days), "event": "page_view", "visitor_id": visitor, "properties": {}})

    def test_rows_follow_the_inactivity_limit_and_the_file_goes_when_empty(self):
        path = self.data / "product_events.jsonl"
        self._write(self._row(800, "v-old1"), self._row(731, "v-old2"), self._row(1, "v-new"), "not json")
        dry = retention.run(self.settings, dry_run=True)
        self.assertEqual(dry["legacy_events_removed"], 2)
        self.assertIn("v-old1", path.read_text())

        report = retention.run(self.settings)
        self.assertEqual(report["legacy_events_removed"], 2)
        kept = path.read_text().splitlines()
        self.assertEqual(len(kept), 2)
        self.assertIn("v-new", kept[0])
        self.assertEqual(kept[1], "not json")
        self.assertIn("(+2 legacy)", retention.summary_line(report))

        self._write(self._row(900, "v-old3"))
        self.assertEqual(retention.run(self.settings)["legacy_events_removed"], 1)
        self.assertFalse(path.exists())
        self.assertEqual(retention.run(self.settings)["legacy_events_removed"], 0)


class QuestionCountTests(_TempData):
    def _ask(self, key: str, days_ago: int = 0) -> tuple[bool, int]:
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        with mock.patch.object(billing, "_now", return_value=when):
            return billing.take_question(self.settings.product_dir, key, 3)

    def test_an_earlier_days_counts_are_deleted(self):
        path = self.settings.product_dir / "question_counts.json"
        self._ask("ip:203.0.113.7", days_ago=2)
        self._ask("anon-total", days_ago=2)
        dry = retention.run(self.settings, dry_run=True)
        self.assertEqual(dry["question_counts_cleared"], 2)
        self.assertTrue(path.is_file())

        report = retention.run(self.settings)
        self.assertEqual(report["question_counts_cleared"], 2)
        self.assertFalse(path.exists())
        self.assertIn("2 stale question counters", retention.summary_line(report))
        self.assertEqual(self._ask("ip:203.0.113.7"), (True, 2))

    def test_todays_counts_stay(self):
        path = self.settings.product_dir / "question_counts.json"
        self._ask("u:abc")
        self._ask("u:abc")
        self.assertEqual(retention.run(self.settings)["question_counts_cleared"], 0)
        self.assertEqual(json.loads(path.read_text())["counts"], {"u:abc": 2})
        self.assertEqual(self._ask("u:abc"), (True, 0))

    def test_unreadable_counter_file_is_removed(self):
        path = self.settings.product_dir / "question_counts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        self.assertEqual(retention.run(self.settings)["question_counts_cleared"], 0)
        self.assertFalse(path.exists())
