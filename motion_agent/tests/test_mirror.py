"""The Supabase mirror: what leaves the server, and that deletions follow the files."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from motion_agent.app import mirror, site_traffic
from motion_agent.app.accounts import delete_account, register


class FakeSupabase:
    """Just enough of PostgREST for the mirror: select keys, upsert, delete by key."""

    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method, path, body=None, prefer=None):
        self.calls.append((method, path, body))
        table, _, query = path.partition("?")
        params = urllib.parse.parse_qs(query)
        rows = self.tables.setdefault(table, {})
        if method == "POST":
            conflict = params["on_conflict"][0].split(",")
            for row in body:
                rows["|".join(str(row[c]) for c in conflict)] = row
            return None
        if method == "GET":
            cols = params["select"][0].split(",")
            data = [{c: r.get(c) for c in cols} for r in rows.values()]
            if "key" in params:
                data = [d for d, r in zip(data, rows.values()) if r.get("key")]
            offset = int(params.get("offset", ["0"])[0])
            return data[offset:offset + 1000]
        if method == "DELETE":
            for name, values in params.items():
                spec = values[0]
                if spec.startswith("in.("):
                    wanted = set(re.findall(r'"((?:[^"\\]|\\.)*)"', spec))
                    for k in [k for k, r in rows.items() if str(r.get(name)) in wanted]:
                        del rows[k]
                elif spec.startswith("eq.") and name == "user_id":
                    start = params["started_at"][0][3:]
                    for k in [k for k, r in rows.items() if r["user_id"] == spec[3:] and r["started_at"] == start]:
                        del rows[k]
            return None
        raise AssertionError(method)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.product = Path(self.tmp.name)
        self.fake = FakeSupabase()
        site_traffic.clear_presence()
        for p in (
            mock.patch.dict(os.environ, {"CEREBEL_SUPABASE_URL": "https://x.example.test", "CEREBEL_SUPABASE_SERVICE_KEY": "sb_secret_x"}),
            mock.patch.object(mirror, "_request", self.fake),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.user = register(self.product, "golfer@example.com", "first-pass")["user"]
        (self.product / "events.jsonl").write_text(
            json.dumps({"ts": "2026-11-02T10:00:00+00:00", "kind": "signup", "user_id": self.user["user_id"]}) + "\n"
            + json.dumps({"ts": "2026-11-02T10:05:00+00:00", "kind": "upload_saved", "user_id": self.user["user_id"], "session_id": "S1"}) + "\n"
        )
        (self.product / "library.json").write_text(json.dumps([
            {"user_id": self.user["user_id"], "session_id": "S1", "filename": "a.mp4", "status": "ready", "created_at": "2026-11-02T10:05:00+00:00"}
        ]))
        (self.product / "activity.json").write_text(json.dumps({
            self.user["user_id"]: {"visits": [{"start": "2026-11-02T10:00:00+00:00", "end": "2026-11-02T10:20:00+00:00"}]}
        }))

    def test_first_cycle_copies_everything_but_secrets(self):
        report = mirror.Mirror(self.product).cycle()
        self.assertNotIn((-1, -1), report.values())
        self.assertEqual(len(self.fake.tables["accounts"]), 1)
        self.assertEqual(len(self.fake.tables["events"]), 2)
        self.assertEqual(len(self.fake.tables["captures"]), 1)
        self.assertEqual(len(self.fake.tables["visits"]), 1)
        sent = json.dumps([c[2] for c in self.fake.calls if c[0] == "POST"])
        accounts_raw = (self.product / "accounts.json").read_text()
        secret = json.loads(accounts_raw)["golfer@example.com"]
        for field in ("password_hash", "salt"):
            self.assertNotIn(secret[field], sent)
        tokens = json.loads((self.product / "tokens.json").read_text())
        for token in tokens:
            self.assertNotIn(token, sent)

    def test_nothing_is_sent_twice(self):
        m = mirror.Mirror(self.product)
        m.cycle()
        before = len(self.fake.calls)
        report = m.cycle()
        self.assertTrue(all(c == (0, 0) for c in report.values()), report)
        self.assertEqual([c for c in self.fake.calls[before:] if c[0] != "GET"], [])

    def test_changes_and_deletions_follow_the_files(self):
        m = mirror.Mirror(self.product)
        m.cycle()
        rows = json.loads((self.product / "library.json").read_text())
        rows[0]["status"] = "error"
        (self.product / "library.json").write_text(json.dumps(rows))
        lines = (self.product / "events.jsonl").read_text().splitlines()
        (self.product / "events.jsonl").write_text(lines[1] + "\n")  # the retention job rewrote the log
        self.assertEqual(m.cycle()["events"], (0, 1))
        self.assertEqual(self.fake.tables["captures"]["S1"]["status"], "error")
        self.assertEqual(len(self.fake.tables["events"]), 1)
        delete_account(self.product, self.user["user_id"])
        (self.product / "activity.json").write_text("{}")
        m.cycle()
        self.assertEqual(self.fake.tables["accounts"], {})
        self.assertEqual(self.fake.tables["visits"], {})

    def test_a_restart_removes_rows_deleted_while_it_was_down(self):
        mirror.Mirror(self.product).cycle()
        delete_account(self.product, self.user["user_id"])
        mirror.Mirror(self.product).cycle()  # a fresh process: no memory of what it sent
        self.assertEqual(self.fake.tables["accounts"], {})

    def test_failure_in_one_table_does_not_stop_the_others(self):
        real = self.fake

        def flaky(method, path, body=None, prefer=None):
            if path.startswith("events"):
                raise mirror.MirrorError("boom")
            return real(method, path, body, prefer)

        with mock.patch.object(mirror, "_request", flaky):
            report = mirror.Mirror(self.product).cycle()
        self.assertEqual(report["events"], (-1, -1))
        self.assertEqual(len(self.fake.tables["accounts"]), 1)

    def test_disabled_without_configuration(self):
        with mock.patch.dict(os.environ, {"CEREBEL_SUPABASE_SERVICE_KEY": ""}):
            self.assertFalse(mirror.start(self.product))


if __name__ == "__main__":
    unittest.main()
