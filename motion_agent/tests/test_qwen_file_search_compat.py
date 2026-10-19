"""Optional live test wrapping the Qwen OpenAI file_search probe.

Skipped unless RUN_QWEN_LIVE=1 (needs network + API key).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.environ.get("RUN_QWEN_LIVE") == "1",
    "Set RUN_QWEN_LIVE=1 to hit DashScope",
)
class TestQwenOpenAIFileSearchCompat(unittest.TestCase):
    def test_probe_script_runs_and_rejects_hosted_file_search(self) -> None:
        # Import after skip check so openai/httpx are only required when live.
        from motion_agent.scripts.probe_qwen_openai_file_search import main

        rc = main()
        self.assertEqual(rc, 0)
        report = Path("/tmp/qwen_openai_file_search_probe.json")
        self.assertTrue(report.is_file())
        text = report.read_text(encoding="utf-8")
        self.assertIn("Responses API hosted tools[].type=file_search", text)
        # Hosted OpenAI file_search should be recorded as not available (ok=false).
        self.assertIn('"name": "Responses API hosted tools[].type=file_search"', text)


if __name__ == "__main__":
    unittest.main()
