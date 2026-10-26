"""Instant coach answers are mapped to the finding the chip asks about."""

from __future__ import annotations

import unittest
from unittest import mock

from motion_agent.app.coach_briefs import build_coach_briefs, match_brief
from motion_agent.app.product_session import CONSUMER_GOLF_QUESTIONS

ARM_Q = CONSUMER_GOLF_QUESTIONS[2]
PELVIS_Q = CONSUMER_GOLF_QUESTIONS[3]


def _note(fid: str, series: str, title: str, why: str, value: str) -> dict:
    return {
        "id": fid,
        "title": title,
        "headline": title,
        "detail": title,
        "series": series,
        "metric": {"value": value, "caption": series},
        "why": why,
        "cue": "",
        "drill": "",
    }


# Same wording as the motion board: the hip-turn and knee notes talk about arms and hips.
SEPARATION = _note(
    "separation",
    "separation",
    "Hips and shoulders turn together",
    "With little of it, the arms have to make the speed.",
    "-8°",
)
TRAIL_KNEE = _note(
    "trail-knee",
    "trail-knee",
    "Trail knee straightens",
    "When the back knee locks, the hips stop turning and the arms have to invent the swing.",
    "59° → 41°",
)
LEAD_ARM = _note(
    "lead-arm",
    "lead-elbow",
    "Lead arm bends",
    "A bent lead arm narrows the swing radius.",
    "51°",
)
EARLY_EXT = _note(
    "early-extension",
    "pelvis-drift",
    "Early extension",
    "Standing up through impact sends the club out and up.",
    "20 cm",
)


def _briefs(findings: list[dict]) -> dict[str, dict]:
    payload = {"error": False, "sport": "golf", "phases": [], "findings": findings}
    with mock.patch("motion_agent.app.coach_briefs.get_sport_analysis", return_value=payload):
        out = build_coach_briefs(video_id="s1", sport="golf")
    return {b["question"]: b for b in out["briefs"]}


class CoachBriefMappingTests(unittest.TestCase):
    def test_lead_arm_chip_skips_hip_turn_note_that_mentions_arms(self) -> None:
        briefs = _briefs([SEPARATION, TRAIL_KNEE, LEAD_ARM, EARLY_EXT])
        self.assertEqual(briefs[ARM_Q]["finding_id"], "lead-arm")
        self.assertIn("51°", briefs[ARM_Q]["answer"])
        self.assertNotIn("-8°", briefs[ARM_Q]["answer"])

    def test_pelvis_chip_skips_notes_that_mention_hips(self) -> None:
        briefs = _briefs([SEPARATION, TRAIL_KNEE, LEAD_ARM, EARLY_EXT])
        self.assertEqual(briefs[PELVIS_Q]["finding_id"], "early-extension")
        self.assertIn("20 cm", briefs[PELVIS_Q]["answer"])

    def test_no_matching_finding_means_no_instant_answer(self) -> None:
        briefs = _briefs([SEPARATION, TRAIL_KNEE])
        # The chip falls through to the coach instead of answering with another fault.
        self.assertNotIn(ARM_Q, briefs)
        self.assertNotIn(PELVIS_Q, briefs)
        self.assertEqual(briefs[CONSUMER_GOLF_QUESTIONS[0]]["finding_id"], "fault_incomplete_shoulder_turn")

    def test_reading_on_the_right_trace_answers_the_chip(self) -> None:
        reading = _note(
            "read-lead-elbow",
            "lead-elbow",
            "Lead elbow flex",
            "This is the largest change on this trace.",
            "112°",
        )
        briefs = _briefs([SEPARATION, reading])
        self.assertEqual(briefs[ARM_Q]["finding_id"], "read-lead-elbow")

    def test_spoken_question_finds_the_arm_brief(self) -> None:
        payload = {
            "error": False,
            "sport": "golf",
            "phases": [],
            "findings": [SEPARATION, LEAD_ARM],
        }
        with mock.patch("motion_agent.app.coach_briefs.get_sport_analysis", return_value=payload):
            briefs = build_coach_briefs(video_id="s1", sport="golf")["briefs"]
        hit = match_brief("is my lead arm collapsing", briefs)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["finding_id"], "lead-arm")


if __name__ == "__main__":
    unittest.main()
