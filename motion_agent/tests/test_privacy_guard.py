"""The deployment must match what the video consent and the Privacy Policy disclose.

privacy_guard compares the server's configuration with consent.PROCESSORS; the coach has no
built-in model endpoint any more (the old default was DashScope's mainland-China host); the
consent text and the Privacy Policy's processor table come from the same list.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from motion_agent.app import consent, privacy_guard

# What a deployment that matches today's disclosure looks like (DigitalOcean web, Modal GPU,
# Together AI coach, SpaceMail email).
US_PLAN = {
    "CEREBEL_DATA_DIR": "/opt/cerebel/data",
    "CEREBEL_HOST_PROVIDER": "digitalocean",
    "CEREBEL_GPU_BACKEND": "modal",
    "MOTION_AGENT_BACKEND": "dashscope",
    "MOTION_AGENT_BASE_URL": "https://api.together.xyz/v1",
    "CEREBEL_SMTP_HOST": "mail.spacemail.com",
}

# What /api/health reported on 2026-10-26: the old onethingai GPU box, the local GEM-X worker.
LEGACY_BOX = {
    "CEREBEL_DATA_DIR": "/root/CerebelServer/data",
    "CEREBEL_GPU_BACKEND": "local",
    "MOTION_AGENT_BACKEND": "dashscope",
}


def _codes(env: dict) -> set[str]:
    return {f["code"] for f in privacy_guard.findings(env)}


class FindingsTests(unittest.TestCase):
    def test_the_disclosed_us_plan_is_consistent(self):
        self.assertEqual(privacy_guard.findings(US_PLAN), [])
        self.assertTrue(privacy_guard.report(US_PLAN)["ok"])

    def test_the_live_server_as_observed_is_flagged(self):
        codes = _codes(LEGACY_BOX)
        self.assertIn("LEGACY_HOST", codes)
        self.assertIn("HOST_UNDECLARED", codes)
        self.assertIn("GPU_LOCAL", codes)
        self.assertIn("LLM_UNSET", codes)

    def test_mainland_china_model_endpoint_is_flagged(self):
        env = {**US_PLAN, "MOTION_AGENT_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
        found = privacy_guard.findings(env)
        self.assertEqual([f["code"] for f in found], ["LLM_NOT_DISCLOSED"])
        self.assertIn("China (mainland)", found[0]["message"])

    def test_separate_embedding_host_is_checked_too(self):
        env = {**US_PLAN, "MOTION_AGENT_KB_EMBED_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"}
        self.assertEqual(_codes(env), {"EMBEDDINGS_NOT_DISCLOSED"})
        same = {**US_PLAN, "MOTION_AGENT_KB_EMBED_BASE_URL": "https://api.together.xyz/v1"}
        self.assertEqual(_codes(same), set())

    def test_unknown_hosts_are_reported_not_guessed(self):
        self.assertEqual(_codes({**US_PLAN, "MOTION_AGENT_BASE_URL": "https://llm.example/v1"}), {"LLM_UNKNOWN"})
        self.assertEqual(_codes({**US_PLAN, "CEREBEL_SMTP_HOST": "smtp.example"}), {"EMAIL_UNKNOWN"})

    def test_hosting_and_gpu_must_be_disclosed(self):
        self.assertEqual(_codes({**US_PLAN, "CEREBEL_HOST_PROVIDER": "Mac mini (Boston)"}), {"HOST_NOT_DISCLOSED"})
        self.assertEqual(_codes({**US_PLAN, "CEREBEL_GPU_BACKEND": "local"}), {"GPU_LOCAL"})

    def test_mock_coach_needs_no_endpoint(self):
        env = {k: v for k, v in US_PLAN.items() if k != "MOTION_AGENT_BASE_URL"}
        self.assertIn("LLM_UNSET", _codes(env))
        self.assertNotIn("LLM_UNSET", _codes({**env, "MOTION_AGENT_BACKEND": "mock"}))

    def test_gate_modes(self):
        found = privacy_guard.findings(LEGACY_BOX)
        self.assertFalse(privacy_guard.blocks_uploads(found, {}))  # default is warn
        self.assertFalse(privacy_guard.blocks_uploads(found, {"CEREBEL_PRIVACY_GATE": "bogus"}))
        self.assertTrue(privacy_guard.blocks_uploads(found, {"CEREBEL_PRIVACY_GATE": "enforce"}))
        self.assertTrue(privacy_guard.blocks_coach(found, {"CEREBEL_PRIVACY_GATE": "ENFORCE"}))
        hosting_only = [f for f in found if f["kind"] != "llm"]
        self.assertFalse(privacy_guard.blocks_coach(hosting_only, {"CEREBEL_PRIVACY_GATE": "enforce"}))
        self.assertFalse(privacy_guard.blocks_uploads([], {"CEREBEL_PRIVACY_GATE": "enforce"}))

    def test_cli_exit_code(self):
        with mock.patch.dict(os.environ, US_PLAN, clear=True), mock.patch("builtins.print"):
            self.assertEqual(privacy_guard.main([]), 0)
        with mock.patch.dict(os.environ, LEGACY_BOX, clear=True), mock.patch("builtins.print") as out:
            self.assertEqual(privacy_guard.main([]), 1)
        self.assertIn("LEGACY_HOST", " ".join(str(c) for c in out.call_args_list))


class ModelEndpointDefaultTests(unittest.TestCase):
    def test_coach_client_has_no_built_in_endpoint(self):
        from motion_agent.llm.qwen_client import QwenDashScopeVLM

        env = {k: v for k, v in os.environ.items() if k != "MOTION_AGENT_BASE_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                QwenDashScopeVLM(api_key="k")
        self.assertIn("MOTION_AGENT_BASE_URL", str(ctx.exception))
        with mock.patch.dict(os.environ, {"MOTION_AGENT_BASE_URL": "https://api.together.xyz/v1"}):
            self.assertEqual(QwenDashScopeVLM(api_key="k").base_url, "https://api.together.xyz/v1")

    def test_embedder_has_no_built_in_endpoint(self):
        from motion_agent.knowledge import DashScopeEmbedder

        env = {k: v for k, v in os.environ.items()
               if k not in {"MOTION_AGENT_BASE_URL", "MOTION_AGENT_KB_EMBED_BASE_URL"}}
        with mock.patch.dict(os.environ, env, clear=True):
            emb = DashScopeEmbedder(api_key="k")
            self.assertIsNone(emb.base_url)
            with self.assertRaises(RuntimeError):
                emb._get_client()

    def test_no_china_endpoint_left_in_the_product_code(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("llm/qwen_client.py", "knowledge/__init__.py", "app/config.py", "app/chat_server.py"):
            self.assertNotIn("aliyuncs.com", (root / rel).read_text(encoding="utf-8"), rel)

    def test_health_reports_a_missing_endpoint(self):
        from motion_agent.app.config import Settings
        from motion_agent.app.health import health_report

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), host="127.0.0.1", port=1, backend="dashscope",
                                prompt_password=None, max_upload_bytes=1, invite_daily_limit=1)
            env = {k: v for k, v in os.environ.items() if k != "MOTION_AGENT_BASE_URL"}
            with mock.patch.dict(os.environ, {**env, "MOTION_AGENT_API_KEY": "k"}, clear=True), mock.patch(
                "motion_agent.app.product_api.gpu_status", return_value="ready"
            ):
                code, payload = health_report(settings)
        self.assertEqual(code, 503)
        self.assertEqual(payload["model"]["credential"], "endpoint_missing")


class DisclosureSourceTests(unittest.TestCase):
    # Accounts that agreed to version 2026-10-24-us agreed to exactly this text. Any change to the
    # wording or the processors it names needs a new VIDEO_CONSENT_VERSION (everyone is asked again).
    CONSENT_SHA256 = {"2026-10-24-us": "2fdc686a18901d4c3cf99e3144dff35f7b11cd746cdaf9fbb1e3fe408c108e9c"}

    def test_consent_text_changes_need_a_new_version(self):
        env = {k: v for k, v in os.environ.items()
               if k not in {"CEREBEL_RAW_VIDEO_RETENTION_DAYS", "CEREBEL_INACTIVE_DELETE_DAYS"}}
        with mock.patch.dict(os.environ, env, clear=True):
            digest = hashlib.sha256(consent.video_consent_text().encode("utf-8")).hexdigest()
        self.assertIn(consent.VIDEO_CONSENT_VERSION, self.CONSENT_SHA256,
                      "new consent version: record the hash of its text here")
        self.assertEqual(digest, self.CONSENT_SHA256[consent.VIDEO_CONSENT_VERSION],
                         "the consent text changed: bump VIDEO_CONSENT_VERSION")

    def test_every_processor_is_typed_and_in_the_us(self):
        kinds = {p["kind"] for p in consent.PROCESSORS}
        self.assertEqual(kinds, {"hosting", "gpu", "llm", "payments", "network", "email"})
        for p in consent.PROCESSORS:
            self.assertEqual(p["country"], "United States", p["name"])
            self.assertTrue(p["role"] and p["policy_role"], p["name"])

    def test_privacy_policy_table_comes_from_the_list(self):
        from motion_agent.app import chat_server

        html = chat_server._legal_html("privacy").decode("utf-8")
        self.assertNotIn("{{PROCESSOR_ROWS}}", html)
        for p in consent.PROCESSORS:
            self.assertIn(f"<tr><td>{p['name']}</td>", html)
        self.assertIn('<td>Account and "analysis ready" emails</td>', html)
        with mock.patch.object(consent, "PROCESSORS", consent.PROCESSORS + [
            {"name": "Acme <GPU>", "kind": "gpu", "country": "United States", "role": "r", "policy_role": "x & y"}
        ]):
            html = chat_server._legal_html("privacy").decode("utf-8")
        self.assertIn("<td>Acme &lt;GPU&gt;</td><td>x &amp; y</td>", html)

    def test_consent_payload_sends_only_display_fields(self):
        payload = consent.public_video_consent()
        self.assertEqual(set(payload["processors"][0]), {"name", "role", "country"})


class GateHttpTests(unittest.TestCase):
    """With CEREBEL_PRIVACY_GATE=enforce a mismatched server pauses uploads (and the coach for an LLM
    mismatch) before reading the upload or counting a question."""

    @classmethod
    def setUpClass(cls):
        from motion_agent.app import chat_server
        from motion_agent.app.accounts import register
        from motion_agent.app.config import Settings

        cls.chat_server = chat_server
        cls.tmp = tempfile.TemporaryDirectory()
        settings = Settings(data_dir=Path(cls.tmp.name), host="127.0.0.1", port=1, backend="mock",
                            prompt_password=None, max_upload_bytes=1_000_000, invite_daily_limit=3)
        cls.token = register(settings.product_dir, "gate@example.com", "longenough")["token"]
        cls.patches = [mock.patch.object(chat_server, "SETTINGS", settings),
                       mock.patch.object(chat_server, "DATA_DIR", settings.data_dir)]
        for p in cls.patches:
            p.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def _post(self, path: str, body: bytes, ctype: str) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}", data=body,
                                     headers={"Content-Type": ctype, "Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_enforce_pauses_uploads_and_coach(self):
        env = {**LEGACY_BOX, "CEREBEL_PRIVACY_GATE": "enforce"}
        with mock.patch.dict(os.environ, env), mock.patch(
            "motion_agent.app.product_api.parse_upload"
        ) as parse, mock.patch("motion_agent.app.billing.take_question") as take:
            code, payload = self._post("/api/upload", b"--x--", "multipart/form-data; boundary=x")
            self.assertEqual(code, 503)
            self.assertIn("didn't use one of your analyses", payload["message"])
            parse.assert_not_called()
            code, payload = self._post("/api/chat", json.dumps({"question": "hi"}).encode(), "application/json")
            self.assertEqual(code, 503)
            self.assertTrue(payload["unavailable"])
            take.assert_not_called()

    def test_warn_mode_changes_nothing(self):
        env = {**LEGACY_BOX, "CEREBEL_PRIVACY_GATE": "warn"}
        with mock.patch.dict(os.environ, env), mock.patch(
            "motion_agent.app.product_api.parse_upload", return_value=(200, {"ok": True})
        ) as parse:
            code, _ = self._post("/api/upload", b"--x--", "multipart/form-data; boundary=x")
        self.assertEqual(code, 200)
        parse.assert_called_once()


if __name__ == "__main__":
    unittest.main()
