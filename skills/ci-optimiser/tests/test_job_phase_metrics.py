"""Per-job phase evidence must not conceal missing timing or failed first attempts."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_gh as analysis  # noqa: E402
import check_job_budgets as budgets  # noqa: E402
import phase_map as mappings  # noqa: E402


def stamp(seconds):
    return (datetime(2026, 11, 20, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def run(rid=1, attempt=1, **extra):
    return {"id": rid, "name": "ci", "workflow_id": 10, "head_sha": "a" * 40,
            "head_branch": "feature", "event": "pull_request", "run_attempt": attempt,
            "status": "completed", "conclusion": "success", "created_at": stamp(0), **extra}


def step(name, start, end, **extra):
    return {"name": name, "status": "completed", "conclusion": "success",
            "started_at": stamp(start), "completed_at": stamp(end), **extra}


def job(name="tests", queued=0, start=2, end=22, attempt=1, **extra):
    return {"name": name, "status": "completed", "conclusion": "success", "run_attempt": attempt,
            "created_at": stamp(queued), "started_at": stamp(start), "completed_at": stamp(end),
            "labels": ["linux"], "steps": [step("Install dependencies", start, start + 5),
                                           step("Run tests", start + 5, end)], **extra}


class JobPhaseMetricsTest(unittest.TestCase):
    def test_each_job_keeps_its_own_phase_percentiles(self):
        runs = [run(1), run(2)]
        jobs = {1: [job("lint", start=1, end=11), job("tests", start=40, end=100)],
                2: [job("lint", start=2, end=14), job("tests", start=50, end=120)]}
        result = analysis.analyze_group(runs, jobs)["job_phases"]
        self.assertEqual((result["lint"]["waiting_s"]["p50"], result["lint"]["waiting_s"]["p95"]), (1, 2))
        self.assertEqual((result["tests"]["waiting_s"]["p50"], result["tests"]["waiting_s"]["p95"]), (40, 50))
        self.assertEqual((result["tests"]["running_s"]["p50"], result["tests"]["running_s"]["p95"]), (55, 65))
        self.assertEqual(result["tests"]["complete_phase_observations"], 2)
        self.assertEqual(result["tests"]["unknown_by_metric"]["setup_s"], 0)

    def test_missing_install_finish_cannot_look_like_zero_setup(self):
        value = job(start=1, end=101, steps=[step("Install dependencies", 1, 91, completed_at=None),
                                           step("Run tests", 91, 101)])
        result = analysis.job_phase_metrics([run()], {1: [value]})["tests"]
        for key in ("setup_s", "running_s", "unattributed_s", "overlap_s"):
            self.assertEqual(result[key], "unknown")
            self.assertEqual(result["unknown_by_metric"][key], 1)
        self.assertEqual(result["waiting_s"]["p50"], 1)
        self.assertEqual(result["runner_s"]["p50"], 100)
        self.assertEqual(result["complete_phase_observations"], 0)
        self.assertEqual(result["unknown_phase_observations"], 1)

    def test_overlap_and_unattributed_time_are_distinct(self):
        value = job(start=0, end=30, steps=[step("Install dependencies", 0, 15),
                                          step("Run tests", 5, 20)])
        result = analysis.phase_observation(run(), value)
        self.assertEqual((result["setup_s"], result["running_s"], result["overlap_s"],
                          result["unattributed_s"], result["runner_s"]), (15, 15, 10, 10, 30))
        self.assertEqual(result["runner_s"], result["setup_s"] + result["running_s"]
                         - result["overlap_s"] + result["unattributed_s"])
        report = analysis.job_phase_metrics([run()], {1: [value]})["tests"]
        self.assertIn("percentiles are not additive", report["decomposition"])
        self.assertIn("heuristic", report["classification"])

    def test_runner_gaps_are_not_assigned_to_test_execution(self):
        value = job(start=0, end=50, steps=[step("Install dependencies", 5, 10),
                                          step("Run tests", 30, 40)])
        result = analysis.phase_observation(run(), value)
        self.assertEqual((result["setup_s"], result["running_s"], result["unattributed_s"]), (5, 10, 35))

    def test_successful_rerun_keeps_failed_first_attempt(self):
        first = job(start=2, end=22, conclusion="failure",
                    steps=[step("Install dependencies", 2, 7), step("Run tests", 7, 22, conclusion="failure")])
        later = job(queued=30, start=32, end=42, attempt=2)
        result = analysis.analyze_group([run(attempt=2)], {1: [first, later]})["job_phases"]["tests"]
        self.assertEqual(result["observations"], 2)
        self.assertEqual(result["outcomes"], {"failure": 1, "success": 1})
        self.assertEqual(result["attempts"], {"1": 1, "2": 1})
        self.assertEqual(result["first_attempt"]["outcomes"], {"failure": 1})
        self.assertEqual(result["first_attempt"]["running_s"]["p50"], 15)
        self.assertEqual(result["reruns"]["outcomes"], {"success": 1})
        self.assertEqual(result["reruns"]["running_s"]["p50"], 5)
        self.assertEqual(result["by_outcome"]["failure"]["observations"], 1)

    def test_skipped_cancelled_and_incomplete_jobs_are_not_zero_duration_passes(self):
        cases = [job(conclusion="skipped"), job(conclusion="cancelled"), job(conclusion="timed_out"),
                 job(status="in_progress", conclusion=None), job(status=None), job(conclusion={})]
        result = analysis.job_phase_metrics([run()], {1: cases})["tests"]
        self.assertEqual(result["observations"], len(cases))
        self.assertEqual(result["complete_phase_observations"], 0)
        for key in analysis.PHASE_METRICS:
            self.assertEqual(result[key], "unknown")
            self.assertEqual(result["unknown_by_metric"][key], len(cases))
        self.assertEqual(result["outcomes"]["skipped"], 1)
        self.assertEqual(result["outcomes"]["cancelled"], 1)

    def test_bad_step_evidence_invalidates_all_step_phases(self):
        cases = [None, [], {}, [None], [step("Run tests", 2, 22, completed_at=None)],
                 [step("Run tests", 2, 23)], [step("Run tests", 1, 22)],
                 [step("Run tests", 22, 2)], [step("Run tests", 2, 22, conclusion="skipped")],
                 [step("Run tests", 2, 22, status="in_progress")],
                 [step("Run tests", 2, 22, conclusion={})], [step(None, 2, 22)],
                 [step("Run tests", 2, 22, conclusion="failure")]]
        for steps in cases:
            with self.subTest(steps=steps):
                result = analysis.phase_observation(run(), job(steps=steps))
                self.assertIsNone(result["setup_s"])
                self.assertIsNone(result["running_s"])
                self.assertIsNone(result["unattributed_s"])
                self.assertEqual(result["phase_state"], "unknown")
                self.assertTrue(result["gaps"])

    def test_missing_queue_evidence_stays_unknown_without_discarding_known_work(self):
        for created in (None, "bad", stamp(3)):
            with self.subTest(created=created):
                result = analysis.phase_observation(run(), job(created_at=created))
                self.assertIsNone(result["waiting_s"])
                self.assertEqual(result["setup_s"], 5)
                self.assertEqual(result["running_s"], 15)
                self.assertEqual(result["phase_state"], "unknown")

    def test_bad_attempt_and_job_interval_cannot_supply_phases(self):
        for value in (job(attempt=None), job(attempt=True), job(attempt=2), job(name=None),
                      job(started_at=None), job(completed_at=None), job(completed_at=stamp(1))):
            with self.subTest(value=value):
                result = analysis.phase_observation(run(), value)
                self.assertTrue(all(result[key] is None for key in analysis.PHASE_METRICS))
                self.assertEqual(result["phase_state"], "unknown")
        report = analysis.job_phase_metrics([run()], {1: [job(attempt=None)]})["tests"]
        self.assertEqual(report["unknown_attempt"]["observations"], 1)
        self.assertEqual(report["first_attempt"]["waiting_s"], "unknown")

    def test_exact_duplicate_records_are_counted_once(self):
        value = job(id=10)
        report = analysis.job_phase_metrics([run()], {1: [value, copy.deepcopy(value)]})["tests"]
        self.assertEqual(report["observations"], 1)
        self.assertEqual(report["first_attempt"]["observations"], 1)

    def test_conflicting_job_ids_cannot_contribute_timing(self):
        values = [job(id=10), job(id=10, conclusion="failure")]
        report = analysis.job_phase_metrics([run()], {1: values})["tests"]
        self.assertEqual(report["observations"], 2)
        self.assertEqual(report["unknown_phase_observations"], 2)
        self.assertEqual(report["complete_phase_observations"], 0)
        self.assertEqual(report["waiting_s"], "unknown")
        self.assertEqual(report["gaps"]["conflicting records for one job ID"], 2)

    def test_report_is_nested_within_workflow_and_existing_cohorts(self):
        result = analysis.analyze([run()], {1: [job()]}, {})
        self.assertEqual(result["workflows"]["ci [pull_request]"]["job_phases"]["tests"]["observations"], 1)
        self.assertEqual(result["cohorts"][0]["metrics"]["job_phases"]["tests"]["observations"], 1)
        self.assertEqual(result["workflows"]["ci [pull_request]"]["job_duration_s"]["tests"]["p50"], 20)


class ReviewedPhaseMapTest(unittest.TestCase):
    def setUp(self):
        self.config = "sha256:" + "b" * 64
        self.run = run(cohort={"config": self.config})
        self.job = job(start=2, end=32, id=101, run_id=1, head_sha="a" * 40, steps=[
            step("Checkout", 2, 4, number=1), step("Restore cache", 4, 10, number=2),
            step("Test", 12, 22, number=3), step("Post cache save", 23, 28, number=4),
            step("Complete job", 28, 30, number=5)])
        self.value = {"schema": "ci-phase-map/1", "review_reference": "review/example",
                      "entries": [{"workflow_id": 10, "config": self.config,
                                   "job_identity": {"field": "name", "value": "tests"},
                                   "setup_end_step": 2, "steps": [
                                       {"number": n, "phase": phase} for n, phase in enumerate(mappings.PHASES, 1)]}]}

    def observe(self):
        return analysis.phase_observation(self.run, self.job, mappings.validate(self.value))

    def test_boundary_setup_and_separate_restore_save_teardown_keep_runner_cost(self):
        result = self.observe()
        self.assertEqual(result["classification"], "reviewed_map")
        self.assertEqual(result["phase_state"], "complete")
        self.assertEqual((result["setup_s"], result["running_s"], result["runner_s"], result["unattributed_s"]),
                         (10, 10, 30, 3))
        self.assertEqual(result["mapped_phase_s"], {"setup": 2, "restore": 6, "work": 10, "save": 5, "teardown": 2})
        self.assertEqual(result["runner_s"], result["setup_s"] + result["running_s"]
                         + result["mapped_phase_s"]["save"] + result["mapped_phase_s"]["teardown"]
                         - result["overlap_s"] + result["unattributed_s"])

    def test_display_name_cannot_change_reviewed_phase(self):
        before = self.observe()
        for row in self.job["steps"]:
            row["name"] = "save cache and install deps"  # Deliberately misleading and duplicated.
        self.assertEqual(self.observe(), before)

    def test_map_missing_wrong_config_workflow_or_job_stays_unknown(self):
        for updates in ({"workflow_id": 11}, {"workflow_id": True}, {"cohort": {}},
                        {"cohort": {"config": "sha256:" + "c" * 64}}, {"cohort": {"config": "main"}}):
            with self.subTest(updates=updates):
                current = {**self.run, **updates}
                result = analysis.phase_observation(current, self.job, mappings.validate(self.value))
                self.assertIsNone(result["setup_s"])
                self.assertEqual(result["classification"], "unknown")
                self.assertEqual(result["runner_s"], 30)
                self.assertEqual(result["waiting_s"], 2)
        self.job["name"] = "another job"
        self.assertIsNone(self.observe()["running_s"])

    def test_inventory_mismatch_duplicate_reordered_or_missing_numbers_fail_closed(self):
        original = copy.deepcopy(self.job["steps"])
        cases = [original[:-1], original + [step("Injected work", 30, 32, number=6)],
                 [*original[:1], {**original[1], "number": 1}, *original[2:]],
                 [{**original[0], "number": None}, *original[1:]],
                 [original[1], original[0], *original[2:]]]
        for steps in cases:
            with self.subTest(steps=steps):
                self.job["steps"] = steps
                result = self.observe()
                self.assertIsNone(result["setup_s"])
                self.assertIsNone(result["running_s"])
                self.assertTrue(all(value is None for value in result["mapped_phase_s"].values()))

    def test_conflicting_time_boundaries_are_unknown(self):
        for index, field, seconds in ((1, "completed_at", 13), (3, "started_at", 21)):
            with self.subTest(index=index):
                original = copy.deepcopy(self.job)
                self.job["steps"][index][field] = stamp(seconds)
                self.assertIsNone(self.observe()["setup_s"])
                self.job = original

    def test_overlapping_work_and_post_intervals_keep_union_invariant(self):
        self.job["steps"] = self.job["steps"][:3] + [step("More work", 14, 26, number=4),
                                                   step("Save", 26, 30, number=5),
                                                   step("Teardown", 28, 32, number=6)]
        self.value["entries"][0]["steps"] = [{"number": n, "phase": phase} for n, phase in
                                            enumerate(("setup", "restore", "work", "work", "save", "teardown"), 1)]
        result = self.observe()
        self.assertEqual((result["running_s"], result["overlap_s"], result["unattributed_s"]), (14, 2, 0))
        self.assertEqual(result["runner_s"], 10 + 14 + 4 + 4 - 2)

    def test_audit_matches_existing_strict_budget_boundaries(self):
        policy = {"schema": "ci-job-budgets/1", "jobs": {"tests": {
            "waiting_s": 10, "setup_s": 30, "running_s": 60, "runner_s": 120,
            "setup_end_step": "Restore cache", "work_steps": ["Test"]}}}
        checked = budgets.verify({"total_count": 1, "jobs": [self.job]}, policy)
        self.assertEqual(checked["status"], "pass")
        audit = self.observe()
        self.assertEqual(checked["jobs"][0]["measurements"], {key: audit[key] for key in budgets.METRICS})
        policy["jobs"]["tests"]["setup_s"] = audit["setup_s"]
        self.assertEqual(budgets.verify({"total_count": 1, "jobs": [self.job]}, policy)["status"], "fail")

    def test_schema_and_ambiguous_identity_are_rejected(self):
        for field, value in (("workflow_id", True), ("config", "mutable-main"), ("setup_end_step", 3),
                             ("steps", []), ("job_identity", {"field": "prefix", "value": "test"})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                current = copy.deepcopy(self.value)
                current["entries"][0][field] = value
                mappings.validate(current)
        self.value["entries"].append(copy.deepcopy(self.value["entries"][0]))
        with self.assertRaises(ValueError):
            mappings.validate(self.value)
        self.value["entries"][1]["job_identity"] = {"field": "logical_job_id", "value": "test-node"}
        self.job["logical_job_id"] = "test-node"
        self.assertIsNone(self.observe()["setup_s"])

    def test_map_propagates_through_analysis_and_cohort_context(self):
        current = {key: value for key, value in self.run.items() if key != "cohort"}
        context = {"schema": "ci-cohort-context/1", "runs": {"1": {"config": self.config}}}
        result = analysis.analyze([current], {1: [self.job]}, {}, phase_map=self.value, cohort_context=context)
        self.assertEqual(result["phase_mapping"]["review_reference"], "review/example")
        self.assertFalse(result["phase_mapping"]["independently_verified"])
        self.assertRegex(result["phase_mapping"]["sha256"], r"^[0-9a-f]{64}$")
        for group in (result["workflows"]["ci [pull_request]"], result["cohorts"][0]["metrics"]):
            report = group["job_phases"]["tests"]
            self.assertEqual(report["classification_counts"], {"reviewed_map": 1})
            self.assertEqual(report["mapped_phase_s"]["save"]["p50"], 5)
            self.assertEqual(report["first_attempt"]["unknown_by_mapped_phase"]["restore"], 0)

    def test_two_jobs_sharing_one_mapped_identity_are_unknown(self):
        other = {**self.job, "id": 102}
        report = analysis.job_phase_metrics([self.run], {1: [self.job, other]}, mappings.validate(self.value))["tests"]
        self.assertEqual(report["classification_counts"], {"unknown": 2})
        self.assertEqual(report["setup_s"], "unknown")
        self.assertEqual(report["mapped_phase_s"]["save"], "unknown")
        self.assertEqual(report["runner_s"]["n"], 2)

    def test_duplicate_json_and_null_map_are_not_an_opt_out(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "phases.json"
            for value in ('{"schema":"ci-phase-map/1","schema":"ci-phase-map/1"}', 'null'):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    mappings.load(path)
            path.write_text(json.dumps(self.value))
            self.assertIsInstance(mappings.load(path), mappings.PhaseMap)

    def test_no_map_is_explicitly_preliminary(self):
        result = analysis.phase_observation(self.run, self.job)
        self.assertEqual(result["classification"], "preliminary_heuristic")
        self.assertTrue(all(value is None for value in result["mapped_phase_s"].values()))


if __name__ == "__main__":
    unittest.main()
