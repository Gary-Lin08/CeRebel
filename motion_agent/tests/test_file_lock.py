"""Writers in different processes (chat_server, retention, privacy_backfill) must not lose rows."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from motion_agent.app import accounts, billing, file_lock, library, privacy_backfill, product_api
from motion_agent.tests.test_billing import SECRET, _sign
from motion_agent.tests.test_privacy_retention import _settings

APPENDERS = 3
ROWS_EACH = 150
REWRITES = 80
ACCOUNTS_EACH = 20  # registering hashes the password (~50 ms), so fewer rows
START_TIMEOUT_S = 60


def _append_events(data: str, worker: int, go) -> None:
    settings = _settings(Path(data))
    go.wait(START_TIMEOUT_S)
    for i in range(ROWS_EACH):
        product_api.record_event(settings, "chat_question", session_id=f"S{worker}-{i}")


def _rewrite_events(data: str, go) -> None:
    """What retention and privacy_backfill do: read the whole log, write a .tmp, replace."""
    settings = _settings(Path(data))
    go.wait(START_TIMEOUT_S)
    for i in range(REWRITES):
        product_api.record_event(settings, "signup", user_id="gone")
        product_api.scrub_events(settings, user_id="gone")
        product_api.record_event(settings, "account_deleted", user_id="gone2", n=i)
        privacy_backfill._anonymize_deletion_rows(settings, {"gone2"})  # noqa: SLF001


def _add_captures(data: str, worker: int, go) -> None:
    product_dir = _settings(Path(data)).product_dir
    go.wait(START_TIMEOUT_S)
    for i in range(ROWS_EACH):
        library.add_capture(product_dir, user_id=f"u{worker}", session_id=f"S{worker}-{i}", filename="a.mp4")


def _purge_captures(data: str, go) -> None:
    product_dir = _settings(Path(data)).product_dir
    go.wait(START_TIMEOUT_S)
    for i in range(REWRITES):
        library.add_capture(product_dir, user_id="gone", session_id=f"G{i}", filename="a.mp4")
        library.purge_user_rows(product_dir, "gone")


def _register(data: str, worker: int, go) -> None:
    product_dir = _settings(Path(data)).product_dir
    go.wait(START_TIMEOUT_S)
    for i in range(ACCOUNTS_EACH):
        accounts.register(product_dir, f"w{worker}-{i}@example.com", "swing-fix-123")


def _delete_accounts(data: str, go) -> None:
    """What retention does for an inactive account: rewrite accounts.json and tokens.json."""
    product_dir = _settings(Path(data)).product_dir
    gone = accounts.list_accounts(product_dir)[0]["user_id"]
    go.wait(START_TIMEOUT_S)
    for _ in range(REWRITES):
        accounts.delete_account(product_dir, gone)


def _webhook(product_dir: Path, user_id: str, event_id: str) -> None:
    payload = json.dumps({
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {"mode": "subscription", "client_reference_id": user_id, "subscription": "sub_" + user_id}},
    }).encode()
    billing.handle_webhook(product_dir, payload, _sign(payload))


def _subscribe(data: str, worker: int, go) -> None:
    product_dir = _settings(Path(data)).product_dir
    go.wait(START_TIMEOUT_S)
    with mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET}):
        for i in range(ROWS_EACH):
            _webhook(product_dir, f"u{worker}-{i}", f"evt_{worker}_{i}")


def _forget_billing(data: str, go) -> None:
    product_dir = _settings(Path(data)).product_dir
    go.wait(START_TIMEOUT_S)
    with mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET}):
        for i in range(REWRITES):
            _webhook(product_dir, "gone", f"evt_gone_{i}")
            product_api._forget_billing(product_dir, "gone")  # noqa: SLF001


class CrossProcessWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.data.mkdir()
        self.settings = _settings(self.data)

    def tearDown(self):
        self._tmp.cleanup()

    def _race(self, appender, rewriter) -> None:
        ctx = multiprocessing.get_context("spawn")
        go = ctx.Event()
        procs = [ctx.Process(target=appender, args=(str(self.data), w, go)) for w in range(APPENDERS)]
        procs.append(ctx.Process(target=rewriter, args=(str(self.data), go)))
        for proc in procs:
            proc.start()
        go.set()
        for proc in procs:
            proc.join(START_TIMEOUT_S)
        self.assertEqual([proc.exitcode for proc in procs], [0] * len(procs))

    def test_appends_survive_events_rewrites_in_other_processes(self):
        self._race(_append_events, _rewrite_events)
        path = self.settings.product_dir / "events.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        questions = {r["session_id"] for r in rows if r["kind"] == "chat_question"}
        self.assertEqual(len(questions), APPENDERS * ROWS_EACH)
        self.assertFalse([r for r in rows if r.get("user_id") in {"gone", "gone2"}])
        self.assertEqual(sum(r["kind"] == "account_deleted" for r in rows), REWRITES)

    def test_library_rows_survive_rewrites_in_other_processes(self):
        self._race(_add_captures, _purge_captures)
        rows = library.all_captures(self.settings.product_dir)
        self.assertEqual(len({r["session_id"] for r in rows}), APPENDERS * ROWS_EACH)
        self.assertFalse([r for r in rows if r["user_id"] == "gone"])

    def test_accounts_and_tokens_survive_deletions_in_other_processes(self):
        product_dir = self.settings.product_dir
        gone = accounts.register(product_dir, "gone@example.com", "swing-fix-123")["user"]["user_id"]
        self._race(_register, _delete_accounts)
        rows = accounts.list_accounts(product_dir)
        self.assertEqual(len(rows), APPENDERS * ACCOUNTS_EACH)
        self.assertNotIn(gone, {r["user_id"] for r in rows})
        for row in rows:
            self.assertEqual(len(accounts.tokens_for_user(product_dir, row["user_id"])), 1, row["email"])

    def test_subscriptions_survive_billing_rewrites_in_other_processes(self):
        self._race(_subscribe, _forget_billing)
        users = billing._read(self.settings.product_dir)["users"]  # noqa: SLF001
        self.assertEqual(len(users), APPENDERS * ROWS_EACH)
        self.assertNotIn("gone", users)

    def test_lock_file_sits_next_to_the_data_file_and_outlives_replace(self):
        path = self.settings.product_dir / "events.jsonl"
        product_api.record_event(self.settings, "signup", user_id="gone")
        product_api.scrub_events(self.settings, user_id="gone")
        lock = file_lock.lock_path(path)
        self.assertEqual(lock, self.settings.product_dir / "events.jsonl.lock")
        self.assertTrue(lock.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
