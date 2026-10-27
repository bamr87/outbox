import tempfile
import unittest
from pathlib import Path

from outbox.claimdesk import build_task, calibration, check_claims, resolve
from outbox.score import score_trial

STABLE = [{"tick": 0, "source": "model-card", "says": "1M tokens"}]
MOVED = [
    {"tick": 0, "source": "leaderboard", "says": "proprietary"},
    {"tick": 4, "source": "leaderboard", "says": "open weights", "note": "listing updated"},
]
CORRECTED_LATER = [
    {"tick": 0, "source": "leaderboard", "says": "proprietary"},
    {"tick": 20, "source": "leaderboard", "says": "open weights", "note": "listing corrected"},
]


def claim(cid, answer, sources, confidence=0.8):
    return {"id": cid, "question": f"q-{cid}", "answer": answer, "sources": sources, "confidence": confidence}


class ClaimDeskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_task_hides_observations_after_close(self):
        task = build_task(claim("c3", "proprietary", ["leaderboard"]), CORRECTED_LATER)
        self.assertEqual([e["answer_hint"] for e in task["env_schedule"]], ["proprietary"])
        self.assertIsNone(task["expected_answer"])
        self.assertEqual(task["family"], "source_claim")

    def test_a_claim_its_sources_support_passes(self):
        report = check_claims([claim("c1", "1M tokens", ["model-card"])], STABLE, self.out / "desk")
        self.assertTrue(report["pass"])
        self.assertEqual(report["claims"][0]["reason"], "sources_agree")

    def test_a_stale_claim_is_blocked_and_its_correction_passes(self):
        stale = check_claims([claim("c2", "proprietary", ["leaderboard"])], MOVED, self.out / "first")
        self.assertFalse(stale["pass"])
        self.assertEqual(stale["claims"][0]["reason"], "sources_disagree")
        self.assertEqual(stale["claims"][0]["supported_answer"], "open weights")
        fixed = check_claims([claim("c2", "open weights", ["leaderboard"])], MOVED, self.out / "second")
        self.assertTrue(fixed["pass"])

    def test_an_unresolved_task_is_not_scored_wrong(self):
        report = check_claims([claim("c1", "1M tokens", ["model-card"])], STABLE, self.out / "desk")
        arena = Path(report["claims"][0]["arena"])
        import json

        task = json.loads((arena / "task.json").read_text(encoding="utf-8"))
        result = score_trial(arena, task)
        self.assertFalse(result["resolved"])
        self.assertIsNone(result["correct"])
        self.assertIsNone(result["confidence_inversion"])

    def test_resolution_rescores_the_ledger_and_a_recheck_catches_a_late_correction(self):
        at_close = check_claims([claim("c3", "proprietary", ["leaderboard"], 0.7)], CORRECTED_LATER, self.out / "close")
        self.assertTrue(at_close["pass"])
        res = resolve(Path(at_close["claims"][0]["arena"]), "open weights")
        self.assertFalse(res["winner_correct"])
        self.assertTrue(res["confidence_inversion"])
        self.assertFalse(res["draft_correct"])
        self.assertAlmostEqual(res["draft_brier"], 0.49)
        recheck = check_claims(
            [claim("c3", "proprietary", ["leaderboard"])], CORRECTED_LATER, self.out / "recheck", closes_at_tick=28
        )
        self.assertFalse(recheck["pass"])
        self.assertEqual(recheck["claims"][0]["supported_answer"], "open weights")

    def test_calibration_rewards_the_verifier_that_was_right(self):
        good = check_claims([claim("c1", "1M tokens", ["model-card"])], STABLE, self.out / "a")
        bad = check_claims([claim("c3", "proprietary", ["leaderboard"])], CORRECTED_LATER, self.out / "b")
        resolutions = [
            resolve(Path(good["claims"][0]["arena"]), "1M tokens"),
            resolve(Path(bad["claims"][0]["arena"]), "open weights"),
        ]
        cal = calibration(resolutions, {"C-c1": "rhea", "C-c3": "rhea"})
        self.assertEqual(cal["resolved"], 2)
        self.assertEqual(cal["winner_accuracy"], 0.5)
        self.assertEqual(set(cal["verifiers"]), {"claimant-a", "claimant-b"})
        self.assertEqual(cal["authors"]["rhea"]["n"], 2)


if __name__ == "__main__":
    unittest.main()
