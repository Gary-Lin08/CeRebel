"""The supervisor-run daily loop around the retention job (old GPU box: no cron, no systemd)."""

from __future__ import annotations

import configparser
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from motion_agent.app import retention_schedule as rs

AT = (7, 41)
REPO = Path(__file__).resolve().parents[2]


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class _Clock:
    """Fake time: sleeping moves it forward, a retention run takes a few seconds."""

    def __init__(self, start: datetime):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class ScheduleMathTests(unittest.TestCase):
    def test_parse_at(self):
        self.assertEqual(rs.parse_at("07:41"), (7, 41))
        self.assertEqual(rs.parse_at(" 23:05 "), (23, 5))
        for bad in ("", None, "7", "24:00", "07:60", "noon"):
            with self.assertLogs(rs.LOG, "WARNING") if bad else contextlib.nullcontext():
                self.assertEqual(rs.parse_at(bad), (7, 41), bad)

    def test_slots_around_the_daily_time(self):
        before = _utc("2026-10-26T07:40:59")
        after = _utc("2026-10-26T07:41:00")
        self.assertEqual(rs.last_scheduled(before, AT), _utc("2026-10-25T07:41:00"))
        self.assertEqual(rs.next_scheduled(before, AT), _utc("2026-10-26T07:41:00"))
        self.assertEqual(rs.last_scheduled(after, AT), after)
        self.assertEqual(rs.next_scheduled(after, AT), _utc("2026-10-27T07:41:00"))

    def test_due_once_a_slot_has_passed_since_the_last_run(self):
        now = _utc("2026-10-26T18:00:00")
        self.assertFalse(rs.due(now, _utc("2026-10-26T07:45:00"), AT))
        self.assertTrue(rs.due(now, _utc("2026-10-26T07:40:00"), AT))
        self.assertTrue(rs.due(now, _utc("2026-10-20T07:45:00"), AT))


class LoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.product = Path(self._tmp.name) / "product"
        self.product.mkdir()
        self.runs: list[datetime] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _log(self, when: datetime, *, dry_run: bool = False) -> None:
        with (self.product / "retention.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"dry_run": dry_run, "now": when.isoformat()}) + "\n")

    def _runner(self, clock: _Clock, code: int = 0):
        def run() -> int:
            self.runs.append(clock.now)
            if code == 0:  # the real job logs every completed run
                self._log(clock.now)
            clock.now += timedelta(seconds=5)
            return code

        return run

    def _loop(self, clock: _Clock, runner, iterations: int) -> None:
        rs.loop(self.product, AT, clock=clock, sleep=clock.sleep, runner=runner, iterations=iterations)

    def test_last_run_skips_dry_runs_and_junk(self):
        self.assertIsNone(rs.last_run(self.product))
        self._log(_utc("2026-10-24T07:41:10"))
        with (self.product / "retention.jsonl").open("a") as handle:
            handle.write("not json\n{\"now\": \"bad\"}\n")
        self._log(_utc("2026-10-25T09:00:00"), dry_run=True)
        self.assertEqual(rs.last_run(self.product), _utc("2026-10-24T07:41:10"))
        with (self.product / "retention.jsonl").open("a") as handle:
            handle.write(json.dumps({"dry_run": False, "now": "2026-10-25T07:41:00"}) + "\n")  # naive = UTC
        self.assertEqual(rs.last_run(self.product), _utc("2026-10-25T07:41:00"))

    def test_first_install_waits_for_the_daily_time_then_runs_once_a_day(self):
        clock = _Clock(_utc("2026-10-26T18:12:00"))
        with self.assertLogs(rs.LOG, "INFO") as logs:
            self._loop(clock, self._runner(clock), iterations=45)
        self.assertIn("first run at 2026-10-27T07:41:00", "\n".join(logs.output))
        self.assertTrue(all(1.0 <= s <= rs.MAX_SLEEP_S for s in clock.sleeps))
        self.assertEqual([r.isoformat() for r in self.runs],
                         ["2026-10-27T07:41:00+00:00", "2026-10-28T07:41:00+00:00"])

    def test_a_run_missed_while_the_box_was_off_happens_at_start(self):
        self._log(_utc("2026-10-24T07:41:30"))
        clock = _Clock(_utc("2026-10-26T18:12:00"))
        self._loop(clock, self._runner(clock), iterations=3)
        self.assertEqual(self.runs, [_utc("2026-10-26T18:12:00")])

    def test_not_run_again_after_a_restart_on_the_same_day(self):
        self._log(_utc("2026-10-26T07:41:30"))
        clock = _Clock(_utc("2026-10-26T18:12:00"))
        self._loop(clock, self._runner(clock), iterations=5)
        self.assertEqual(self.runs, [])

    def test_failed_run_is_retried_an_hour_later(self):
        self._log(_utc("2026-10-24T07:41:30"))
        clock = _Clock(_utc("2026-10-26T18:12:00"))
        with self.assertLogs(rs.LOG, "ERROR"):
            self._loop(clock, self._runner(clock, code=2), iterations=4)
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(self.runs[1] - self.runs[0], rs.RETRY_AFTER + timedelta(seconds=5))

    def test_success_without_a_log_line_does_not_loop(self):
        self._log(_utc("2026-10-24T07:41:30"))
        clock = _Clock(_utc("2026-10-26T18:12:00"))

        def run() -> int:
            self.runs.append(clock.now)
            return 0

        self._loop(clock, run, iterations=6)
        self.assertEqual(len(self.runs), 1)

    def test_runner_starts_the_retention_module_from_the_repo(self):
        with mock.patch.object(rs.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(rs.run_retention(), 0)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-2:], ["-m", "motion_agent.app.retention"])
        self.assertEqual(run.call_args.kwargs["cwd"], REPO)
        with mock.patch.object(rs.subprocess, "run", side_effect=rs.subprocess.TimeoutExpired(cmd, 1)), \
                self.assertLogs(rs.LOG, "ERROR"):
            self.assertEqual(rs.run_retention(), 1)

    def test_cli_next(self):
        self._log(_utc("2026-10-24T07:41:30"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CEREBEL_DATA_DIR": str(self.product.parent)}), \
                mock.patch("sys.stdout", out):
            self.assertEqual(rs.main(["--next", "--at", "07:41"]), 0)
        text = out.getvalue()
        self.assertIn("last run: 2026-10-24T07:41:30+00:00", text)
        self.assertIn("next run:", text)


class SupervisorConfTests(unittest.TestCase):
    def test_program_runs_the_loop_with_the_product_environment(self):
        conf = configparser.ConfigParser(inline_comment_prefixes=None, comment_prefixes=(";", "#"))
        conf.read(REPO / "deploy" / "supervisor" / "cerebel-retention.conf")
        prog = conf["program:cerebel-retention"]
        self.assertIn("source /root/CerebelServer/.env.product", prog["command"])
        self.assertIn("-m motion_agent.app.retention_schedule", prog["command"])
        self.assertEqual(prog["directory"], "/root/CerebelServer")
        self.assertEqual(prog["autorestart"], "true")
        self.assertEqual(prog["stopasgroup"], "true")
        self.assertEqual(conf.sections(), ["program:cerebel-retention"])


if __name__ == "__main__":
    unittest.main()
