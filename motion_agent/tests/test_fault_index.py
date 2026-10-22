"""Tests for KB fault index ↔ detector wiring."""

from __future__ import annotations

import unittest

from motion_agent.knowledge.fault_index import (
    enrich_finding,
    load_sport_faults,
    resolve_fault_id,
)
from motion_agent.sports.fault_event_map import catalog_summary, lookup


class FaultIndexTests(unittest.TestCase):
    def test_golf_kb_has_canonical_faults(self):
        faults = load_sport_faults("golf")
        self.assertIn("fault_early_extension", faults)
        self.assertGreaterEqual(
            len([k for k in faults if k.startswith("fault_")]), 25
        )

    def test_detector_maps_to_fault(self):
        self.assertEqual(
            resolve_fault_id("golf", "early-extension"), "fault_early_extension"
        )
        self.assertEqual(
            resolve_fault_id("baseball", "back-leg"), "fault_back_leg_soft"
        )

    def test_enrich_attaches_kb_fields(self):
        note = {
            "id": "early-extension",
            "title": "Early extension",
            "detail": "hips stand up",
        }
        enriched = enrich_finding("golf", note)
        self.assertEqual(enriched.get("fault_id"), "fault_early_extension")
        self.assertTrue(enriched.get("cue") or enriched.get("drill"))

    def test_lookup_and_catalog(self):
        row = lookup("golf", detector_id="early-extension", phase_id="impact")
        self.assertEqual(row["fault_id"], "fault_early_extension")
        summary = catalog_summary()
        self.assertTrue(any(r["sport"] == "golf" for r in summary))


if __name__ == "__main__":
    unittest.main()
