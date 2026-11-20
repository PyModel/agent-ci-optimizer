"""Four-category policy tests: stale, failed and ambiguous evidence cannot pass."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate_validation as validation

NOW = datetime(2026, 11, 17, 12, 10, tzinfo=timezone.utc)
REV = "a" * 40
INPUTS = "sha256:" + "b" * 64
ARTIFACT = "sha256:" + "c" * 64


def fixture():
    policy = {
        "schema": "ci-validation-policy/1", "repository": "example/project",
        "feedback_budget_s": 300, "minimum_pr_samples": 2, "max_evidence_age_s": 120,
        "categories": {name: [{"identity": f"github-app:123/workflow:ci/{name}"}]
                       for name in validation.CATEGORIES},
        "integration": {"mode": "rolling", "max_green_age_s": 3600,
                        "max_running_age_s": 1800, "max_unverified_commits": 10},
        "rollout": {"enabled": True, "stages_pct": [5, 25, 100], "minimum_samples": 1000,
                    "max_age_s": 300, "minimum_observation_s": 120,
                    "metrics": {"error_rate": {"max": 0.01},
                                "p95_latency_ms": {"max": 200, "max_relative_increase": 0.1}}},
    }
    fingerprint = validation.digest(policy)

    def record(categories, start, end, revision=REV, inputs=None):
        item = {"revision": revision, "received_at": f"2026-11-17T12:{start}:00Z",
                "policy_digest": fingerprint, "state": "completed", "checks_complete": True, "checks": []}
        if inputs:
            item["input_digest"] = inputs
        for category in categories:
            check = {"identity": policy["categories"][category][0]["identity"],
                     "revision": revision, "policy_digest": fingerprint, "attempt": 1,
                     "conclusion": "success", "source": "https://example.invalid/check/123",
                     "completed_at": f"2026-11-17T12:{end}:00Z"}
            if inputs:
                check["input_digest"] = inputs
            if category == "release":
                check["artifact_digest"] = ARTIFACT
            item["checks"].append(check)
        return item

    one = record(["rules", "change"], "00", "03")
    two = record(["rules", "change"], "01", "04", "d" * 40)
    one.update(pr="1", state="completed")
    two.update(pr="2", state="completed")
    release = record(["release"], "05", "07", inputs=INPUTS)
    release.update(artifact_digest=ARTIFACT, provenance={
        "verified": True, "source": "https://example.invalid/attestation/123",
        "verified_at": "2026-11-17T12:08:00Z", "revision": REV,
        "input_digest": INPUTS, "artifact_digest": ARTIFACT, "policy_digest": fingerprint})
    evidence = {
        "schema": "ci-validation-evidence/1", "repository": policy["repository"],
        "policy_digest": fingerprint, "observed_at": NOW.isoformat(),
        "pr_events_complete": True, "pr_events": [one, two],
        "integration_records_complete": True,
        "integration_records": [record(["integration"], "00", "05", inputs=INPUTS)],
        "integration_state": {"main_head_revision": REV, "unverified_commits": 0, "active": False},
        "release_candidate": release,
        "rollout_state": {"deployment_id": "deploy-1", "environment": "production", "revision": REV,
                          "artifact_digest": ARTIFACT, "stage_pct": 5, "stage_started_at": "2026-11-17T12:05:00Z"},
        "rollout_observation": {"revision": REV, "artifact_digest": ARTIFACT, "stage_pct": 5,
                                "deployment_id": "deploy-1", "environment": "production",
                                "stage_started_at": "2026-11-17T12:05:00Z",
                                "source": "https://example.invalid/metrics/query/123", "complete": True,
                                "window_started_at": "2026-11-17T12:06:00Z",
                                "window_ended_at": NOW.isoformat(), "samples": 1000,
                                "metrics": {"error_rate": 0.001, "p95_latency_ms": 100},
                                "baseline": {"p95_latency_ms": 100}, "baseline_comparable": True,
                                "baseline_source": "https://example.invalid/metrics/baseline/123",
                                "baseline_context": "Verified comparable environment, workload and request mix",
                                "baseline_window_started_at": "2026-11-17T12:00:00Z",
                                "baseline_window_ended_at": "2026-11-17T12:04:00Z"},
    }
    return policy, evidence


class ValidationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.policy, self.evidence = fixture()

    def evaluate(self):
        return validation.evaluate(self.policy, self.evidence, NOW)

    def test_complete_evidence_and_progressive_stage(self):
        report = self.evaluate()
        self.assertEqual(report["pr_budget"]["state"], "pass")
        self.assertEqual(report["integration"]["state"], "pass")
        self.assertEqual(report["release"]["state"], "pass")
        self.assertEqual(report["rollout"]["next_stage_pct"], 25)
        self.assertEqual(report["pr_budget"]["latest_revision_p95_s"], 180)

    def test_one_real_check_can_cover_multiple_categories(self):
        self.policy["categories"]["change"] = copy.deepcopy(self.policy["categories"]["rules"])
        fingerprint = validation.digest(self.policy)
        self.evidence = json.loads(json.dumps(self.evidence).replace(self.evidence["policy_digest"], fingerprint))
        for event in self.evidence["pr_events"]:
            event["checks"] = [event["checks"][0]]
        self.assertEqual(self.evaluate()["pr_budget"]["state"], "pass")
        self.policy["categories"]["change"][0]["allowed_skip_reasons"] = ["docs-only"]
        with self.assertRaises(ValueError):
            validation.validate_policy(self.policy)

    def test_cli_gate_requires_explicit_passing_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "policy.json").write_text(json.dumps(self.policy))
            args = ["--policy", str(root / "policy.json"), "--evidence", str(root / "evidence.json"),
                    "--now", NOW.isoformat(), "--require", "release"]
            (root / "evidence.json").write_text(json.dumps(self.evidence))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(validation.main(args), 0)
                self.evidence["release_candidate"]["provenance"]["verified"] = False
                (root / "evidence.json").write_text(json.dumps(self.evidence))
                self.assertEqual(validation.main(args), 1)
                self.evidence["policy_digest"] = "wrong"
                (root / "evidence.json").write_text(json.dumps(self.evidence))
                self.assertEqual(validation.main(args), 1)

    def test_latest_failed_event_invalidates_old_success_in_either_order(self):
        later = copy.deepcopy(self.evidence["pr_events"][0])
        later.update(received_at="2026-11-17T12:04:00Z", state="failed", revision="e" * 40)
        later["checks"] = []
        self.evidence["pr_events"].append(later)
        first = self.evaluate()["pr_budget"]
        self.evidence["pr_events"].reverse()
        second = self.evaluate()["pr_budget"]
        self.assertEqual(first, second)
        self.assertEqual(first["n_successful_latest"], 1)
        self.assertEqual(first["state"], "insufficient")

    def test_attempt_recovery_and_later_failure(self):
        event = self.evidence["pr_events"][0]
        retry = copy.deepcopy(event["checks"][0])
        retry.update(attempt=2, conclusion="failure", completed_at="2026-11-17T12:04:00Z")
        event["checks"].append(retry)
        self.assertEqual(self.evaluate()["pr_budget"]["prs"][0]["gate"]["state"], "blocked")
        retry["conclusion"] = "success"
        self.assertEqual(self.evaluate()["pr_budget"]["prs"][0]["latest_revision_feedback_s"], 240)

    def test_duplicate_event_is_ambiguous(self):
        self.evidence["pr_events"].append(copy.deepcopy(self.evidence["pr_events"][0]))
        report = self.evaluate()["pr_budget"]
        self.assertEqual(report["state"], "insufficient")
        self.assertTrue(any("ambiguous" in reason for reason in report["reasons"]))

    def test_skipped_and_neutral_do_not_mean_pass(self):
        for conclusion in ("skipped", "neutral", "in_progress", None):
            with self.subTest(conclusion=conclusion):
                self.evidence["pr_events"][0]["checks"][0]["conclusion"] = conclusion
                self.assertNotEqual(self.evaluate()["pr_budget"]["prs"][0]["gate"]["state"], "pass")

    def test_explicit_verified_skip_contract(self):
        self.policy["categories"]["rules"][0]["allowed_skip_reasons"] = ["docs-only"]
        fingerprint = validation.digest(self.policy)
        self.evidence = json.loads(json.dumps(self.evidence).replace(self.evidence["policy_digest"], fingerprint))
        check = self.evidence["pr_events"][0]["checks"][0]
        check.update(conclusion="skipped", skip_reason="docs-only", selection_verified=True)
        self.assertEqual(self.evaluate()["pr_budget"]["state"], "pass")
        check["selection_verified"] = False
        self.assertEqual(self.evaluate()["pr_budget"]["prs"][0]["gate"]["state"], "blocked")

    def test_superseded_slow_event_still_triggers_investigation(self):
        first = self.evidence["pr_events"][0]
        first["checks"][0]["completed_at"] = "2026-11-17T12:06:00Z"
        later = copy.deepcopy(first)
        later.update(received_at="2026-11-17T12:07:00Z", revision="f" * 40)
        for check in later["checks"]:
            check.update(revision=later["revision"], completed_at="2026-11-17T12:09:00Z")
        self.evidence["pr_events"].append(later)
        report = self.evaluate()["pr_budget"]
        self.assertEqual(report["state"], "breach")
        self.assertEqual(report["action"], "investigate_and_optimize")
        self.assertEqual(report["prs"][0]["latest_revision_feedback_s"], 120)
        self.assertEqual(report["prs"][0]["first_push_to_current_green_s"], 540)

    def test_slow_failure_triggers_budget_investigation_without_green(self):
        event = self.evidence["pr_events"][0]
        event.update(state="failed", completed_at="2026-11-17T12:07:00Z",
                     source="https://example.invalid/run/failed")
        event["checks"][0]["conclusion"] = "failure"
        report = self.evaluate()["pr_budget"]
        self.assertEqual(report["state"], "breach")
        self.assertEqual(report["completed_event_breaches"][0]["outcome"], "failed")
        self.assertEqual(report["n_successful_latest"], 1)
        self.assertIsNone(report["prs"][0]["latest_revision_feedback_s"])

    def test_running_breach_is_censored_not_a_success_sample(self):
        self.evidence["pr_events"][0].update(state="running", checks=[])
        report = self.evaluate()["pr_budget"]
        self.assertEqual(report["state"], "breach")
        self.assertEqual(report["inflight_breaches"][0]["elapsed_s"], 600)
        self.assertEqual(report["n_successful_latest"], 1)
        self.assertEqual(report["prs"][0]["gate"]["state"], "unknown")

    def test_missing_check_and_wrong_revision_are_unknown(self):
        for mutation in (lambda x: x["checks"].pop(),
                         lambda x: x["checks"][0].update(revision="f" * 40),
                         lambda x: x.update(checks_complete=False)):
            policy, evidence = fixture()
            mutation(evidence["pr_events"][0])
            report = validation.evaluate(policy, evidence, NOW)
            self.assertEqual(report["pr_budget"]["prs"][0]["gate"]["state"], "unknown")

    def test_full_suite_failure_blocks_release_of_same_revision(self):
        old = self.evidence["integration_records"][0]
        newer = copy.deepcopy(old)
        newer["received_at"] = "2026-11-17T12:06:00Z"
        newer["checks"][0].update(conclusion="failure", completed_at="2026-11-17T12:09:00Z")
        self.evidence["integration_records"].append(newer)
        report = self.evaluate()
        self.assertNotEqual(report["integration"]["state"], "pass")
        self.assertEqual(report["release"]["state"], "blocked")
        self.assertEqual(report["rollout"]["state"], "pause")

    def test_incomplete_or_cancelled_records_cannot_reuse_successful_checks(self):
        for state in (None, "running", "cancelled", "failed", "superseded"):
            for category in ("integration", "release"):
                with self.subTest(state=state, category=category):
                    policy, evidence = fixture()
                    record = evidence["integration_records"][0] if category == "integration" else evidence["release_candidate"]
                    record["state"] = state
                    report = validation.evaluate(policy, evidence, NOW)
                    self.assertNotEqual(report[category]["state"], "pass")
                    self.assertNotEqual(report["release"]["state"], "pass")

    def test_other_revision_green_cannot_refresh_old_exact_candidate_evidence(self):
        old = self.evidence["integration_records"][0]
        recent = copy.deepcopy(old)
        recent["revision"] = "e" * 40
        for check in recent["checks"]:
            check["revision"] = recent["revision"]
        old["received_at"] = "2026-11-17T11:00:00Z"
        for check in old["checks"]:
            check["completed_at"] = "2026-11-17T11:01:00Z"
        self.evidence["integration_records"].append(recent)
        self.evidence["integration_state"]["main_head_revision"] = recent["revision"]
        report = self.evaluate()
        self.assertEqual(report["integration"]["state"], "pass")
        self.assertEqual(report["release"]["state"], "blocked")
        self.assertIn("exact candidate full-suite evidence is stale", report["release"]["reasons"])

    def test_release_checks_are_bound_to_artifact_even_with_updated_provenance(self):
        replacement = "sha256:" + "d" * 64
        candidate = self.evidence["release_candidate"]
        candidate["artifact_digest"] = replacement
        candidate["provenance"]["artifact_digest"] = replacement
        report = self.evaluate()
        self.assertEqual(report["release"]["state"], "blocked")
        self.assertTrue(any("artifact provenance mismatch" in reason for reason in report["release"]["reasons"]))
        candidate["checks"][0]["artifact_digest"] = replacement
        self.assertEqual(self.evaluate()["release"]["state"], "pass")

    def test_release_artifact_checks_allow_only_latest_matching_attempt(self):
        candidate = self.evidence["release_candidate"]
        prior = copy.deepcopy(candidate["checks"][0])
        prior["artifact_digest"] = "sha256:" + "d" * 64
        candidate["checks"][0]["attempt"] = 2
        candidate["checks"].append(prior)
        self.assertEqual(self.evaluate()["release"]["state"], "pass")

    def test_all_release_and_rollout_times_must_precede_snapshot_observation(self):
        for field in ("receipt", "release_check", "provenance", "telemetry", "active_start"):
            with self.subTest(field=field):
                policy, evidence = fixture()
                evidence["observed_at"] = "2026-11-17T12:09:30Z"
                evidence["rollout_observation"]["window_ended_at"] = evidence["observed_at"]
                after = "2026-11-17T12:09:45Z"
                if field == "receipt":
                    evidence["release_candidate"]["received_at"] = after
                elif field == "release_check":
                    evidence["release_candidate"]["checks"][0]["completed_at"] = after
                elif field == "provenance":
                    evidence["release_candidate"]["provenance"]["verified_at"] = after
                elif field == "telemetry":
                    evidence["rollout_observation"]["window_ended_at"] = after
                else:
                    evidence["integration_state"].update(active=True, active_started_at=after)
                self.assertEqual(validation.evaluate(policy, evidence, NOW)["state"], "unknown")

    def test_full_suite_lag_and_starvation_bounds(self):
        self.evidence["integration_state"]["unverified_commits"] = 11
        self.assertEqual(self.evaluate()["integration"]["state"], "blocked")
        self.evidence["integration_state"].update(unverified_commits=1, active=True,
                                                  active_started_at="2026-11-17T11:00:00Z")
        self.assertEqual(self.evaluate()["integration"]["state"], "blocked")

    def test_rebuild_does_not_inherit_old_artifact_proof(self):
        self.evidence["release_candidate"]["artifact_digest"] = "sha256:" + "d" * 64
        self.assertEqual(self.evaluate()["release"]["state"], "blocked")

    def test_changed_build_inputs_do_not_inherit_full_suite(self):
        self.evidence["release_candidate"]["input_digest"] = "sha256:" + "d" * 64
        self.assertNotEqual(self.evaluate()["release"]["state"], "pass")

    def test_missing_inconclusive_and_bad_telemetry(self):
        telemetry = self.evidence["rollout_observation"]
        telemetry["samples"] = 10
        self.assertEqual(self.evaluate()["rollout"]["state"], "pause")
        telemetry["samples"] = 1000
        telemetry["metrics"]["error_rate"] = 0.02
        self.assertEqual(self.evaluate()["rollout"]["state"], "abort")
        telemetry["metrics"]["error_rate"] = 0.001
        telemetry["baseline"] = {}
        self.assertEqual(self.evaluate()["rollout"]["state"], "pause")

    def test_previous_deployment_environment_or_stage_cannot_supply_rollout_evidence(self):
        for key, value in (("deployment_id", "old-deploy"), ("environment", "staging"), ("stage_pct", 25),
                           ("stage_started_at", "2026-11-17T12:04:00Z")):
            with self.subTest(key=key):
                policy, evidence = fixture()
                evidence["rollout_observation"][key] = value
                self.assertEqual(validation.evaluate(policy, evidence, NOW)["rollout"]["state"], "pause")
        self.evidence.pop("rollout_state")
        self.assertEqual(self.evaluate()["rollout"]["state"], "pause")

    def test_telemetry_before_active_stage_cannot_authorize_promotion(self):
        self.evidence["rollout_state"]["stage_started_at"] = "2026-11-17T12:07:00Z"
        self.evidence["rollout_observation"]["stage_started_at"] = "2026-11-17T12:07:00Z"
        self.assertEqual(self.evaluate()["rollout"]["state"], "pause")

    def test_relative_threshold_requires_baseline_comparability_evidence(self):
        for key, value in (("baseline_comparable", False), ("baseline_source", ""), ("baseline_context", ""),
                           ("baseline_window_started_at", "2026-11-17T12:05:00Z"),
                           ("baseline_window_ended_at", None)):
            with self.subTest(key=key):
                policy, evidence = fixture()
                evidence["rollout_observation"][key] = value
                self.assertEqual(validation.evaluate(policy, evidence, NOW)["rollout"]["state"], "pause")

    def test_known_absolute_failure_still_aborts_without_comparable_baseline(self):
        self.evidence["rollout_observation"]["baseline_comparable"] = False
        self.evidence["rollout_observation"]["metrics"]["error_rate"] = 0.02
        self.assertEqual(self.evaluate()["rollout"]["state"], "abort")

    def test_baseline_and_stage_timestamps_cannot_follow_snapshot(self):
        for key in ("baseline_window_started_at", "baseline_window_ended_at", "stage_started_at"):
            with self.subTest(key=key):
                policy, evidence = fixture()
                evidence["observed_at"] = "2026-11-17T12:09:30Z"
                evidence["rollout_observation"]["window_ended_at"] = evidence["observed_at"]
                evidence["rollout_observation"][key] = "2026-11-17T12:09:45Z"
                self.assertEqual(validation.evaluate(policy, evidence, NOW)["state"], "unknown")

    def test_stale_policy_or_snapshot_does_not_pass(self):
        self.evidence["observed_at"] = "2026-11-17T12:00:00Z"
        self.assertEqual(self.evaluate()["state"], "unknown")
        self.evidence["observed_at"] = NOW.isoformat()
        self.policy["feedback_budget_s"] = 600
        self.assertEqual(self.evaluate()["state"], "unknown")

    def test_naive_future_and_conflicting_times_do_not_pass(self):
        for value in ("2026-11-17T12:00:00", "2026-11-17T13:00:00Z"):
            self.evidence["pr_events"][0]["received_at"] = value
            self.assertNotEqual(self.evaluate().get("pr_budget", {}).get("state"), "pass")
        self.policy, self.evidence = fixture()
        self.evidence["pr_events"][0]["checks"][0]["completed_at"] = "2026-11-17T13:00:00Z"
        self.assertEqual(self.evaluate()["state"], "unknown")

    def test_invalid_policy_fails(self):
        for key, value in (("feedback_budget_s", True), ("minimum_pr_samples", 0),
                           ("feedback_budget_s", float("nan"))):
            with self.subTest(key=key, value=value):
                policy, _ = fixture()
                policy[key] = value
                with self.assertRaises(ValueError):
                    validation.validate_policy(policy)

    def test_low_sample_and_incomplete_history_are_visible(self):
        self.evidence["pr_events_complete"] = False
        self.assertEqual(self.evaluate()["pr_budget"]["state"], "insufficient")

    def test_library_policy_can_omit_traffic_rollout(self):
        self.policy["rollout"] = {"enabled": False}
        fingerprint = validation.digest(self.policy)
        self.evidence = json.loads(json.dumps(self.evidence).replace(self.evidence["policy_digest"], fingerprint))
        self.assertEqual(self.evaluate()["rollout"]["state"], "not_applicable")
        self.assertEqual(self.evaluate()["release"]["state"], "pass")


if __name__ == "__main__":
    unittest.main()
