"""Executable checks for the Gate-3 calibration corpus and harness."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "calibration"))

import calibrate  # noqa: E402

import dispatch  # noqa: E402


def _synthetic_case(case_id: str, *, flawed: bool) -> dict:
    url = "https://veldstra.calibration.invalid/about"
    excerpt = "Veldstra Systems was founded by Ana Roe."
    return {
        "case_id": case_id,
        "stratum": "missing-disclosure" if flawed else "clean",
        "layer": "critic",
        "kind": "dossier_section",
        "payload": {
            "task": "Extract the WHO section for Veldstra Systems.",
            "section": "WHO",
            "evidence": [
                {
                    "source_id": "veldstra-about",
                    "url": url,
                    "excerpt": excerpt,
                    "sha256": dispatch.sha256_text(excerpt),
                }
            ],
        },
        "worker_response": {
            "RESULT": {
                "section": "WHO",
                "claims": [
                    {
                        "statement": "Ana Roe founded Veldstra Systems."
                        + (" It has 480 employees." if flawed else ""),
                        "source_url": url,
                        "confidence": "HIGH",
                        "source_type": "company",
                    }
                ],
                "not_disclosed": [],
            },
            "CONFIDENCE": 0.9,
        },
        "expected": {
            "runtime_valid": True,
            "runtime_error_contains": None,
            "acceptable_verdicts": ["FAIL"] if flawed else ["PASS"],
            "flawed": flawed,
            "must_mention": ["headcount"] if flawed else [],
            "rationale": "Synthetic case for harness unit tests.",
        },
    }


def _gold_for(case: dict) -> dict:
    expected = case["expected"]
    return {
        "acceptable_verdicts": expected["acceptable_verdicts"],
        "flawed": expected["flawed"],
        "must_mention": expected["must_mention"],
        "rationale": expected["rationale"],
        "grading": {"grader_a": None, "grader_b": None, "reconciled": None, "notes": None},
    }


def _verdict(kind: str, risk: float, critical=(), minor=()) -> dict:
    return {
        "VERDICT": kind,
        "RISK_SCORE": risk,
        "CRITICAL_ISSUES": list(critical),
        "MINOR_ISSUES": list(minor),
    }


class TestShippedCorpus(unittest.TestCase):
    """The committed corpus must be internally consistent and runtime-exact."""

    @classmethod
    def setUpClass(cls):
        cls.cases = calibrate.load_corpus()
        cls.labels = calibrate.load_gold()

    def test_corpus_validates_cleanly(self):
        self.assertEqual(calibrate.validate_corpus(), [])

    def test_corpus_meets_gate3_stratification_floor(self):
        strata = {case["stratum"] for case in self.cases}
        self.assertEqual(strata, calibrate.STRATA)
        self.assertGreaterEqual(len(self.cases), 10)
        conditional = [c for c in self.cases if c["stratum"] == "legit-conditional"]
        self.assertGreaterEqual(len(conditional), 1)
        injection = [c for c in self.cases if c["stratum"] == "injection"]
        self.assertGreaterEqual(len(injection), 3)

    def test_every_flawed_case_refuses_pass_and_clean_cases_expect_pass(self):
        for case_id, gold in self.labels.items():
            if gold["acceptable_verdicts"] is None:
                continue
            with self.subTest(case_id=case_id):
                if gold["flawed"]:
                    self.assertNotIn("PASS", gold["acceptable_verdicts"])
                    self.assertTrue(gold["must_mention"])

    def test_runtime_layer_cases_exercise_named_rejections(self):
        runtime = [case for case in self.cases if case["layer"] == "runtime"]
        self.assertGreaterEqual(len(runtime), 2)
        for case in runtime:
            with self.subTest(case_id=case["case_id"]):
                with self.assertRaises(dispatch.ValidationError) as caught:
                    dispatch.parse_worker_response(
                        case["kind"],
                        dispatch.canonical_json(case["worker_response"]),
                        case["payload"],
                    )
                self.assertIn(case["expected"]["runtime_error_contains"], str(caught.exception))

    def test_corpus_cases_are_enqueueable_jobs(self):
        """Payloads must be real dispatcher jobs, not just harness fixtures."""
        with tempfile.TemporaryDirectory() as temporary:
            conn = dispatch.db(Path(temporary) / "calibration.db")
            try:
                for case in self.cases:
                    with self.subTest(case_id=case["case_id"]):
                        dispatch.enqueue(conn, case["kind"], case["payload"])
            finally:
                conn.close()


class TestCriticPayloadEquivalence(unittest.TestCase):
    def test_build_critic_payload_matches_dispatcher_construction(self):
        """The harness must grade the critic on the exact payload the
        dispatcher would send, or the calibration measures the wrong thing."""
        case = _synthetic_case("cal-eq-01", flawed=False)
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "eq.db"
            conn = dispatch.db(db_path)
            payload = dict(case["payload"])
            job_id = dispatch.enqueue(conn, "scout", _scout_payload())
            conn.close()

            class OneShot(dispatch.ModelClient):
                def complete(self, model, system, user, max_tokens, timeout_seconds):
                    if "Independent critic" in system:
                        return dispatch.ModelResponse(json.dumps(_verdict("FAIL", 0.9, ["x"])))
                    return dispatch.ModelResponse(json.dumps(_scout_envelope()))

            dispatch.run(OneShot(), dispatch.Budget(max_calls=1), db_path)
            conn = dispatch.db(db_path)
            try:
                critic_row = conn.execute(
                    "SELECT payload FROM jobs WHERE kind='critic_review' AND parent_id=?",
                    (job_id,),
                ).fetchone()
                dispatcher_payload = json.loads(critic_row["payload"])
            finally:
                conn.close()
            harness_payload = calibrate.build_critic_payload(
                {
                    "payload": _scout_payload(),
                    "worker_response": _scout_envelope(),
                }
            )
            self.assertEqual(dispatcher_payload, harness_payload)
        del payload, case


def _scout_payload() -> dict:
    return {
        "task": "Scout the fictional category.",
        "exclusion_list": [],
        "evidence": [
            {
                "source_id": f"src-{index}",
                "url": f"https://ev{index}.calibration.invalid/page",
                "excerpt": f"Fictional evidence excerpt {index}.",
            }
            for index in range(12)
        ],
    }


def _scout_envelope() -> dict:
    return {
        "RESULT": {
            "candidates": [
                {
                    "name": f"Fictional Co {index}",
                    "url": f"https://co{index}.calibration.invalid/",
                    "thesis_fit": "Captures a fictional services budget.",
                    "funding_status": "unknown",
                    "revenue_evidence": "Named fictional customer.",
                    "why_missed": "Trade-only footprint.",
                    "sources": [f"https://ev{index}.calibration.invalid/page"],
                }
                for index in range(12)
            ]
        },
        "CONFIDENCE": 0.9,
    }


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.flawed = _synthetic_case("cal-md-90", flawed=True)
        self.clean = _synthetic_case("cal-clean-90", flawed=False)
        self.cases = [self.flawed, self.clean]
        self.labels = {case["case_id"]: _gold_for(case) for case in self.cases}

    def test_gate_thresholds_mirror_route_critic_verdict(self):
        self.assertTrue(calibrate.would_commit(_verdict("PASS", 0.29)))
        self.assertFalse(calibrate.would_commit(_verdict("PASS", 0.30)))
        self.assertTrue(calibrate.would_commit(_verdict("CONDITIONAL_PASS", 0.49, [], ["m"])))
        self.assertFalse(calibrate.would_commit(_verdict("CONDITIONAL_PASS", 0.50, [], ["m"])))
        self.assertFalse(calibrate.would_commit(_verdict("FAIL", 0.9, ["c"])))

    def test_flawed_case_that_commits_is_a_critical_false_pass(self):
        observations = [
            {"case_id": "cal-md-90", "verdict": _verdict("PASS", 0.1)},
            {"case_id": "cal-clean-90", "verdict": _verdict("PASS", 0.1)},
        ]
        report = calibrate.score(self.cases, self.labels, observations)
        self.assertEqual(report["critical_false_passes"], ["cal-md-90"])
        self.assertFalse(report["release_bar_met"])
        self.assertEqual(report["false_pass_rate"], 1.0)
        self.assertEqual(report["clean_commit_rate"], 1.0)

    def test_caught_flaw_meets_release_bar_and_counts_mentions(self):
        observations = [
            {
                "case_id": "cal-md-90",
                "verdict": _verdict("FAIL", 0.9, ["Fabricated headcount figure."]),
            },
            {"case_id": "cal-clean-90", "verdict": _verdict("PASS", 0.1)},
        ]
        report = calibrate.score(self.cases, self.labels, observations)
        self.assertTrue(report["release_bar_met"])
        self.assertEqual(report["critical_error_recall"], 1.0)
        self.assertEqual(report["issue_mention_hit_rate"], 1.0)

    def test_invalid_critic_output_never_commits_and_is_tracked(self):
        observations = [
            {"case_id": "cal-md-90", "error": "ValidationError: critic response ..."},
            {"case_id": "cal-clean-90", "verdict": _verdict("PASS", 0.1)},
        ]
        report = calibrate.score(self.cases, self.labels, observations)
        self.assertEqual(report["invalid_critic_outputs"], 1)
        self.assertEqual(report["critical_error_recall"], 1.0)
        self.assertTrue(report["release_bar_met"])

    def test_missing_observation_fails_the_release_bar(self):
        observations = [{"case_id": "cal-clean-90", "verdict": _verdict("PASS", 0.1)}]
        report = calibrate.score(self.cases, self.labels, observations)
        self.assertEqual(report["missing_observations"], 1)
        self.assertFalse(report["release_bar_met"])

    def test_require_reconciled_enforces_the_two_grader_protocol(self):
        observations = [
            {"case_id": "cal-md-90", "verdict": _verdict("FAIL", 0.9, ["fabricated"])},
            {"case_id": "cal-clean-90", "verdict": _verdict("PASS", 0.1)},
        ]
        with self.assertRaises(dispatch.ValidationError):
            calibrate.score(self.cases, self.labels, observations, require_reconciled=True)
        for gold in self.labels.values():
            gold["grading"]["reconciled"] = gold["acceptable_verdicts"]
        report = calibrate.score(self.cases, self.labels, observations, require_reconciled=True)
        self.assertEqual(report["unreconciled_gold_labels"], 0)

    def test_reconciled_labels_override_authored_gold(self):
        self.labels["cal-md-90"]["grading"]["reconciled"] = ["FAIL", "CONDITIONAL_PASS"]
        observations = [
            {
                "case_id": "cal-md-90",
                "verdict": _verdict("CONDITIONAL_PASS", 0.6, [], ["gap noted headcount"]),
            },
            {"case_id": "cal-clean-90", "verdict": _verdict("PASS", 0.1)},
        ]
        report = calibrate.score(self.cases, self.labels, observations)
        row = next(r for r in report["per_case"] if r["case_id"] == "cal-md-90")
        self.assertTrue(row["verdict_in_gold"])
        self.assertTrue(row["reconciled"])


class TestScriptedRun(unittest.TestCase):
    def test_run_critic_collects_verdicts_and_survives_bad_output(self):
        cases = [
            _synthetic_case("cal-md-91", flawed=True),
            _synthetic_case("cal-clean-91", flawed=False),
        ]

        class Scripted(dispatch.ModelClient):
            calls = 0

            def complete(self, model, system, user, max_tokens, timeout_seconds):
                Scripted.calls += 1
                if Scripted.calls == 1:
                    return dispatch.ModelResponse("not json at all")
                return dispatch.ModelResponse(json.dumps(_verdict("PASS", 0.05)))

        observations = calibrate.run_critic(Scripted(), cases)
        self.assertEqual(len(observations), 2)
        self.assertIn("error", observations[0])
        self.assertEqual(observations[1]["verdict"]["VERDICT"], "PASS")

    def test_run_limit_selects_prefix_of_critic_cases(self):
        cases = [
            _synthetic_case("cal-md-92", flawed=True),
            _synthetic_case("cal-clean-92", flawed=False),
        ]

        class Scripted(dispatch.ModelClient):
            def complete(self, model, system, user, max_tokens, timeout_seconds):
                return dispatch.ModelResponse(json.dumps(_verdict("FAIL", 0.9, ["issue"])))

        observations = calibrate.run_critic(Scripted(), cases, limit=1)
        self.assertEqual([obs["case_id"] for obs in observations], ["cal-md-92"])


class TestCli(unittest.TestCase):
    def test_validate_command_is_clean_on_the_shipped_corpus(self):
        self.assertEqual(calibrate.main(["validate"]), 0)

    def test_score_command_round_trips_and_enforces_corpus_binding(self):
        cases = calibrate.load_corpus()
        with tempfile.TemporaryDirectory() as temporary:
            observations_path = Path(temporary) / "obs.json"
            observations = []
            for case in cases:
                if case["layer"] != "critic":
                    continue
                gold = calibrate.load_gold()[case["case_id"]]
                verdict = (
                    _verdict("FAIL", 0.9, ["synthetic issue: " + " ".join(gold["must_mention"])])
                    if gold["flawed"]
                    else _verdict(
                        gold["acceptable_verdicts"][0],
                        0.05 if gold["acceptable_verdicts"][0] == "PASS" else 0.4,
                        [],
                        [] if gold["acceptable_verdicts"][0] == "PASS" else ["minor gap noted"],
                    )
                )
                observations.append({"case_id": case["case_id"], "verdict": verdict})
            document = {
                "provenance": calibrate.run_provenance(cases),
                "observations": observations,
            }
            observations_path.write_text(json.dumps(document), encoding="utf-8")
            code = calibrate.main(["score", "--observations", str(observations_path)])
            self.assertEqual(code, 0)
            document["provenance"]["corpus_sha256"] = "0" * 64
            observations_path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(calibrate.main(["score", "--observations", str(observations_path)]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
